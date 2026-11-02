"""Mail that asks SampleOwner something gets a reply draft in his voice (phase 3 item 1).

One box in the mail's proposal; approving saves the draft into Proton Drafts, threaded to the mail. The agent writes
what it can and leaves [square brackets] for the answers only he can give. It never sends.
"""
from __future__ import annotations

from datetime import datetime

import test_mail
from gtd_agent.housekeeping import Housekeeper
from test_mail import OWNER, STRANGER, forward, mail_answer, message

QUESTION = "Can you meet Thursday at 3pm to go over the slides?"
ASKS = "Whether SampleOwner can meet Thursday at 3pm"
DRAFT = {"subject": "Re: Thursday", "body": "Hi Pat,\n\n[Yes or no] on Thursday at 3pm.\n\nSampleOwner"}


def asking(evidence: str = "Can you meet Thursday at 3pm", asks: str = ASKS):
    return mail_answer(replies=[{"asks": asks, "evidence": evidence}])


class ReplyCase(test_mail.LabeledMailCase):
    def ask(self, sender: str = "Pat Lee <pat@example.net>", subject: str = "Thursday", body: str = "",
            headers: tuple[str, ...] = (), to: str = test_mail.USER) -> bytes:
        raw = message(sender, subject, body=body or f"Hi SampleOwner,\n{QUESTION}\nPat", to=to)
        if headers:
            raw = raw.replace(b"Subject:", ("\r\n".join(headers) + "\r\nSubject:").encode(), 1)
        return raw

    def check(self, minute: int = 0) -> list[str]:
        return Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, minute))

    def draft_calls(self) -> list[dict]:
        """The draft model's calls for mail (the vault's own overdue waits make follow-up drafts too)."""
        return [call for name, call in self.provider.calls if name == "draft" and call["kind"] != "follow_up"]

    def mail_calls(self) -> list[dict]:
        return [call for name, call in self.provider.calls if name == "mail"]

    def read_mail(self, raw: bytes, answer=None) -> None:
        self.add(raw)
        self.provider.mails.append(answer if answer is not None else asking())
        self.provider.draft_by_kind["answer"] = DRAFT
        self.check()


class ReplyDraftTests(ReplyCase):
    def test_a_question_gets_a_reply_draft_that_waits_in_approval(self):
        self.read_mail(self.ask())
        self.assertEqual(len(self.pending("mail")), 1)
        approval = self.read("_agent/APPROVAL.md")
        self.assertIn("Reply · draft to Pat Lee", approval)
        self.assertIn("Subject: Re: Thursday", approval)
        self.assertIn("[Yes or no] on Thursday at 3pm.", approval)
        call = self.draft_calls()[0]
        self.assertEqual(call["kind"], "answer")
        self.assertEqual(call["context"]["asks"], ASKS)
        self.assertEqual(call["context"]["their_subject"], "Thursday")
        self.assertIn("go over the slides", call["context"]["their_mail"])
        self.assertEqual(self.drafts(), [])  # nothing reaches Proton before he approves

    def test_approving_saves_it_threaded_to_the_mail_and_changes_no_note(self):
        self.read_mail(self.ask())
        notes = {rel: self.read(rel) for rel in ("01_GTD/SINGLE_ACTIONS.md", "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md")}
        self.approve(self.pending("mail")[0]["id"])
        drafts = self.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual((drafts[0]["To"], drafts[0]["Subject"]), ("Pat Lee <pat@example.net>", "Re: Thursday"))
        self.assertEqual(drafts[0]["In-Reply-To"], "<thursday@example.com>")
        self.assertEqual(drafts[0]["References"], "<thursday@example.com>")
        self.assertIn("[Yes or no]", drafts[0].get_payload(decode=True).decode())
        self.assertIn("\\Draft", self.server.folders["Drafts"][0]["flags"])
        self.assertEqual(notes, {rel: self.read(rel) for rel in notes})
        self.assertIn("Saved 1 draft to your Proton Drafts", self.read("_agent/LOG.md"))

    def test_the_draft_waits_in_the_outbox_when_bridge_is_closed(self):
        self.read_mail(self.ask())
        self.server.stop()
        self.approve(self.pending("mail")[0]["id"])
        self.check(1)
        self.assertIn("Drafts waiting for Proton: 1", self.read("_agent/TODAY.md"))
        self.server.start()
        self.check(2)
        self.assertEqual(len(self.drafts()), 1)

    def test_a_question_the_mail_does_not_contain_makes_no_draft(self):
        self.read_mail(self.ask(), asking(evidence="Will you be at the party on Saturday"))
        self.assertEqual(self.pending("mail"), [])
        self.assertEqual(self.draft_calls(), [])

    def test_the_proposal_keeps_its_other_items(self):
        answer = mail_answer(
            actions=[{"title": "Send Pat the slides", "context": "#computer", "due_evidence": "",
                      "evidence": "go over the slides"}],
            replies=[{"asks": ASKS, "evidence": "Can you meet Thursday at 3pm"}])
        self.read_mail(self.ask(), answer)
        approval = self.read("_agent/APPROVAL.md")
        self.assertIn("Action · Send Pat the slides", approval)
        self.assertIn("Reply · draft to Pat Lee", approval)

    def test_an_answered_wait_keeps_its_own_reply_and_gets_no_second_draft(self):
        def both(call):
            wait = next(w for w in call["open_waits"] if w["person"] == "Pat")
            return mail_answer(answers=[{"wait_id": wait["id"], "evidence": "Here are the slides"}],
                               replies=[{"asks": ASKS, "evidence": "Can you meet Thursday at 3pm"}])
        self.provider.draft_by_kind["reply"] = {"subject": "Re: Slides", "body": "Hi Pat,\n\nThanks!\n\nSampleOwner"}
        self.read_mail(self.ask(subject="Slides", body=f"Here are the slides you asked for.\n{QUESTION}"), both)
        self.assertEqual([call["kind"] for call in self.draft_calls()], ["reply"])
        self.approve(self.pending("mail")[0]["id"])
        self.assertEqual(len(self.drafts()), 1)


class NoDraftTests(ReplyCase):
    def test_no_reply_draft_goes_to_an_automated_sender(self):
        cases = (
            self.ask(sender="Campus Alerts <noreply@example.net>", subject="Survey"),
            self.ask(sender="Campus News <hello@example.net>", subject="Digest",
                     headers=("List-Unsubscribe: <mailto:leave@example.net>",)),
            self.ask(sender="Robot <robot@example.net>", subject="Ticket", headers=("Auto-Submitted: auto-generated",)),
            self.ask(sender="Dept List <dept@example.net>", subject="Seminar", headers=("Precedence: bulk",)),
        )
        for raw in cases:
            self.add(raw)
            self.provider.mails.append(asking())
        self.provider.draft_by_kind["answer"] = DRAFT
        self.check()
        self.assertEqual(self.draft_calls(), [])
        self.assertEqual(self.pending("mail"), [])

    def test_no_reply_draft_for_mail_you_wrote_to_someone_else(self):
        self.read_mail(self.ask(sender=f"SampleOwner <{OWNER}>", to="pat@example.net"))
        self.assertEqual(len(self.mail_calls()), 1)  # it is read like any labeled mail ...
        self.assertEqual(self.draft_calls(), [])  # ... but you do not answer yourself
        self.assertEqual(self.pending("mail"), [])

    def test_a_sensitive_mail_reaches_no_model_so_no_draft_is_written(self):
        raw = self.ask(sender="Valley Credit Union <member@example.net>", subject="Your statement",
                       body="Your account statement is ready. Can you confirm the payment of $2,450.00 by Friday?")
        self.add(raw)
        self.provider.draft_by_kind["answer"] = DRAFT
        self.check()
        self.assertEqual((self.mail_calls(), self.draft_calls()), ([], []))  # no model saw it
        self.assertEqual(self.drafts(), [])

    def test_a_mail_is_read_once_so_it_makes_one_draft(self):
        self.read_mail(self.ask())
        self.check(1)
        self.check(2)
        self.assertEqual(len(self.draft_calls()), 1)
        self.assertEqual(len(self.pending("mail")), 1)


class SwitchedOffTests(ReplyCase):
    def extra_config(self) -> str:
        return super().extra_config() + "reply_drafts = false\n"

    def test_reply_drafts_can_be_switched_off(self):
        self.read_mail(self.ask())
        self.assertEqual(self.draft_calls(), [])
        self.assertEqual(self.pending("mail"), [])


class AgentInboxTests(ReplyCase):
    """In the agent-inbox mode a draft is only text in APPROVAL, so a question makes no draft at all."""
    bridge = False

    def test_no_reply_draft_without_bridge(self):
        self.add(forward("Pat Lee <pat@example.net>", "Thursday", ["Hi SampleOwner,", QUESTION, "Pat"]))
        self.provider.mails.append(asking())
        self.provider.draft_by_kind["answer"] = DRAFT
        self.check()
        self.assertEqual(self.draft_calls(), [])
        self.assertEqual(self.pending("mail"), [])
