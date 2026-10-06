"""The agent inbox (phase 2, docs/PRIVACY.md). A fake IMAP server over TLS stands in for the real one; no real mail is read."""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from fake_imap import CA_FILE, FakeImap
from helpers import VaultCase, make_pdf, make_settings, needs_pypdf
from gtd_agent.cli import main
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.mail import read_forward, screen_mail
from gtd_agent.mailbox import Mailbox, MailError

USER = "agent@example.org"
PASSWORD = "app-pw-Zx81q"
OWNER = "taylor@example.com"
CHANGES = {"SELECT", "STORE", "EXPUNGE", "MOVE", "COPY", "APPEND", "DELETE", "CREATE", "RENAME"}
AUTH = "mx.example.org"  # the receiving server's authserv-id
YOURS = (f"{AUTH}; dkim=pass (2048-bit key) header.d=example.com header.i=@example.com; "
         "dmarc=pass (p=quarantine dis=none) header.from=example.com")
FAILED = f"{AUTH}; dkim=fail header.d=example.com; dmarc=fail (p=quarantine) header.from=example.com"
STRANGER = f"{AUTH}; dkim=pass header.d=example.net; dmarc=pass header.from=example.net"


def message(sender: str, subject: str, results: tuple[str, ...] = (), body: str = "Hello", to: str = USER) -> bytes:
    """A made-up message as the agent inbox would store it: the receiving server's results on top."""
    slug = re.sub(r"\W+", ".", subject.lower()).strip(".")
    lines = [f"Authentication-Results: {result}" for result in results]
    lines += [f"From: {sender}", f"To: {to}", f"Subject: {subject}", "Date: Tue, 29 Sep 2026 10:00:00 -0400",
              f"Message-ID: <{slug}@example.com>", "MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8"]
    return ("\r\n".join(lines) + "\r\n\r\n" + body + "\r\n").encode("utf-8")


class MailCase(VaultCase):
    """A vault whose [mail] points at a running fake IMAP server this machine trusts."""
    remote = True
    password = PASSWORD
    trust = True
    move = True
    tls = "tls"

    def setUp(self) -> None:
        self.server = FakeImap(USER, PASSWORD, move=self.move, tls=self.tls)
        self.server.start()
        self.addCleanup(self.server.stop)
        patcher = mock.patch.dict(os.environ, {"GTD_SECRET_AGENT_MAIL": self.password, "SSL_CERT_FILE": CA_FILE})
        patcher.start()
        self.addCleanup(patcher.stop)
        if not self.trust:
            del os.environ["SSL_CERT_FILE"]
        super().setUp()

    def extra_config(self) -> str:
        return (f'[mail]\nenabled = true\nhost = "127.0.0.1"\nport = {self.server.port}\n'
                f'restricted_domains = ["restricted.example"]\nuser = "{USER}"\nown_addresses = ["{OWNER}"]\nauth_server = "{AUTH}"\n')

    def doctor(self) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.settings.config_path), "doctor"])
        return code, out.getvalue()


class ConnectTests(MailCase):
    def test_doctor_logs_in_and_counts_the_waiting_mail(self):
        self.server.add(b"Subject: one\r\n\r\nfirst\r\n")
        self.server.add(b"Subject: two\r\n\r\nsecond\r\n")
        _, out = self.doctor()
        self.assertIn(f"OK: Agent inbox {USER} (2 messages waiting: 0 from you, 2 others)", out)

    def test_the_check_changes_nothing_in_the_inbox(self):
        self.server.add(b"Subject: one\r\n\r\nfirst\r\n")
        with Mailbox(self.settings) as box:
            self.assertEqual(len(box.uids("INBOX", readonly=True)), 1)
        self.assertIn("EXAMINE", self.server.names())
        self.assertFalse(CHANGES & set(self.server.names()))


class WrongPasswordTests(MailCase):
    password = "not-the-app-pw"

    def test_a_refused_login_names_the_fix_and_never_prints_the_password(self):
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn(f"CHECK: Agent inbox · 127.0.0.1 refused the login for {USER}. Make a new app password for the "
                      "agent, then run: run.ps1 set-secret agent-mail", out)
        self.assertNotIn("not-the-app-pw", out)


class UntrustedCertificateTests(MailCase):
    trust = False

    def test_an_untrusted_certificate_stops_before_the_password_is_sent(self):
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("CHECK: Agent inbox · 127.0.0.1 sent a certificate this computer doesn't trust", out)
        self.assertIn("Check [mail] host", out)
        self.assertNotIn("LOGIN", self.server.names())


class MissingPasswordTests(MailCase):
    password = ""

    def test_doctor_asks_for_the_app_password(self):
        _, out = self.doctor()
        self.assertIn("CHECK: Agent inbox password saved · missing: run set-secret agent-mail", out)
        self.assertEqual(self.server.names(), [])


class UnreachableTests(MailCase):
    def test_an_unreachable_server_names_the_fix(self):
        self.server.stop()
        _, out = self.doctor()
        self.assertIn(f"CHECK: Agent inbox · Can't reach 127.0.0.1:{self.server.port}", out)
        self.assertIn("Check [mail] host and port", out)


class SenderTests(MailCase):
    def check_mail(self, minute: int = 0) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, minute))

    def test_only_your_authenticated_mail_is_read(self):
        self.server.add(message(f"Taylor <{OWNER}>", "Fwd: Room booking", (YOURS,)))
        self.server.add(message("Mallory <mallory@example.net>", "Hi there", (STRANGER,)))
        self.server.add(message(OWNER, "Urgent", (FAILED,)))
        messages = self.check_mail()
        self.assertEqual(self.server.subjects("GTD Done"), ["Fwd: Room booking"])
        self.assertEqual(self.server.subjects("GTD Ignored"), ["Hi there", "Urgent"])
        self.assertIn("Mail: moved 2 messages not from you to GTD Ignored", messages)

    def test_ignored_mail_is_judged_on_headers_alone_and_no_model_sees_it(self):
        stranger = self.server.add(message("mallory@example.net", "Hi there", (STRANGER,)))
        spoof = self.server.add(message(OWNER, "Urgent", (FAILED,)))
        self.check_mail()
        self.assertEqual({items for uid, items in self.server.fetched if uid in (stranger, spoof)},
                         {"BODY.PEEK[HEADER]"})
        self.assertEqual([name for name, _ in self.provider.calls if name == "mail"], [])

    def test_a_result_added_below_the_servers_own_does_not_count(self):
        self.server.add(message(OWNER, "Urgent", (FAILED, YOURS)))  # the sender wrote the second one
        self.check_mail()
        self.assertEqual(self.server.subjects("GTD Ignored"), ["Urgent"])

    def test_results_from_another_server_do_not_count(self):
        self.server.add(message(OWNER, "Relayed", (YOURS.replace(AUTH, "relay.example.net"),)))
        self.check_mail()
        self.assertEqual(self.server.subjects("GTD Ignored"), ["Relayed"])

    def test_mail_is_checked_every_five_minutes(self):
        self.check_mail(0)
        self.server.add(message("mallory@example.net", "Later", (STRANGER,)))
        self.check_mail(2)
        self.assertEqual(self.server.subjects("INBOX"), ["Later"])
        self.check_mail(5)
        self.assertEqual(self.server.subjects("GTD Ignored"), ["Later"])

    def test_doctor_counts_mail_from_you_and_from_others_without_changing_anything(self):
        self.server.add(message(OWNER, "Fwd: Room booking", (YOURS,)))
        self.server.add(message("mallory@example.net", "Hi there", (STRANGER,)))
        _, out = self.doctor()
        self.assertIn(f"OK: Agent inbox {USER} (2 messages waiting: 1 from you, 1 other)", out)
        self.assertFalse(CHANGES & set(self.server.names()))

    def test_doctor_stops_when_your_mail_arrives_without_results_it_can_trust(self):
        self.server.add(message(OWNER, "Fwd: Room booking", (YOURS.replace(AUTH, "relay.example.net"),)))
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn(f"CHECK: Mail from you · 1 message from your address has no passing DKIM or DMARC result "
                      f"from {AUTH} (results seen from: relay.example.net). Ask Claude before relying on mail.", out)


PROTON_SIGNATURE = "Sent with Proton Mail secure email."


def forward(original: str, subject: str, body: list[str], note: str = "Can you track this?",
            when: str = "Tuesday, September 29th, 2026 at 10:00 AM", results: tuple[str, ...] = (YOURS,),
            message_id: str = "", to: str = USER) -> bytes:
    """A forward the way Proton writes it in plain text: Taylor's note, the signature, the separator, the
    original's headers, then the original quoted with '> '."""
    text = "\n".join([note, "", "", "", PROTON_SIGNATURE, "", "", "------- Forwarded Message -------",
                      f"From: {original}", f"Date: On {when}", f"Subject: {subject}", f"To: Taylor <{OWNER}>", "",
                      *[f"> {line}" if line else ">" for line in body]])
    slug = message_id or re.sub(r"\W+", ".", subject.lower()).strip(".") + ".fwd"
    head = [f"Authentication-Results: {result}" for result in results]
    head += [f"From: Taylor <{OWNER}>", f"To: {to}", f"Subject: Fwd: {subject}",
             "Date: Wed, 30 Sep 2026 08:00:00 -0400", f"Message-ID: <{slug}@example.com>", "MIME-Version: 1.0",
             "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]
    return ("\r\n".join(head) + "\r\n\r\n" + text.replace("\n", "\r\n") + "\r\n").encode("utf-8")


ROOM = ["Hi Taylor,", "Can you confirm the room for Oct 6 by Friday?", "Pat", "",
        f"On Monday, September 28th, 2026 at 9:00 AM, Taylor <{OWNER}> wrote:", "", "> Do we have a room?"]

HTML_FORWARD = f"""<div>Can you track this?</div><div><br></div>
<div class="protonmail_signature_block"><div class="protonmail_signature_block-proton">Sent with
<a href="https://proton.me/mail/home">Proton Mail</a> secure email.</div></div><div><br></div>
<div class="protonmail_quote">
------- Forwarded Message -------<br>
From: Pat Lee &lt;pat@example.net&gt;<br>
Date: On Tuesday, September 29th, 2026 at 10:00 AM<br>
Subject: Room booking<br>
To: Taylor &lt;{OWNER}&gt;<br>
<br>
<blockquote class="protonmail_quote" type="cite">
<p>Hi Taylor,</p><p>Can you confirm the room for Oct 6 by Friday?</p><p>Pat</p>
<div class="gmail_quote">On Mon, Sep 28, 2026 at 9:00 AM Taylor &lt;{OWNER}&gt; wrote:<br>
<blockquote>Do we have a room?</blockquote></div>
</blockquote></div>"""


class ReadForwardTests(MailCase):
    def test_a_proton_forward_gives_the_original_without_its_history(self):
        mail = read_forward(forward("Pat Lee <pat@example.net>", "Room booking", ROOM), self.settings)
        self.assertTrue(mail.forwarded)
        self.assertEqual((mail.sender_name, mail.sender), ("Pat Lee", "pat@example.net"))
        self.assertEqual(mail.date, datetime(2026, 9, 29, 10, 0))
        self.assertEqual(mail.subject, "Room booking")
        self.assertEqual(mail.body, "Hi Taylor,\nCan you confirm the room for Oct 6 by Friday?\nPat")
        self.assertEqual(mail.note, "Can you track this?")

    def test_an_html_forward_reads_the_same(self):
        raw = (f"From: Taylor <{OWNER}>\r\nTo: {USER}\r\nSubject: Fwd: Room booking\r\n"
               "Message-ID: <html.fwd@example.com>\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/html; charset=utf-8\r\n\r\n" + HTML_FORWARD).encode("utf-8")
        mail = read_forward(raw, self.settings)
        self.assertEqual((mail.sender, mail.subject, mail.date), ("pat@example.net", "Room booking",
                                                                 datetime(2026, 9, 29, 10, 0)))
        self.assertEqual(mail.body, "Hi Taylor,\nCan you confirm the room for Oct 6 by Friday?\nPat")
        self.assertEqual(mail.note, "Can you track this?")

    def test_a_header_style_date_lands_in_your_zone_even_without_a_tz_database(self):
        raw = forward("Pat Lee <pat@example.net>", "Room booking", ROOM, when="Tue, 29 Sep 2026 14:00:00 +0000")
        self.assertEqual(read_forward(raw, self.settings).date, datetime(2026, 9, 29, 10, 0))
        from zoneinfo import ZoneInfoNotFoundError
        with mock.patch("gtd_agent.mail.ZoneInfo", side_effect=ZoneInfoNotFoundError("no tzdata")):
            self.assertIsNotNone(read_forward(raw, self.settings).date)

    def plain(self, text: str, sender: str = f"Taylor <{OWNER}>") -> bytes:
        return (f"From: {sender}\r\nTo: {USER}\r\nSubject: FW: Room booking\r\nMessage-ID: <other.fwd@example.com>\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\n" + text.replace("\n", "\r\n")).encode("utf-8")

    def test_outlook_and_apple_mail_forwards_read_the_same(self):
        outlook = ("Can you track this?\n\n________________________________\nFrom: Pat Lee <pat@example.net>\n"
                   "Sent: Tuesday, September 29, 2026 10:00 AM\nTo: Taylor <taylor@example.com>\nSubject: Room booking\n\n"
                   "Hi Taylor,\nCan you confirm the room for Oct 6 by Friday?\nPat\n")
        apple = ("Can you track this?\n\nBegin forwarded message:\n\nFrom: Pat Lee <pat@example.net>\n"
                 "Subject: Room booking\nDate: September 29, 2026 at 10:00:00 AM EDT\nTo: Taylor <taylor@example.com>\n\n"
                 "Hi Taylor,\nCan you confirm the room for Oct 6 by Friday?\nPat\n")
        for text in (outlook, apple, outlook.replace("________________________________\n", "")):
            mail = read_forward(self.plain(text), self.settings)
            self.assertTrue(mail.forwarded, text)
            self.assertEqual((mail.sender_name, mail.sender, mail.subject, mail.date),
                             ("Pat Lee", "pat@example.net", "Room booking", datetime(2026, 9, 29, 10, 0)))
            self.assertEqual(mail.body, "Hi Taylor,\nCan you confirm the room for Oct 6 by Friday?\nPat")
            self.assertEqual(mail.note, "Can you track this?")

    DANA = "Dana Roy <dana@example.org>"
    PASSED_ON = ("Taylor, can you take this one? I need it by Friday.\nDana\n\n"
                 "---------- Forwarded message ---------\nFrom: Pat Lee <pat@example.net>\n"
                 "Date: Tue, Sep 29, 2026 at 10:00 AM\nSubject: Room booking\nTo: Dana Roy <dana@example.org>\n\n"
                 "Hi Dana,\nCan you confirm the room for Oct 6?\nPat")

    def test_a_forward_someone_sends_you_stays_their_mail(self):
        history = "\n\nOn Mon, Sep 28, 2026 at 9:00 AM Dana Roy <dana@example.org> wrote:\n> Do we have a room?\n"
        mail = read_forward(self.plain(self.PASSED_ON + history, sender=self.DANA), self.settings)
        self.assertEqual((mail.sender_name, mail.sender, mail.subject), ("Dana Roy", "dana@example.org", "FW: Room booking"))
        self.assertEqual(mail.note, "")  # you wrote nothing above it: the words on top are Dana's
        self.assertEqual(mail.body, self.PASSED_ON)  # her ask, then what she passed on, its own history cut
        self.assertEqual(mail.to, (USER,))

    def test_addresses_in_a_passed_on_message_are_screened_too(self):
        mail = read_forward(self.plain(self.PASSED_ON.replace("pat@example.net", "alerts@chase.com"), sender=self.DANA),
                            self.settings)
        self.assertEqual(screen_mail(mail, self.settings), "money")
        self.assertIsNone(screen_mail(read_forward(self.plain(self.PASSED_ON, sender=self.DANA), self.settings),
                                      self.settings))

    def test_outlook_marks_under_someone_elses_words_are_a_forward_only_when_the_subject_says_fw(self):
        text = ("Can you take this one?\nDana\n\n-----Original Message-----\nFrom: Pat Lee <pat@example.net>\n"
                "Sent: Tuesday, September 29, 2026 10:00 AM\nTo: Dana Roy <dana@example.org>\nSubject: Room booking\n\n"
                "Can you confirm the room for Oct 6?\nPat\n")
        raw = self.plain(text, sender=self.DANA)
        self.assertIn("Can you confirm the room for Oct 6?", read_forward(raw, self.settings).body)
        reply = read_forward(raw.replace(b"Subject: FW:", b"Subject: RE:"), self.settings)
        self.assertEqual(reply.body, "Can you take this one?\nDana")  # under a reply the same marks head old history

    def test_a_note_you_write_yourself_is_not_a_forward(self):
        raw = (f"From: Taylor <{OWNER}>\r\nTo: {USER}\r\nSubject: Call the dentist\r\n"
               "Message-ID: <note@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               f"Book a cleaning\r\n\r\n{PROTON_SIGNATURE}\r\n").encode("utf-8")
        mail = read_forward(raw, self.settings)
        self.assertFalse(mail.forwarded)
        self.assertEqual((mail.subject, mail.note), ("Call the dentist", "Book a cleaning"))


class ReadOnceTests(MailCase):
    def check_mail(self, minute: int = 0) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, minute))

    def full_reads(self) -> int:
        return sum(1 for _, items in self.server.fetched if items == "BODY.PEEK[]")

    def test_a_forward_is_read_once_then_filed_in_done(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", ROOM))
        self.check_mail(0)
        self.assertEqual(self.server.subjects("GTD Done"), ["Fwd: Room booking"])
        self.assertEqual(self.server.subjects("INBOX"), [])
        self.check_mail(10)
        self.assertEqual(self.full_reads(), 1)

    def test_the_same_message_twice_is_read_once(self):
        raw = forward("Pat Lee <pat@example.net>", "Room booking", ROOM)
        self.server.add(raw)
        self.server.add(raw)
        self.check_mail()
        self.assertEqual(self.full_reads(), 1)
        self.assertEqual(self.server.subjects("GTD Done"), ["Fwd: Room booking", "Fwd: Room booking"])

    def test_sensitive_mail_is_skipped_before_any_model_and_leaves_only_a_count(self):
        self.server.add(forward("Chase <no-reply@alerts.chase.com>", "Your statement is ready", ["View it online."]))
        self.server.add(forward("Valley Clinic <portal@valleyclinic.example>", "New message",
                                ["Your lab results are ready in the patient portal."]))
        self.server.add(forward("Some App <hello@app.example>", "Welcome aboard", ["Your verification code is 482913."]))
        self.server.add(forward("Alex Smith <alex.smith@restricted.example>", "Next steps", ["Looking forward to January."]))
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", ROOM))
        self.check_mail()
        self.assertEqual(self.server.subjects("GTD Skipped"), ["Fwd: Your statement is ready", "Fwd: New message",
                                                            "Fwd: Welcome aboard", "Fwd: Next steps"])
        self.assertEqual(self.server.subjects("GTD Done"), ["Fwd: Room booking"])
        self.assertEqual([call["mail"]["subject"] for name, call in self.provider.calls if name == "mail"],
                         ["Room booking"])
        today = self.read("_agent/TODAY.md")
        self.assertIn("- Mail today: 1 message read (1 with nothing to act on) · 4 skipped as sensitive (money, health, "
                      "accounts, restricted work)",
                      today)
        for secret in ("statement is ready", "lab results", "482913", "alex.smith", "Next steps"):
            for path in self.base.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode("utf-8"), path.read_bytes(), f"{secret!r} left a trace in {path}")

    def test_your_own_list_skips_more(self):
        self.server.add(forward("Coach <coach@club.example>", "Dues", ["See you Monday."]))
        self.check_mail()
        self.assertEqual(self.server.subjects("GTD Skipped"), ["Fwd: Dues"])

    def extra_config(self) -> str:
        return super().extra_config() + 'private_senders = ["club.example"]\n'


class SharedMailboxTests(MailCase):
    """Several agents share one mailbox; this one owns only the mail sent to its own alias."""

    def extra_config(self) -> str:
        return super().extra_config() + 'address = "GTD@example.org"\n'

    def test_mail_for_other_aliases_is_left_for_their_agents(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", ROOM, to="gtd@example.org"))
        self.server.add(forward("Pat Lee <pat@example.net>", "Reading list", ["Here are the papers."],
                                to="research@example.org"))
        self.server.add(message("mallory@example.net", "Hi there", (STRANGER,), to="research@example.org"))
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        self.assertEqual(self.server.subjects("GTD Done"), ["Fwd: Room booking"])
        self.assertEqual(self.server.subjects("INBOX"), ["Fwd: Reading list", "Hi there"])
        self.assertEqual(self.server.subjects("GTD Ignored"), [])

    def test_doctor_counts_only_this_agents_mail(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", ROOM, to="gtd@example.org"))
        self.server.add(message("mallory@example.net", "Hi there", (STRANGER,), to="research@example.org"))
        _, out = self.doctor()
        self.assertIn(f"OK: Agent inbox {USER} as gtd@example.org (1 message waiting: 1 from you, 0 others · 1 for "
                      "other addresses)", out)


def mail_answer(target: str = "", actions: tuple = (), events: tuple = (), waits: tuple = (), answers: tuple = (),
                summary: str = "Pat asks about the lab room.", explanation: str = "An ask with a date.") -> dict:
    return {"target_key": target, "summary": summary, "actions": list(actions), "events": list(events),
            "waits": list(waits), "answers": list(answers), "explanation": explanation}


def answering(person: str, evidence: str):
    """A model answer that picks the open wait whose responder is `person`."""
    return lambda call: mail_answer(answers=[{"wait_id": next(w["id"] for w in call["open_waits"]
                                                            if w["person"] == person), "evidence": evidence}])


class ProposalTests(MailCase):
    def check_mail(self, minute: int = 0) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, minute))

    def approve(self, row_id: str) -> list[str]:
        text = self.read("_agent/APPROVAL.md")
        start = text.index(f"<!-- gtd-agent:proposal id={row_id} -->")
        end = text.index("<!-- gtd-agent:item-end -->", start)
        self.write("_agent/APPROVAL.md", text[:start] + text[start:end].replace("- [ ] Approve", "- [x] Approve", 1)
                   + text[end:])
        return self.agent.sync_review_requests()

    def line(self, rel: str, *words: str) -> str:
        return next(line for line in self.read(rel).splitlines() if all(word in line for word in words))

    def test_an_ask_and_a_date_become_one_proposal_that_names_the_mail(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", [
            "Hi Taylor,", "Can you confirm the room for Oct 6 by Friday?", "Kickoff is Tuesday Oct 6 at 3:30pm."]))
        self.provider.mails.append(mail_answer("DEMO_PROJECT", actions=[
            {"title": "Confirm the room for Oct 6", "context": "#computer", "due_evidence": "Friday",
             "evidence": "Can you confirm the room for Oct 6 by Friday?"}], events=[
            {"title": "Lab kickoff", "day_evidence": "Oct 6", "time_evidence": "3:30pm", "end_evidence": "",
             "evidence": "Kickoff is Tuesday Oct 6 at 3:30pm"}]))
        self.check_mail()
        rows = self.pending("mail")
        self.assertEqual(len(rows), 1)
        proposal = json.loads(rows[0]["proposal_json"])
        self.assertEqual(proposal["title"], "Pat Lee: Room booking")
        labels = [item["label"] for item in proposal["items"]]
        self.assertIn("Action · Confirm the room for Oct 6 · due Fri Oct 2", labels[0])
        self.assertIn("Event · Add to Proton Calendar: Lab kickoff, Tue Oct 6 15:30–16:30", labels[1])
        self.assertIn("## Mail", self.read("_agent/APPROVAL.md"))
        self.approve(rows[0]["id"])
        project = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
        self.assertIn("📅 2026-10-02", self.line(project, "Confirm the room for Oct 6 #computer"))
        self.assertIn("Mail from Pat Lee: Room booking.", self.read(project))

    def test_an_answer_marks_the_wait_replied_and_ticking_the_reply_closes_both(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Slides", ["Here are the slides you asked for."]))
        self.provider.mails.append(answering("Pat", "Here are the slides"))
        self.check_mail()
        self.approve(self.pending("mail")[0]["id"])
        single = "01_GTD/SINGLE_ACTIONS.md"
        wait = self.line(single, "#waiting", "Pat")
        self.assertIn("#replied", wait)
        reply = self.line(single, "Reply to Pat about slides")
        reply_id = reply.split("🆔 ", 1)[1].split()[0]
        self.assertIn(f"⛔ {reply_id}", wait)
        self.write(single, self.read(single).replace(reply, reply.replace("- [ ]", "- [x]", 1)))
        self.check_mail(10)
        self.assertTrue(self.line(single, "#waiting", "Pat").startswith("- [x]"))

    def test_an_answer_drops_the_follow_up_you_no_longer_owe(self):
        project = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
        self.check_mail(0)  # the admin office is past its follow-up date: a follow-up action is proposed
        self.approve(next(row["id"] for row in self.pending("followup") if "Admin office" in row["proposal_json"]))
        follow_id = self.line(project, "Follow up with Admin office").split("🆔 ", 1)[1].split()[0]
        self.assertIn(f"⛔ {follow_id}", self.line(project, "#waiting", "Admin office"))
        self.server.add(forward("Room Desk <rooms@example.edu>", "Room approval", ["Your room approval is attached."]))
        self.provider.mails.append(answering("Admin office", "Your room approval is attached."))
        self.check_mail(10)  # their answer arrives before you followed up
        self.approve(self.pending("mail")[0]["id"])
        self.assertNotIn(follow_id, self.line(project, "#waiting", "Admin office"))
        self.assertTrue(self.line(project, "Follow up with Admin office").startswith("- [-]"))
        reply = self.line(project, "Reply to Admin office")
        self.write(project, self.read(project).replace(reply, reply.replace("- [ ]", "- [x]", 1)))
        self.check_mail(20)
        self.assertTrue(self.line(project, "#waiting", "Admin office").startswith("- [x]"))  # your reply closes it
        self.assertEqual(self.pending("drop"), [])  # the agent cancelled the follow-up: no "why did you drop it?"

    def test_a_replied_wait_gets_no_follow_up(self):
        self.server.add(forward("Room Desk <rooms@example.edu>", "Room approval", ["Your room approval is attached."]))
        self.provider.mails.append(answering("Admin office", "Your room approval is attached."))
        self.check_mail()
        self.assertTrue(any("Admin office" in row["proposal_json"] for row in self.pending("followup")))  # overdue
        self.approve(self.pending("mail")[0]["id"])
        self.assertIn("Reply to Admin office about room approval #computer #next",
                      self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"))
        self.check_mail(10)
        self.assertFalse(any("Admin office" in row["proposal_json"] for row in self.pending("followup")))
        today = self.read("_agent/TODAY.md")
        self.assertIn("### Follow-ups due (1)", today)  # Riley still owes an answer; the admin office replied
        self.assertIn("tags do not include #replied", today)

    def test_mail_with_nothing_to_act_on_makes_no_proposal(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Thanks", ["Thanks for yesterday!"]))
        self.check_mail()
        self.assertEqual(self.pending("mail"), [])
        self.assertIn("- Mail today: 1 message read (1 with nothing to act on)", self.read("_agent/TODAY.md"))

    def test_items_the_mail_does_not_support_are_dropped(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Room", ["Can you confirm the room?"]))
        self.provider.mails.append(mail_answer(actions=[
            {"title": "Confirm the room", "context": "#computer", "due_evidence": "", "evidence": "confirm the room"},
            {"title": "Book a bus", "context": "#computer", "due_evidence": "", "evidence": "please book a bus"}]))
        self.check_mail()
        proposal = json.loads(self.pending("mail")[0]["proposal_json"])
        self.assertEqual(len(proposal["items"]), 1)
        self.assertIn("Dropped 1 item the mail did not support.", proposal["reason"])

    def test_open_waits_and_projects_marked_private_never_reach_the_model(self):
        single = "01_GTD/SINGLE_ACTIONS.md"
        self.write(single, self.read(single).replace("- [ ] #waiting Pat: slides", "- [ ] #waiting Pat: slides #private"))
        self.server.add(forward("Pat Lee <pat@example.net>", "Thanks", ["Thanks for yesterday!"]))
        self.check_mail()
        call = next(call for name, call in self.provider.calls if name == "mail")
        people = [wait["person"] for wait in call["open_waits"]]
        self.assertEqual(people, ["Admin office"])  # Pat's wait is #private; Riley's sits in a private area


class ContactTests(MailCase):
    CONTACT = "04_REFERENCE/CONTACTS/PAT_LEE.md"

    def check_mail(self, minute: int = 0) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, minute))

    def answer(self, label: str) -> list[str]:
        row = self.pending("contact")[0]
        self.tick(label, proposal_id=row["id"], suffix=" ✅ 2026-09-28")
        return self.agent.sync_review_requests()

    def test_a_new_sender_is_asked_once_and_kept_in_contacts(self):
        self.server.add(forward("Pat Lee <Pat@Example.net>", "Room booking", ROOM))
        self.check_mail()
        rows = self.pending("contact")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["proposal_json"])["title"], "New contact: Pat Lee <pat@example.net>")
        self.answer("Add Pat Lee")
        note = self.read(self.CONTACT)
        self.assertIn("type: contact", note)
        self.assertIn("aliases: [Pat Lee]", note)
        self.assertIn("emails: [pat@example.net]", note)
        self.server.add(forward("Pat Lee <pat@example.net>", "Slides", ["Here are the slides."]))
        self.check_mail(10)
        self.assertEqual(self.pending("contact"), [])

    def test_leave_it_never_asks_again(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", ROOM))
        self.check_mail()
        self.answer("Leave it")
        self.server.add(forward("Pat Lee <pat@example.net>", "Slides", ["Here are the slides."]))
        self.check_mail(10)
        self.assertEqual(self.pending("contact"), [])
        self.assertFalse(self.path(self.CONTACT).exists())

    def test_a_known_name_offers_its_contact_note(self):
        self.path("04_REFERENCE/CONTACTS").mkdir(parents=True, exist_ok=True)
        self.write(self.CONTACT, "---\ntype: contact\naliases: [Pat Lee]\nemails: [pat@school.example]\n---\n# PAT_LEE\n")
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", ROOM))
        self.check_mail()
        labels = [c["label"] for c in json.loads(self.pending("contact")[0]["proposal_json"])["choices"]]
        self.assertEqual(labels, ["Add to PAT_LEE", "New contact PAT_LEE_2"])
        self.answer("Add to PAT_LEE")
        self.assertIn("emails: [pat@school.example, pat@example.net]", self.read(self.CONTACT))

    def test_the_model_learns_which_waits_the_sender_owes(self):
        self.path("04_REFERENCE/CONTACTS").mkdir(parents=True, exist_ok=True)
        self.write("04_REFERENCE/CONTACTS/ROOM_DESK.md",
                   "---\ntype: contact\naliases: [Admin office, Room Desk]\nemails: [rooms@example.edu]\n---\n")
        self.server.add(forward("Room Desk <rooms@example.edu>", "Room approval", ["Approved."]))
        self.check_mail()
        call = next(call for name, call in self.provider.calls if name == "mail")
        self.assertEqual([w["person"] for w in call["open_waits"] if w["from_sender"]], ["Admin office"])
        self.assertEqual(self.pending("contact"), [])  # a known address asks nothing


class ModelOffTests(MailCase):
    remote = False

    def test_mail_waits_in_the_inbox_until_the_model_is_available(self):
        self.server.add(forward("Pat Lee <pat@example.net>", "Room booking", ROOM))
        messages = Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        self.assertEqual(self.server.subjects("INBOX"), ["Fwd: Room booking"])
        self.assertIn("Mail: 1 message waits for the model (remote inference is off)", messages)


class NoMoveExtensionTests(MailCase):
    move = False

    def test_ignored_mail_still_moves(self):
        self.server.add(message("mallory@example.net", "Hi there", (STRANGER,)))
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        self.assertEqual(self.server.subjects("INBOX"), [])
        self.assertEqual(self.server.subjects("GTD Ignored"), ["Hi there"])


PINNED = Path(CA_FILE).as_posix()
LABEL = "Labels/GTD"


class LabeledMailCase(MailCase):
    """Mail Taylor labels GTD in Proton, read through Bridge; with bridge False, mail he sends to the agent inbox."""
    bridge = True

    def extra_config(self) -> str:
        if not self.bridge:
            return super().extra_config()
        return (f'[mail]\nenabled = true\nsource = "bridge"\nhost = "127.0.0.1"\nport = {self.server.port}\n'
                f'restricted_domains = ["restricted.example"]\nuser = "{USER}"\nown_addresses = ["{OWNER}"]\nfolder = "Labels/GTD"\n')

    def add(self, raw: bytes) -> None:
        self.server.add(raw, "Labels/GTD" if self.bridge else "INBOX")

    def approve(self, row_id: str) -> list[str]:
        text = self.read("_agent/APPROVAL.md")
        start = text.index(f"<!-- gtd-agent:proposal id={row_id} -->")
        end = text.index("<!-- gtd-agent:item-end -->", start)
        self.write("_agent/APPROVAL.md", text[:start] + text[start:end].replace("- [ ] Approve", "- [x] Approve", 1)
                   + text[end:])
        return self.agent.sync_review_requests()

    def drafts(self) -> list:
        import email
        return [email.message_from_bytes(m["data"]) for m in self.server.folders.get("Drafts", [])]


class DraftTests(LabeledMailCase):
    """Replies and follow-ups get drafts in Taylor's voice. Through Bridge they land in Proton Drafts on approval."""

    def test_a_reply_draft_lands_in_proton_drafts_threaded_on_approval(self):
        if self.bridge:
            self.add(message("Pat Lee <pat@example.net>", "Slides", body="Here are the slides you asked for."))
        else:
            self.add(forward("Pat Lee <pat@example.net>", "Slides", ["Here are the slides you asked for."]))
        self.provider.mails.append(answering("Pat", "Here are the slides"))
        self.provider.draft_by_kind["reply"] = {"subject": "Re: Slides", "body": "Hi Pat,\n\nGot them, thanks!\n\nTaylor"}
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        row = self.pending("mail")[0]
        approval = self.read("_agent/APPROVAL.md")
        self.assertIn("Got them, thanks!", approval)
        self.assertIn("Subject: Re: Slides", approval)
        self.approve(row["id"])
        drafts = self.drafts()
        if not self.bridge:
            self.assertEqual(drafts, [])  # the agent inbox gets no drafts: the text is in APPROVAL
            return
        self.assertEqual(len(drafts), 1)
        self.assertEqual((drafts[0]["To"], drafts[0]["Subject"]), ("Pat Lee <pat@example.net>", "Re: Slides"))
        self.assertEqual(drafts[0]["In-Reply-To"], "<slides@example.com>")
        self.assertIn("Got them, thanks!", drafts[0].get_payload(decode=True).decode())
        self.assertIn("\\Draft", self.server.folders["Drafts"][0]["flags"])

    def test_a_follow_up_draft_goes_to_the_contact(self):
        self.path("04_REFERENCE/CONTACTS").mkdir(parents=True, exist_ok=True)
        self.write("04_REFERENCE/CONTACTS/ROOM_DESK.md",
                   "---\ntype: contact\naliases: [Admin office]\nemails: [rooms@example.edu]\n---\n")
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        row = next(r for r in self.pending("followup") if "Admin office" in r["proposal_json"])
        call = next(c for name, c in self.provider.calls if name == "draft" and c["kind"] == "follow_up")
        self.assertEqual((call["context"]["to_name"], call["context"]["what"]), ("Admin office", "room approval"))
        self.assertIn("Following up: room approval", self.read("_agent/APPROVAL.md"))
        self.approve(row["id"])
        if self.bridge:
            self.assertEqual([d["To"] for d in self.drafts()], ["Admin office <rooms@example.edu>"])

    def test_a_private_wait_gets_no_draft(self):
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        self.assertTrue(any("Riley" in r["proposal_json"] for r in self.pending("followup")))  # AREA_ADMIN is private
        self.assertFalse(any(c["context"].get("to_name") == "Riley" for name, c in self.provider.calls if name == "draft"))

    def test_your_voice_note_guides_every_draft(self):
        self.path("04_REFERENCE/SYSTEM").mkdir(parents=True, exist_ok=True)
        self.write("04_REFERENCE/SYSTEM/EMAIL_VOICE.md", "---\ntype: reference\n---\n# EMAIL_VOICE\n\nShort. Sign off: Best, Taylor\n")
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        call = next(c for name, c in self.provider.calls if name == "draft")
        self.assertIn("Sign off: Best, Taylor", call["voice"])


class SentReplyTests(LabeledMailCase):
    """Through Bridge, Taylor's reply in Sent ticks the 'Reply to' action, which closes the replied wait."""

    def test_your_reply_in_sent_closes_the_replied_wait(self):
        self.add(message("Pat Lee <pat@example.net>", "Slides", body="Here are the slides you asked for."))
        self.provider.mails.append(answering("Pat", "Here are the slides"))
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        self.approve(self.pending("mail")[0]["id"])
        single = "01_GTD/SINGLE_ACTIONS.md"
        self.assertIn("#replied", next(l for l in self.read(single).splitlines() if "#waiting Pat" in l))
        mine = self.server.add((f"From: Taylor <{OWNER}>\r\nTo: pat@example.net\r\nSubject: Re: Slides\r\n"
                                "In-Reply-To: <slides@example.com>\r\nMessage-ID: <reply@example.com>\r\n\r\nThanks!\r\n"
                                ).encode(), "Sent")
        other = self.server.add((f"From: Taylor <{OWNER}>\r\nTo: sam@example.net\r\nSubject: Lunch\r\n"
                                 "Message-ID: <lunch@example.com>\r\n\r\nNoon?\r\n").encode(), "Sent")
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 10))
        text = self.read(single)
        self.assertTrue(next(l for l in text.splitlines() if "Reply to Pat" in l).startswith("- [x]"))
        self.assertTrue(next(l for l in text.splitlines() if "#waiting Pat" in l).startswith("- [x]"))
        self.assertEqual({items for uid, items in self.server.fetched if uid in (mine, other)}, {"HEADER.FIELDS"})
        self.assertIn("your reply is in Sent", self.read("_agent/LOG.md"))


class MailCaptureTests(LabeledMailCase):
    """Mail Taylor writes to himself becomes Inbox lines, like Telegram, with no model call on the mail."""

    def to_self(self, subject: str, body: str, to: str = "", attach: bool = False) -> bytes:
        message = EmailMessage()
        if not self.bridge:
            message["Authentication-Results"] = YOURS
        message["From"] = f"Taylor <{OWNER}>"
        message["To"] = to or (OWNER if self.bridge else USER)
        message["Subject"] = subject
        slug = re.sub(r"\W+", ".", subject.lower())
        message["Message-ID"] = f"<self.{slug}@example.com>"
        message.set_content(body)
        if attach:
            message.add_attachment(b"%PDF-1.4 flyer", maintype="application", subtype="pdf", filename="flyer.pdf")
        return message.as_bytes()

    def inbox_lines(self) -> list[str]:
        return [line for line in self.read("00_INBOX/INBOX.md").splitlines() if line.startswith("- ")]

    def check(self, minute: int = 0) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, minute))

    def mail_calls(self) -> list[dict]:
        return [call for name, call in self.provider.calls if name == "mail"]

    def test_mail_to_yourself_becomes_inbox_lines(self):
        raw = self.to_self("Call the dentist", f"Book a cleaning\n\n- Ask about the bill\n\n{PROTON_SIGNATURE}\n")
        self.add(raw)
        self.add(raw)  # Proton keeps the Sent copy and the received copy, one Message-ID
        self.check(0)
        self.assertEqual(self.inbox_lines(), ["- Call the dentist", "- Book a cleaning", "- Ask about the bill"])
        self.assertEqual(self.mail_calls(), [])
        self.assertIn("- Mail today: 1 message to yourself captured", self.read("_agent/TODAY.md"))
        self.check(10)
        self.assertEqual(len(self.inbox_lines()), 3)

    def test_your_own_plus_address_still_counts_as_you(self):
        if not self.bridge:
            return  # Proton +aliases: a filter labels mail to you+gtd@ by itself
        self.add(self.to_self("Renew passport", "Book the photo first", to="Taylor <taylor+gtd@example.com>"))
        self.check()
        self.assertEqual(self.inbox_lines(), ["- Renew passport", "- Book the photo first"])

    def test_a_secret_or_restricted_work_never_lands(self):
        self.add(self.to_self("Wifi", "password: hunter22"))
        self.add(self.to_self("Audit", "Ask jane@restricted.example about the audit"))
        self.check()
        self.assertEqual(self.inbox_lines(), [])
        self.assertIn("2 skipped as sensitive (restricted work, secrets)", self.read("_agent/TODAY.md"))

    def test_a_long_message_stops_at_fifty_lines(self):
        self.add(self.to_self("Packing", "\n".join(f"Pack item {n}" for n in range(1, 54))))
        self.check()
        lines = self.inbox_lines()
        self.assertEqual((len(lines), lines[50]), (52, "- Pack item 50"))
        self.assertEqual(lines[-1], "- Read the rest of the mail 'Packing': 3 more lines")

    def test_mail_to_someone_else_or_with_an_attachment_is_read_as_mail(self):
        self.add(self.to_self("Flyer", "Sign up for this", attach=True))
        if self.bridge:  # the agent inbox only gets what you send or forward to it
            self.add(self.to_self("Slides", "Could you send the slides by Friday?", to="Pat Lee <pat@example.net>"))
        self.check()
        self.assertEqual(self.inbox_lines(), [])
        calls = [(c["mail"]["from"], c["mail"]["subject"], c["mail"]["to"]) for c in self.mail_calls()]
        expected = [(OWNER, "Flyer", [OWNER if self.bridge else USER])]
        if self.bridge:
            expected.append((OWNER, "Slides", ["pat@example.net"]))
        self.assertEqual(calls, expected)


class BridgeMissingPasswordTests(LabeledMailCase):
    password = ""

    def test_it_says_where_bridge_shows_the_password(self):
        _, out = self.doctor()
        self.assertIn("CHECK: Bridge password saved · missing: copy it from Bridge (your account › Mailbox details › "
                      "Password), then run set-secret agent-mail", out)
        with self.assertRaisesRegex(MailError, r"No Bridge password is saved\. Copy it from Bridge"):
            Mailbox(self.settings).open()
        self.assertEqual(self.server.names(), [])

    def test_today_says_what_mail_still_needs_and_the_log_hears_it_once(self):
        first = Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        self.assertTrue(any(m.startswith("CHECK: mail: No Bridge password is saved.") for m in first), first)
        self.assertIn("- Mail today: not connected: No Bridge password is saved.", self.read("_agent/TODAY.md"))
        later = Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 10))
        self.assertFalse(any(m.startswith("CHECK: mail") for m in later), later)
        self.assertIsNone(self.agent.health.snapshot()["last_error"])


class AgentInboxCaptureTests(MailCaptureTests):
    bridge = False


class ModelOffCaptureTests(LabeledMailCase):
    remote = False

    def test_mail_to_yourself_needs_no_model(self):
        self.add(MailCaptureTests.to_self(self, "Buy stamps", "At the post office"))
        self.add(message("Pat Lee <pat@example.net>", "Slides", body="Here are the slides you asked for."))
        messages = Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        self.assertIn("- Buy stamps", self.read("00_INBOX/INBOX.md"))
        self.assertIn("Mail: 1 message waits for the model (remote inference is off)", messages)
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 10))
        self.assertEqual(sum(1 for _, items in self.server.fetched if items == "BODY.PEEK[]"), 2)


class AgentInboxDraftTests(DraftTests):
    bridge = False


class PdfAttachmentTests(LabeledMailCase):
    """A PDF on labeled mail is read locally with pypdf; its text passes the screen, then the model reads it."""
    STATEMENT = ["Valley Credit Union", "Account statement", "Account number 4400 1234 1234 1234",
                 "Available balance $1,204.17"]

    def with_pdf(self, subject: str, lines: list[str], filename: str = "flyer.pdf") -> bytes:
        message = EmailMessage()
        message["From"] = "Pat Lee <pat@example.net>"
        message["To"] = OWNER
        message["Subject"] = subject
        message["Date"] = "Wed, 30 Sep 2026 08:00:00 -0400"
        message["Message-ID"] = f"<{subject.lower().replace(' ', '.')}@example.net>"
        message.set_content("Details attached.")
        message.add_attachment(make_pdf(lines), maintype="application", subtype="pdf", filename=filename)
        return message.as_bytes()

    def check(self) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))

    @needs_pypdf
    def test_a_date_in_a_pdf_becomes_a_dated_action(self):
        self.add(self.with_pdf("Charity 5K", ["Charity 5K", "Registration closes Friday, October 9."]))
        self.provider.mails.append(mail_answer(actions=[{
            "title": "Register for the charity 5K", "context": "#computer", "due_evidence": "October 9",
            "evidence": "Registration closes Friday, October 9"}]))
        self.check()
        call = next(c for name, c in self.provider.calls if name == "mail")
        self.assertEqual(call["mail"]["attachments"],
                         [{"name": "flyer.pdf", "text": "Charity 5K\nRegistration closes Friday, October 9."}])
        self.assertIn("Register for the charity 5K · due Fri Oct 9", self.read("_agent/APPROVAL.md"))

    @needs_pypdf
    def test_a_bank_statement_pdf_is_skipped_before_any_model(self):
        self.add(self.with_pdf("Documents", self.STATEMENT, filename="scan.pdf"))
        self.check()
        self.assertEqual([name for name, _ in self.provider.calls if name == "mail"], [])
        self.assertIn("1 skipped as sensitive (money)", self.read("_agent/TODAY.md"))
        for path in self.base.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"1,204.17", path.read_bytes(), f"the statement left a trace in {path}")

    @needs_pypdf
    def test_doctor_shows_the_pdf_reader(self):
        self.assertIn("OK: PDF reader (pypdf ", self.doctor()[1])

    def test_doctor_says_how_to_install_pypdf(self):
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            self.assertIn("CHECK: PDF reader · pypdf is not installed: run py -m pip install pypdf", self.doctor()[1])

    def test_without_pypdf_the_mail_is_read_and_the_pdf_is_named(self):
        self.add(self.with_pdf("Charity 5K", ["Registration closes Friday, October 9."]))
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            self.check()
        call = next(c for name, c in self.provider.calls if name == "mail")
        self.assertEqual(call["mail"]["attachments"], [{"name": "flyer.pdf", "text": ""}])
        row = self.pending("mail")[0]
        self.assertIn("flyer.pdf was not read: pypdf is not installed (py -m pip install pypdf)",
                      row["proposal_json"])


class CalendarMailTests(LabeledMailCase):
    """The agent can't write to Proton Calendar: events in mail, and invites, become actions to add by hand."""

    def check(self) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))

    def labels(self) -> list[str]:
        return [item["label"] for item in json.loads(self.pending("mail")[0]["proposal_json"])["items"]]

    def test_an_event_in_mail_carries_its_place(self):
        self.add(message("Pat Lee <pat@example.net>", "Lab tour", body="The lab tour is Oct 6 at 3pm in Room 204."))
        self.provider.mails.append(mail_answer(events=[{
            "title": "Lab tour", "day_evidence": "Oct 6", "time_evidence": "3pm", "end_evidence": "",
            "place": "Room 204", "evidence": "The lab tour is Oct 6 at 3pm"}]))
        self.check()
        self.assertEqual(self.labels(), ["Event · Add to Proton Calendar: Lab tour (Room 204), Tue Oct 6 15:00–16:00 "
                                         "→ Single action › @computer"])

    def test_a_forwarded_invite_becomes_an_action_from_the_invite_itself(self):
        invite = EmailMessage()
        invite["From"] = "Pat Lee <pat@example.net>"
        invite["To"] = OWNER
        invite["Subject"] = "Invitation: Budget review"
        invite["Message-ID"] = "<invite@example.net>"
        invite.set_content("You are invited to Budget review.")
        invite.add_attachment(("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:budget-1\r\n"
                               "DTSTART:20261007T100000\r\nDTEND:20261007T110000\r\nSUMMARY:Budget review\r\n"
                               "LOCATION:Zoom\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode(),
                              maintype="text", subtype="calendar", filename="invite.ics")
        self.add(invite.as_bytes())
        self.provider.mails.append(mail_answer(events=[{  # the model's copy of the same event is left out
            "title": "Budget review", "day_evidence": "Oct 7", "time_evidence": "10am", "end_evidence": "",
            "place": "", "evidence": "Budget review"}]))
        self.check()
        self.assertEqual(self.labels(), ["Invite · Add to Proton Calendar: Budget review (Zoom), Wed Oct 7 10:00–11:00 "
                                         "→ Single action › @computer"])


class NoSendingTests(unittest.TestCase):
    def test_no_code_path_sends_mail(self):
        source = Path(__file__).resolve().parents[1] / "gtd_agent"
        for path in source.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in ("smtplib", "SMTP(", "sendmail", "send_message("):
                self.assertNotIn(word, text, f"{path.name} mentions {word}")


class BridgeTests(MailCase):
    """Proton through Bridge: only mail Taylor labels GTD is read, and nothing in the mailbox changes."""

    def extra_config(self) -> str:
        return (f'[mail]\nenabled = true\nsource = "bridge"\nhost = "127.0.0.1"\nport = {self.server.port}\n'
                f'restricted_domains = ["restricted.example"]\nuser = "{USER}"\nown_addresses = ["{OWNER}"]\nfolder = "{LABEL}"\n')

    def check_mail(self, minute: int = 0) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, minute))

    def test_only_the_gtd_label_is_read_and_nothing_in_the_mailbox_changes(self):
        self.server.add(message("Pat Lee <pat@example.net>", "Room booking", body="Can you confirm the room?"), LABEL)
        self.server.add(message("Sam Roe <sam@example.net>", "Lunch", body="Lunch Friday?"), "INBOX")
        self.server.add(message("Chase <alerts@chase.com>", "Statement", body="Your statement."), "Folders/Private")
        self.check_mail()
        calls = [call for name, call in self.provider.calls if name == "mail"]
        self.assertEqual([(c["mail"]["from"], c["mail"]["subject"]) for c in calls], [("pat@example.net", "Room booking")])
        self.assertEqual([args[0] for name, args in self.server.commands if name in ("SELECT", "EXAMINE")], [LABEL])
        self.assertEqual({name for name, _ in self.server.commands} & CHANGES, set())
        self.assertFalse({args[0].upper() for name, args in self.server.commands if name == "UID"}
                         & {"MOVE", "COPY", "STORE", "EXPUNGE"})
        self.assertEqual(self.server.subjects(LABEL), ["Room booking"])

    def test_a_forward_someone_sends_you_reaches_the_model_as_their_mail(self):
        body = ("Taylor, can you take this one? I need it by Friday.\r\nDana\r\n\r\n"
                "---------- Forwarded message ---------\r\nFrom: Pat Lee <pat@example.net>\r\n"
                "Date: Tue, Sep 29, 2026 at 10:00 AM\r\nSubject: Room booking\r\nTo: Dana Roy <dana@example.org>\r\n\r\n"
                "Hi Dana,\r\nCan you confirm the room for Oct 6?\r\nPat")
        self.server.add(message("Dana Roy <dana@example.org>", "Fwd: Room booking", body=body), LABEL)
        self.check_mail()
        sent = next(call for name, call in self.provider.calls if name == "mail")["mail"]
        self.assertEqual((sent["from"], sent["subject"], sent["note"]), ("dana@example.org", "Fwd: Room booking", ""))
        self.assertIn("I need it by Friday.", sent["body"])
        self.assertIn("Can you confirm the room for Oct 6?", sent["body"])

    def test_a_missing_label_shows_in_today_until_you_make_it(self):
        self.check_mail(0)
        self.assertIn("- Mail today: not connected: Proton has no GTD label yet. Create it in Proton Mail › Settings › "
                      "All settings › Folders and labels.", self.read("_agent/TODAY.md"))
        self.server.add(message("Pat Lee <pat@example.net>", "Room booking", body="Can you confirm the room?"), LABEL)
        self.check_mail(10)
        self.assertNotIn("not connected", self.read("_agent/TODAY.md"))

    def test_today_says_so_when_bridge_is_closed(self):
        self.server.stop()
        self.check_mail()
        self.assertIn(f"- Mail today: not connected: Can't reach Proton Mail Bridge at 127.0.0.1:{self.server.port}. "
                      "Open Bridge and leave it running.", self.read("_agent/TODAY.md"))

    def test_the_mailbox_opens_only_the_label_and_sent_never_private(self):
        self.server.add(message("Chase <alerts@chase.com>", "Statement", body="Your statement."), "Folders/Private")
        self.server.add(message("Pat Lee <pat@example.net>", "Room booking", body="Can you confirm the room?"), LABEL)
        self.server.add(message(f"Taylor <{OWNER}>", "Re: Room booking", body="Yes."), "Sent")
        with Mailbox(self.settings) as box:  # the wall holds whatever the caller asks for
            for folder in ("Folders/Private", "INBOX", "All Mail", "Labels/Other"):
                with self.subTest(folder=folder), self.assertRaisesRegex(MailError, "never opens"):
                    box.uids(folder, readonly=True)
            with self.assertRaisesRegex(MailError, "only looks"):
                box.uids(LABEL)  # a read-write SELECT could change flags
            with self.assertRaisesRegex(MailError, "never moves"):
                box.move(1, "Folders/Private")
            with self.assertRaisesRegex(MailError, "only into Drafts"):
                box.save_draft(b"Subject: x\r\n\r\nx\r\n", "Folders/Private")
            self.assertEqual(len(box.uids(LABEL, readonly=True)), 1)
            self.assertEqual(len(box.uids("Sent", readonly=True)), 1)
        self.assertEqual([args[0] for name, args in self.server.commands if name in ("SELECT", "EXAMINE")],
                         [LABEL, "Sent"])
        self.assertEqual({name for name, _ in self.server.commands} & CHANGES, set())

    def test_labeled_mail_is_read_once(self):
        self.server.add(message("Pat Lee <pat@example.net>", "Room booking", body="Can you confirm the room?"), LABEL)
        self.check_mail(0)
        self.check_mail(10)
        self.assertEqual(sum(1 for _, items in self.server.fetched if items == "BODY.PEEK[]"), 1)

    def test_sensitive_labeled_mail_is_skipped_where_it_is(self):
        self.server.add(message("Chase <alerts@chase.com>", "Statement", body="Your statement is ready."), LABEL)
        self.check_mail()
        self.assertEqual([name for name, _ in self.provider.calls if name == "mail"], [])
        self.assertEqual(self.server.subjects(LABEL), ["Statement"])
        self.assertIn("- Mail today: 1 skipped as sensitive (money)", self.read("_agent/TODAY.md"))
        self.check_mail(10)
        self.assertEqual(sum(1 for _, items in self.server.fetched if items == "BODY.PEEK[]"), 1)

    def test_doctor_counts_labeled_mail(self):
        self.server.add(message("Pat Lee <pat@example.net>", "Room booking"), LABEL)
        self.server.add(message("Sam Roe <sam@example.net>", "Lunch"), LABEL)
        _, out = self.doctor()
        self.assertIn(f"OK: Proton via Bridge {USER} (2 in {LABEL})", out)
        self.assertFalse({name for name, _ in self.server.commands} & CHANGES)




class StartTlsTests(MailCase):
    tls = "starttls"
    trust = False  # the pinned certificate is the only trust

    def extra_config(self) -> str:
        return super().extra_config() + f'security = "starttls"\nca_file = "{PINNED}"\n'

    def test_starttls_with_a_pinned_certificate(self):
        _, out = self.doctor()
        self.assertIn(f"OK: Agent inbox {USER} (0 messages waiting)", out)
        self.assertLess(self.server.names().index("STARTTLS"), self.server.names().index("LOGIN"))


class NoStartTlsTests(MailCase):
    tls = "none"

    def extra_config(self) -> str:
        return super().extra_config() + 'security = "starttls"\n'

    def test_a_server_without_starttls_is_refused_before_the_password(self):
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("127.0.0.1 didn't offer STARTTLS; the agent never logs in without TLS", out)
        self.assertNotIn("LOGIN", self.server.names())


class PinnedCertificateTests(MailCase):
    trust = False

    def extra_config(self) -> str:
        return super().extra_config() + f'ca_file = "{PINNED}"\n'

    def test_a_pinned_certificate_needs_nothing_from_the_system_store(self):
        _, out = self.doctor()
        self.assertIn(f"OK: Agent inbox {USER} (0 messages waiting)", out)


class MailSettingsTests(unittest.TestCase):
    def load(self, mail: str):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return make_settings(Path(temp.name), extra="[mail]\n" + mail)

    def test_mail_is_off_unless_enabled(self):
        settings = self.load("")
        self.assertFalse(settings.mail_enabled)
        self.assertEqual(settings.mail_port, 993)
        self.assertEqual(settings.mail_check_minutes, 5)
        self.assertEqual(self.load('host = "posteo.de"\n').mail_auth_server, "posteo.de")

    def test_an_enabled_inbox_needs_host_user_and_your_addresses(self):
        with self.assertRaisesRegex(ValueError, "host, user and own_addresses"):
            self.load('enabled = true\nhost = "mail.example.org"\nuser = "a@example.org"\n')

    def test_mail_always_uses_tls(self):
        with self.assertRaisesRegex(ValueError, "tls or starttls"):
            self.load('security = "none"\n')

    def test_through_bridge_the_folder_must_be_a_label(self):
        for folder in ("INBOX", "Folders/Private", "All Mail", "Sent", "Labels/"):
            with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, "reads only a label"):
                self.load(f'source = "bridge"\nfolder = "{folder}"\n')
        self.assertEqual(self.load('source = "bridge"\n').mail_folder, "Labels/GTD")

    def test_your_addresses_are_a_list_kept_in_lower_case(self):
        with self.assertRaisesRegex(ValueError, "own_addresses must be a list"):
            self.load('own_addresses = "b@example.com"\n')
        settings = self.load('own_addresses = ["Taylor@Example.com "]\n')
        self.assertEqual(settings.mail_own_addresses, ("taylor@example.com",))


if __name__ == "__main__":
    unittest.main()
