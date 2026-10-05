"""Synthetic vault and fake model provider shared by the tests. No real vault is read."""
from __future__ import annotations

import os
import logging
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gtd_agent.clarification import blank_draft  # noqa: E402
from gtd_agent.core import Settings  # noqa: E402
from gtd_agent.pdf import version as pdf_version  # noqa: E402
from gtd_agent.workflow import Agent  # noqa: E402

TODAY = date(2026, 9, 28)

FILES = {
    "00_INBOX/INBOX.md": "%% Capture one thing per line. %%\n",
    "01_GTD/SINGLE_ACTIONS.md": textwrap.dedent("""\
        # SINGLE_ACTIONS

        ## @computer
        - [ ] Renew library card #computer

        ## @calls
        - [ ] Call the dentist [[AREA_ADMIN]] #calls

        ## @anywhere

        ## @errands

        ## Waiting For
        - [ ] #waiting Riley: signed form [[AREA_ADMIN]] ➕ 2026-09-15 📅 2026-09-25
        - [ ] #waiting Pat: slides ➕ 2026-09-27 📅 2026-10-04
        """),
    "01_GTD/SOMEDAY_MAYBE.md": "# SOMEDAY_MAYBE\n\n## Ideas\n- Learn to juggle\n",
    "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md": textwrap.dedent("""\
        ---
        key: DEMO_PROJECT
        type: project
        status: active
        area: "[[AREA_CLUB]]"
        due: 2026-12-01
        priority: high
        aliases: [Demo Project, The Demo]
        review:
        private: false
        ---
        # DEMO_PROJECT

        ## Outcome
        Launch the lab.

        ## Steps
        - [ ] Email the advisor #computer #next
        - [ ] Book the room #calls
        - [ ] Plan first meeting #computer

        ### Done
        - [x] Pick a name ✅ 2026-09-20

        ## Waiting For
        - [ ] #waiting Admin office: room approval ➕ 2026-09-20 📅 2026-09-26

        ## Log
        - 2026-09-20 Named the lab.

        ## Links
        """),
    "02_PROJECTS/TAXES/TAXES.md": textwrap.dedent("""\
        ---
        key: TAXES
        type: project
        status: active
        area: "[[AREA_ADMIN]]"
        due:
        priority:
        aliases: [Taxes]
        review:
        private: false
        ---
        # TAXES

        ## Outcome
        Filed.

        ## Steps
        - [ ] Download 1099 forms #computer #next

        ## Log
        """),
    "02_PROJECTS/LATIN/LATIN.md": textwrap.dedent("""\
        ---
        key: LATIN
        type: project
        status: someday
        area: "[[AREA_CLUB]]"
        aliases: [Latin]
        ---
        # LATIN

        ## Steps
        - [ ] Buy the textbook #errands
        """),
    "02_PROJECTS/PROJECTS_INDEX.md": "# PROJECTS_INDEX\n",
    "03_AREAS/AREA_CLUB.md": "---\nkey: AREA_CLUB\ntype: area\naliases: [Club]\nprivate: false\n---\n# AREA_CLUB\n",
    "03_AREAS/AREA_ADMIN.md": "---\nkey: AREA_ADMIN\ntype: area\naliases: [Admin, Personal Admin]\nprivate: true\n---\n# AREA_ADMIN\n",
    "04_REFERENCE/SYSTEM/TEMPLATES/WEEKLY_REVIEW_TEMPLATE.md": "## Get clear\n- [ ] Empty the inbox\n",
    "04_REFERENCE/NOTES/GUIDE.md": "# GUIDE\nSee [[DEMO_PROJECT]] and `[[NOT_A_LINK]]`.\n",
}


needs_pypdf = unittest.skipUnless(pdf_version(), "pypdf is not installed (py -m pip install pypdf)")


def make_pdf(lines: list[str]) -> bytes:
    """A one-page PDF holding these lines as text, built by hand: synthetic, for the PDF tests."""
    def escaped(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 12 Tf 72 720 Td 16 TL " + " ".join(f"({escaped(line)}) Tj T*" for line in lines) + " ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               "/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return out


def make_vault(root: Path, extra: dict[str, str] | None = None) -> None:
    for rel, text in {**FILES, **(extra or {})}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_settings(base: Path, **overrides: Any) -> Settings:
    lines = ["[vault]", f'root = "{(base / "vault").as_posix()}"', "[agent]", 'mode = "approval"',
             f'state_dir = "{(base / "state").as_posix()}"', "poll_seconds = 3", "scan_interval_seconds = 5",
             f"auto_promote_next = {'true' if overrides.pop('auto_promote_next', True) else 'false'}",
             f"edit_quiet_minutes = {overrides.pop('quiet', 0)}",
             f"inbox_quiet_seconds = {overrides.pop('inbox_quiet', 0)}",
             "[privacy]", f"remote_inference = {'true' if overrides.pop('remote', False) else 'false'}",
             overrides.pop("extra", "")]
    config = base / "config.toml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Settings.load(config)


class FakeProvider:
    """Records calls; answers with queued interpretations."""

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.interpretations: list[Any] = []
        self.splits: list[list[str]] = []
        self.vague: list[dict] = []
        self.topics: list[dict] = []
        self.documents: list[Any] = []
        self.decided_answers: list[Any] = []
        self.outcome_answers: list[Any] = []
        self.after_event_answers: list[Any] = []
        self.calls: list[tuple[str, Any]] = []
        self.mails: list[Any] = []  # answers for mail(); a callable gets the call
        self.drafts: list[Any] = []  # answers for draft(), in order
        self.draft_by_kind: dict[str, Any] = {}  # or one answer per kind (reply, follow_up)

    def available(self) -> bool:
        return True

    def decide(self, text, examples=None):
        self.calls.append(("decide", text))
        if self.decisions:
            return self.decisions.pop(0)
        return {"operation": ("new_capture", 0.9), "category": ("next_action", 0.9), "multiplicity": ("single", 0.9)}

    def interpret(self, text, issue, projects, areas, items, examples, teacher_feedback=None, previous=None, **kw):
        self.calls.append(("interpret", {"text": text, "projects": projects, "areas": areas, "items": items,
                                         "feedback": teacher_feedback, **kw}))
        answer = self.interpretations.pop(0)
        return answer(text, projects, areas, items) if callable(answer) else answer

    def split(self, text, routing=None):
        self.calls.append(("split", text))
        return self.splits.pop(0)

    def document(self, text, filename, document_date, projects, areas, folders, fixed_target=""):
        self.calls.append(("document", {"text": text, "filename": filename, "projects": projects, "areas": areas,
                                        "folders": folders, "fixed_target": fixed_target}))
        return self.documents.pop(0)

    def mail(self, mail, mail_date, projects, areas, waits):
        call = {"mail": mail, "mail_date": mail_date, "projects": projects, "areas": areas, "open_waits": waits}
        self.calls.append(("mail", call))
        answer = self.mails.pop(0) if self.mails else {
            "target_key": "", "summary": "", "actions": [], "events": [], "waits": [], "answers": [],
            "explanation": "Nothing to act on."}
        return answer(call) if callable(answer) else answer

    def draft(self, kind, context, voice):
        self.calls.append(("draft", {"kind": kind, "context": context, "voice": voice}))
        if kind in self.draft_by_kind:
            return self.draft_by_kind[kind]
        if self.drafts:
            return self.drafts.pop(0)
        subject = f"Re: {context.get('their_subject', '')}" if kind == "reply" else f"Following up: {context.get('what', '')}"
        return {"subject": subject, "body": f"Hi {context.get('to_name') or 'there'},\n\nThank you!\n\nTaylor"}

    def decided(self, capture, project, questions, sections, reference_date):
        self.calls.append(("decided", {"capture": capture, "project": project, "questions": questions,
                                       "sections": sections}))
        return self.decided_answers.pop(0)

    def after_event(self, project, event, outcome, steps, reference_date):
        self.calls.append(("after_event", {"project": project, "event": event, "outcome": outcome, "steps": steps}))
        return self.after_event_answers.pop(0) if self.after_event_answers else {"steps": [], "explanation": ""}

    def outcome_check(self, project, old, new, steps, reference_date):
        self.calls.append(("outcome", {"project": project, "old": old, "new": new, "steps": steps}))
        answer = self.outcome_answers.pop(0)
        return answer(steps) if callable(answer) else answer

    def vague_check(self, actions):
        self.calls.append(("vague", actions))
        return self.vague

    def someday_topics(self, lines):
        self.calls.append(("someday", lines))
        return self.topics


def draft(**values: str) -> dict[str, str]:
    base = {"explanation": "Likely intent.", "lesson": "A reusable distinction.", "title": "Do the thing",
            "context": "#computer"}
    base.update(values)
    return blank_draft(**base)


class VaultCase(unittest.TestCase):
    remote = False
    auto_promote = True
    quiet = 0  # minutes a note must sit untouched before LINT acts on it (config default: 10)
    inbox_quiet = 0  # seconds the Inbox must sit untouched before its lines are read (config default: 30)
    base_subdir = ""  # folders between the temp dir and the vault, e.g. a sync tool's path with spaces

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(logging.shutdown)
        self.base = Path(temp.name) / self.base_subdir
        self.base.mkdir(parents=True, exist_ok=True)
        make_vault(self.base / "vault", self.extra_files())
        self.settings = make_settings(self.base, remote=self.remote, auto_promote_next=self.auto_promote,
                                      quiet=self.quiet, inbox_quiet=self.inbox_quiet,
                                      extra=self.extra_config())
        self.provider = FakeProvider()
        self.agent = Agent(self.settings, self.provider, today=TODAY)
        self.said: list[str] = []
        self.agent.health.out = self.said.append  # lines the agent would print, kept for assertions
        self.addCleanup(self.agent.close)

    def extra_files(self) -> dict[str, str]:
        return {}

    def extra_config(self) -> str:
        """More config.toml tables for this test case."""
        return ""

    def path(self, rel: str) -> Path:
        return self.base / "vault" / rel

    def read(self, rel: str) -> str:
        return self.path(rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        self.path(rel).write_text(text, encoding="utf-8")

    def add_inbox(self, *lines: str) -> None:
        self.write("00_INBOX/INBOX.md", self.read("00_INBOX/INBOX.md") + "".join(line + "\n" for line in lines))

    def inbox_untouched(self, seconds: float) -> None:
        """Date the Inbox as if you last changed it this many seconds ago (negative: a time still to come)."""
        when = time.time() - seconds
        os.utime(self.path("00_INBOX/INBOX.md"), (when, when))

    def tick(self, label: str = "Approve", proposal_id: str | None = None, suffix: str = "") -> None:
        note = "_agent/LOG.md" if label == "Undo" else "_agent/APPROVAL.md"  # Undo boxes live in LOG
        text = self.read(note)
        if proposal_id:
            marker = f"<!-- gtd-agent:proposal id={proposal_id} -->"
            start = text.index(marker)
            end = text.index("<!-- gtd-agent:item-end -->", start)
            block = text[start:end].replace(f"- [ ] {label}", f"- [x] {label}{suffix}", 1)
            text = text[:start] + block + text[end:]
        else:
            text = text.replace(f"- [ ] {label}", f"- [x] {label}{suffix}", 1)
        self.write(note, text)

    def pending(self, origin: str | None = None) -> list:
        return [row for row in self.agent.ledger.pending() if origin is None or row["origin"] == origin]
