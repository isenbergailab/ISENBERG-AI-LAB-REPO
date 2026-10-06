import json
import os
import sys
import time
from datetime import date, datetime
from unittest import mock

from helpers import TODAY, VaultCase, make_pdf, needs_pypdf
from gtd_agent.documents import verify_date
from gtd_agent.housekeeping import Housekeeper

SYLLABUS = """# ACCT 423 Syllabus
Homework 1 due Sep 15.
Homework #3 due Oct 14.
Midterm exam: Tuesday, October 20
Final paper due 12/10.
Guest talk Week 5 Thursday.
Sign up for a presentation slot in the first two weeks.
"""


def deadline(title, day, evidence, kind="assignment", context="#computer"):
    return {"title": title, "date": day, "date_evidence": evidence, "kind": kind, "context": context}


ANSWER = {
    "doc_type": "syllabus", "title": "ACCT 423 syllabus, Fall 2026", "course": "ACCT 423",
    "target_key": "DEMO_PROJECT", "reference_folder": "", "summary": "Four graded items and one midterm.",
    "facts": ["Midterm is 30% of the grade"],
    "deadlines": [deadline("Homework 1", "2026-09-15", "Sep 15"),
                  deadline("Homework #3", "2026-10-14", "Oct 14"),
                  deadline("Midterm exam", "2026-10-20", "Tuesday, October 20", "exam", "#anywhere"),
                  deadline("Final paper", "2026-12-10", "12/10"),
                  deadline("Guest talk reflection", "2026-10-01", "Week 5 Thursday"),
                  deadline("Invented quiz", "2026-11-01", "Nov 1", "exam")],
    "actions": [{"title": "Sign up for a presentation slot", "context": "#computer", "due": "",
                 "evidence": "Sign up for a presentation slot"},
                {"title": "Email the dean", "context": "#computer", "due": "", "evidence": "the dean said"}],
    "explanation": "A course syllabus with dated work."}


class DateCheckTest(VaultCase):
    def test_verify_date(self):
        self.assertTrue(verify_date("Oct 14", date(2026, 10, 14), TODAY))
        self.assertTrue(verify_date("Tuesday, October 20", date(2026, 10, 20), TODAY))
        self.assertTrue(verify_date("Feb 3", date(2027, 2, 3), TODAY))  # year inferred across January
        self.assertFalse(verify_date("Oct 14", date(2026, 10, 15), TODAY))
        self.assertFalse(verify_date("Week 5 Thursday", date(2026, 10, 1), TODAY))


class DocumentCase(VaultCase):
    remote = True

    def drop(self, name, text, age=600):
        self.write(f"00_INBOX/{name}", text)
        stamp = time.time() - age
        os.utime(self.path(f"00_INBOX/{name}"), (stamp, stamp))

    def run_housekeeping(self):
        Housekeeper(self.agent).run(now=datetime.now())

    def block(self, proposal_id):
        text = self.read("_agent/APPROVAL.md")
        start = text.index(f"<!-- gtd-agent:proposal id={proposal_id} -->")
        return text, start, text.index("<!-- gtd-agent:item-end -->", start)


class SyllabusTest(DocumentCase):
    def test_syllabus_items_selection_and_undo(self):
        self.provider.documents.append(json.loads(json.dumps(ANSWER)))
        self.drop("acct 423 syllabus.md", SYLLABUS)
        self.run_housekeeping()
        rows = self.pending("document")
        self.assertEqual(len(rows), 1)
        proposal = json.loads(rows[0]["proposal_json"])
        labels = [item["label"] for item in proposal["items"]]
        self.assertEqual(len(labels), 5, labels)
        self.assertIn("check the date", labels[0])            # Week 5 Thursday
        self.assertIn("Homework No. 3", labels[1])
        self.assertIn("Study for Midterm exam", labels[2])
        self.assertIn("shows from Fri Oct 16", labels[2])       # exams: 4-day lead
        self.assertIn("shows from Sun Oct 11", labels[1])       # coursework: 3-day lead
        self.assertIn("Skipped 1 date already past", proposal["reason"])
        self.assertIn("Dropped 2 items", proposal["reason"])
        self.assertEqual(len([c for c in self.provider.calls if c[0] == "document"]), 1)

        text, start, end = self.block(rows[0]["id"])
        block = text[start:end].replace("- [x] 1. ", "- [ ] 1. ", 1).replace("- [ ] Approve", "- [x] Approve", 1)
        self.write("_agent/APPROVAL.md", text[:start] + block + text[end:])
        outcomes = self.agent.sync_review_requests()
        self.assertTrue(any("Applied document" in o for o in outcomes), outcomes)

        project = self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md")
        self.assertIn("## Deadlines", project)
        self.assertLess(project.index("## Steps"), project.index("## Deadlines"))
        self.assertLess(project.index("## Deadlines"), project.index("## Waiting For"))
        self.assertIn("Homework No. 3 #computer #next 🛫 2026-10-11 📅 2026-10-14", project)
        self.assertIn("Study for Midterm exam #anywhere #next 🛫 2026-10-16 📅 2026-10-20", project)
        self.assertNotIn("Guest talk", project)
        self.assertIn("- [ ] Sign up for a presentation slot #computer", project)
        self.assertNotIn("Sign up for a presentation slot #computer #next", project)  # queued behind the #next step
        self.assertIn("Filed [[ACCT_423_SYLLABUS]] from the Inbox.", project)
        reference = self.read("04_REFERENCE/COURSES/ACCT_423_SYLLABUS.md")
        self.assertIn('project: ["[[DEMO_PROJECT]]"]', reference)
        self.assertIn("> [!summary]", reference)
        self.assertIn("Midterm is 30% of the grade", reference)
        self.assertIn("Homework #3 due Oct 14.", reference)
        self.assertFalse(self.path("00_INBOX/acct 423 syllabus.md").exists())

        undo_id = self.agent.ledger.list_undo()[0]["id"]
        self.agent.undo(undo_id)
        self.assertEqual(self.read("00_INBOX/acct 423 syllabus.md"), SYLLABUS)
        self.assertFalse(self.path("04_REFERENCE/COURSES/ACCT_423_SYLLABUS.md").exists())
        self.assertNotIn("## Deadlines", self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"))

    def test_new_file_waits_and_changed_file_is_reread(self):
        self.drop("fresh.md", SYLLABUS, age=5)
        self.run_housekeeping()
        self.assertEqual(self.pending("document"), [])
        self.assertEqual([name for name, _ in self.provider.calls if name == "document"], [])


class PrivateDocumentTest(DocumentCase):
    def test_a_restricted_mention_is_ordinary_text_and_goes_to_the_model(self):
        self.provider.documents.append({
            "doc_type": "notes", "title": "ExampleCorp recruiting call", "course": "", "target_key": "DEMO_PROJECT",
            "reference_folder": "", "summary": "Start date questions.", "facts": [], "deadlines": [],
            "actions": [{"title": "Email the recruiter about start dates", "context": "#computer", "due": "",
                         "evidence": "Call with ExampleCorp recruiting about start dates"}],
            "explanation": "Notes from a recruiting call."})
        self.drop("recruiting.md", "Call with ExampleCorp recruiting about start dates.\n")
        self.run_housekeeping()
        sent = [c[1] for c in self.provider.calls if c[0] == "document"]
        self.assertEqual(len(sent), 1)
        self.assertIn("ExampleCorp recruiting", sent[0]["text"])
        proposal = json.loads(self.pending("document")[0]["proposal_json"])
        self.assertNotIn("Private", proposal["reason"])
        self.assertEqual(len(proposal["items"]), 1)

    def test_private_file_never_reaches_a_model_and_can_be_filed_as_is(self):
        self.drop("notes.md", "---\nproject: \"[[DEMO_PROJECT]]\"\nprivate: true\n---\nCall with the clinic about forms.\n")
        self.run_housekeeping()
        self.assertFalse([c for c in self.provider.calls if c[0] == "document"])
        row = self.pending("document")[0]
        proposal = json.loads(row["proposal_json"])
        self.assertIn("marked private", proposal["reason"])
        self.tick("Approve", row["id"])
        self.agent.sync_review_requests()
        reference = self.read("04_REFERENCE/DOCUMENTS/NOTES.md")
        self.assertIn('project: ["[[DEMO_PROJECT]]"]', reference)
        self.assertEqual(reference.count("project:"), 1)
        self.assertIn("private: true", reference)
        self.assertFalse(self.path("00_INBOX/notes.md").exists())

    def test_secret_is_never_sent(self):
        self.drop("zoom.md", "Meeting notes\npassword: hunter22\n")
        self.run_housekeeping()
        row = self.pending("document")[0]
        self.assertIn("password or key", json.loads(row["proposal_json"])["reason"])
        self.assertFalse([c for c in self.provider.calls if c[0] == "document"])


STATEMENT = ["Valley Credit Union", "Account statement", "Account number 4400 1234 1234 1234",
             "Available balance $1,204.17"]


class PdfDocumentTest(DocumentCase):
    """A PDF in the Inbox folder is read locally with pypdf and screened; on approval it moves to Reference beside
    a note that embeds it."""

    def drop_pdf(self, name: str, lines: list[str], age: int = 600) -> bytes:
        path = self.path(f"00_INBOX/{name}")
        path.write_bytes(make_pdf(lines))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path.read_bytes()

    def document_calls(self) -> list[dict]:
        return [call for name, call in self.provider.calls if name == "document"]

    @needs_pypdf
    def test_a_pdf_syllabus_gives_deadlines_then_moves_to_reference(self):
        data = self.drop_pdf("ACCT 423 Syllabus.pdf", ["ACCT 423 Syllabus", "Homework #3 due Oct 14."])
        self.provider.documents.append(dict(json.loads(json.dumps(ANSWER)), actions=[],
                                            deadlines=[deadline("Homework #3", "2026-10-14", "Oct 14")]))
        self.run_housekeeping()
        call = self.document_calls()[0]
        self.assertEqual(call["filename"], "ACCT 423 Syllabus.pdf")
        self.assertIn("Homework #3 due Oct 14.", call["text"])
        self.tick("Approve", self.pending("document")[0]["id"])
        outcomes = self.agent.sync_review_requests()
        self.assertTrue(any("Applied document" in o for o in outcomes), outcomes)
        self.assertIn("Homework No. 3 #computer #next 🛫 2026-10-11 📅 2026-10-14",
                      self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"))
        self.assertFalse(self.path("00_INBOX/ACCT 423 Syllabus.pdf").exists())
        self.assertEqual(self.path("04_REFERENCE/COURSES/ACCT_423_SYLLABUS.pdf").read_bytes(), data)
        note = self.read("04_REFERENCE/COURSES/ACCT_423_SYLLABUS.md")
        self.assertIn("![[ACCT_423_SYLLABUS.pdf]]", note)
        self.assertIn("> [!summary]", note)

        self.agent.undo(self.agent.ledger.list_undo()[0]["id"])
        self.assertEqual(self.path("00_INBOX/ACCT 423 Syllabus.pdf").read_bytes(), data)
        self.assertFalse(self.path("04_REFERENCE/COURSES/ACCT_423_SYLLABUS.pdf").exists())
        self.assertFalse(self.path("04_REFERENCE/COURSES/ACCT_423_SYLLABUS.md").exists())

    @needs_pypdf
    def test_a_bank_statement_pdf_never_reaches_a_model_and_can_be_filed_as_is(self):
        data = self.drop_pdf("scan.pdf", STATEMENT)
        self.run_housekeeping()
        self.assertEqual(self.document_calls(), [])
        row = self.pending("document")[0]
        self.assertIn("The sensitive screen flagged it (money)", json.loads(row["proposal_json"])["reason"])
        self.tick("File as is", row["id"])
        self.agent.sync_review_requests()
        self.assertEqual(self.path("04_REFERENCE/DOCUMENTS/SCAN.pdf").read_bytes(), data)
        note = self.read("04_REFERENCE/DOCUMENTS/SCAN.md")
        self.assertIn("private: true", note)
        self.assertIn("![[SCAN.pdf]]", note)
        for path in self.base.rglob("*.md"):
            self.assertNotIn("1,204.17", path.read_text(encoding="utf-8"), f"the statement left a trace in {path}")

    @needs_pypdf
    def test_a_flagged_pdf_you_clear_goes_to_the_model(self):
        self.drop_pdf("tax course.pdf", ["ACCT 551 Federal Tax", "Tax return project due Nov 3."])
        self.run_housekeeping()
        row = self.pending("document")[0]
        self.assertIn("flagged it (government)", json.loads(row["proposal_json"])["reason"])
        self.assertEqual(self.document_calls(), [])
        self.tick("Read it with the model", row["id"])
        self.agent.sync_review_requests()
        self.provider.documents.append(dict(json.loads(json.dumps(ANSWER)), actions=[],
                                            deadlines=[deadline("Tax return project", "2026-11-03", "Nov 3")]))
        self.run_housekeeping()
        self.assertEqual(len(self.document_calls()), 1)
        self.assertTrue(any("Tax return project" in r["proposal_json"] for r in self.pending("document")))

    def test_without_pypdf_a_pdf_waits_and_says_how_to_install(self):
        self.drop_pdf("ACCT 423 Syllabus.pdf", ["Homework #3 due Oct 14."])
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            self.run_housekeeping()
        self.assertEqual(self.document_calls(), [])
        self.assertIn("py -m pip install pypdf", self.pending("document")[0]["proposal_json"])


class OffDocumentTest(DocumentCase):
    remote = False

    def test_remote_off_waits(self):
        self.drop("session.md", "Kellogg info session notes.\n")
        self.run_housekeeping()
        row = self.pending("document")[0]
        self.assertIn("Remote inference is off", json.loads(row["proposal_json"])["reason"])
        self.assertNotIn("- [ ] Approve", self.block(row["id"])[0][self.block(row["id"])[1]:self.block(row["id"])[2]])


BRIEF = """---
new_project: Calendar and Gmail
area: "[[AREA_CLUB]]"
start: 2026-10-14
outcome: The GTD agent reads my calendar and labeled Gmail
---
# Calendar and Gmail integrations

## Steps
- [ ] Install Radicale on the Acer with pip
- [ ] Create a GTD label in Gmail
"""


class ProjectBriefTest(DocumentCase):
    def test_brief_creates_project_with_held_steps(self):
        self.provider.documents.append({
            "doc_type": "notes", "title": "Calendar and Gmail integrations", "course": "", "target_key": "",
            "reference_folder": "", "summary": "Plan to connect a calendar and Gmail.", "facts": [], "deadlines": [],
            "actions": [{"title": "Install Radicale on the Acer with pip", "context": "#computer", "due": "",
                         "evidence": "Install Radicale on the Acer with pip"},
                        {"title": "Create a GTD label in Gmail", "context": "#computer", "due": "",
                         "evidence": "Create a GTD label in Gmail"}],
            "explanation": "A project brief."})
        self.drop("calendar brief.md", BRIEF)
        self.run_housekeeping()
        row = self.pending("document")[0]
        proposal = json.loads(row["proposal_json"])
        self.assertEqual(proposal["title"], "New project CALENDAR_AND_GMAIL")
        self.assertIn("shows from Wed Oct 14", proposal["items"][0]["label"])
        self.assertEqual(self.provider.calls[-1][1]["fixed_target"], "AREA_CLUB")
        self.tick("Approve", row["id"])
        self.agent.sync_review_requests()
        note = self.read("02_PROJECTS/CALENDAR_AND_GMAIL/CALENDAR_AND_GMAIL.md")
        self.assertIn("status: active", note)
        self.assertIn('area: "[[AREA_CLUB]]"', note)
        self.assertIn("The GTD agent reads my calendar and labeled Gmail", note)
        self.assertIn("- [ ] Install Radicale on the Acer with pip #computer #next 🛫 2026-10-14 ➕ 2026-09-28", note)
        self.assertIn("- [ ] Create a GTD label in Gmail #computer 🛫 2026-10-14 ➕ 2026-09-28", note)
        self.assertLess(note.index("Install Radicale"), note.index("Create a GTD label"))
        self.assertNotIn("_No steps yet", note)
        reference = self.read("04_REFERENCE/DOCUMENTS/CALENDAR_BRIEF.md")
        self.assertIn('project: ["[[CALENDAR_AND_GMAIL]]"]', reference)
        self.assertNotIn("new_project", reference.split("---")[1])  # kept keys only
        self.assertFalse(self.path("00_INBOX/calendar brief.md").exists())

    def test_existing_project_name_is_refused_before_any_model_call(self):
        self.drop("brief.md", "---\nnew_project: Demo Project\n---\n# Brief\n- [ ] Do a thing\n")
        self.run_housekeeping()
        row = self.pending("document")[0]
        self.assertIn("already exists", json.loads(row["proposal_json"])["reason"])
        self.assertFalse([c for c in self.provider.calls if c[0] == "document"])
