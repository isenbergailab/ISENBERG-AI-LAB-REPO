"""Deterministic upkeep: next-step promotion, follow-ups, vault check, daily digest, weekly review."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import radicale
from .calendar_ics import Event, fetch_ics, free_gaps, parse_events, rrule_from_tasks
from .clarification import CONTEXT_HEADING, ROUTE_LABEL, compile_ops, new_marker, project_note_text
from .core import CONTEXTS, atomic_replace, digest, parse_captures, read_snapshot, write_owned
from .credentials import get_secret
from .dates import find_time
from .decisions import decisions_note, open_questions
from .markdown import parse_frontmatter, split_frontmatter
from .providers import ProviderError
from .ops import OpError
from .tasks import (TAG_RE, Task, clean_text, format_task, text_before_dates, with_date, with_dependency, with_id,
                    without_dependency)
from .vault import Project, Vault, project_key
from .workflow import PROPOSAL_VERSION, Agent, _review_text
from .documents import DocumentIntake
from .drafts import address as mail_address, draft_op, write_draft
from .mail import MailIntake, contact_notes, mail_today
from .watch import Watcher
from .current_state import NEXT_EVENT, PLACEHOLDER, SECTION, events_by_project, next_events, section_body, state_text

FOLLOW_UP_LIMIT = 3  # unanswered follow-ups before TODAY flags the wait instead of proposing another
AFTER_DAYS = 7       # how far back an ended event still gets its question
UPCOMING_DAYS = 60   # how far ahead Current state looks for a project's next event
AUTO_BEGIN = "<!-- gtd-agent:auto-begin -->"
AUTO_END = "<!-- gtd-agent:auto-end"


def block_sha(inner: str) -> str:
    """Short fingerprint of the agent block, stored in its end marker so any agent copy can tell if you edited it."""
    return digest(inner.rstrip("\n"))[:12]


AUTO_END_RE = re.compile(r"<!-- gtd-agent:auto-end(?: sha=([0-9a-f]{12}))? -->")
STRAY_SUFFIXES = {".zip", ".tmp", ".bak", ".crdownload", ".part"}
CHECK_REFRESH_MINUTES = 15  # TODAY's "Checked" time moves at least this often while the agent runs
RECOVERED_SHOWN_HOURS = 24


def _fmt_day(day: date) -> str:
    return f"{day.strftime('%a')} {day.strftime('%b')} {day.day}"


def _link_text(value: Any) -> str:
    """Text safe inside a wikilink's display part."""
    text = _review_text(value).replace("[[", "").replace("]]", "")
    return text.replace("|", "/").replace("[", "(").replace("]", ")")


def _fmt_when(moment: datetime, now: datetime) -> str:
    return f"{moment:%H:%M}" if moment.date() == now.date() else f"{_fmt_day(moment.date())} {moment:%H:%M}"


def not_in_views(settings: Any) -> str:
    """One Tasks filter that keeps the Inbox, agent notes, reference, archive and review notes out of a view.
    A regex anchored at the start: 'path does not include _agent' would also hide GTD_AGENT (Tasks matches
    case-insensitively)."""
    folders = [settings.inbox_dir, settings.agent_dir, settings.reference_dir, settings.archive_dir, settings.reviews_dir]
    names = "|".join(re.escape(folder).replace("/", "\\/") for folder in folders if folder)
    return f"path regex does not match /^({names})\\//"


APPROVAL_SHOWN = 12


class Housekeeper:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.settings = agent.settings
        self.ledger = agent.ledger
        self._near: tuple[list[Event], str | None] | None = None  # calendar events around now, read once per run

    # ------------------------------------------------------------ helpers
    def _where(self, vault: Vault, task: Task) -> str:
        project = vault.project_for_path(task.path)
        if project is not None:
            return f"[[{project.key}]]"
        area = vault.area_for_path(task.path)
        if area is not None:
            return f"[[{area.key}]]"
        linked = next((link for link in task.links if link in vault.areas), None)
        if linked:
            return f"[[{linked}]]"
        return "[[SINGLE_ACTIONS]]"

    def _task_line(self, vault: Vault, task: Task, extra: str = "") -> str:
        title = task.title
        for link in task.links:
            if link in vault.areas:
                title = title.replace(link, "").strip()
        title = re.sub(r"\s+", " ", title).strip() or task.description
        return f"- {_review_text(title)}{extra} · {self._where(vault, task)}"

    def _propose(self, origin: str, key: str, proposal: dict[str, Any]) -> bool:
        if self.ledger.get_by_key(key) is not None:
            return False
        proposal["engine_version"] = PROPOSAL_VERSION
        if self.settings.mode == "approval":
            self.ledger.upsert(origin, key, proposal)
        return True

    def _expire(self, origin: str, valid: set[str]) -> None:
        for row in self.ledger.list("pending", origin):
            if row["dedupe_key"] not in valid:
                self.ledger.set_status(row["id"], "stale")

    # ------------------------------------------------------------ task history
    def track_tasks(self, vault: Vault) -> None:
        today = self.agent.today().isoformat()
        seen = self.ledger.seen_rows()
        updates = []
        for task in vault.tasks:
            key = digest(f"{task.path}\n{task.norm}")[:20]
            row = seen.get(key)
            if row is None:
                updates.append((key, task.path, task.norm[:300], today, today if task.is_open else None, None))
            elif task.is_done and row["first_open"] and not row["done_seen"]:
                updates.append((key, task.path, task.norm[:300], row["first_seen"], None, today))
            elif task.is_open and not row["first_open"]:
                updates.append((key, task.path, task.norm[:300], row["first_seen"], today, None))
        if updates:
            self.ledger.record_seen(updates)

    def first_seen(self, task: Task) -> date | None:
        if task.created:
            return task.created
        row = self.ledger.seen_rows().get(digest(f"{task.path}\n{task.norm}")[:20])
        return date.fromisoformat(row["first_seen"]) if row else None

    # ------------------------------------------------------------ promotion
    def promote(self, vault: Vault) -> list[str]:
        previous = self.ledger.next_state()
        state: dict[str, list[str]] = {}
        messages: list[str] = []
        for project in vault.projects.values():
            if project.archived:
                continue
            open_next = [task.norm for task in vault.open_next(project)]
            state[project.key] = open_next
            before = previous.get(project.key)
            if before is None or open_next or not before or project.status != "active":
                continue
            done = {task.norm for task in project.tasks if task.is_done or task.is_cancelled}
            if not any(norm in done for norm in before):
                continue  # the #next step was deleted or reworded, not finished or dropped
            step = vault.first_open_step(project)
            if step is None:
                messages.append(f"{project.key}: last step done. Add a step or set the status to done.")
                continue
            op = {"op": "add_tag", "path": project.rel, "match": step.raw, "tag": "#next"}
            if self.settings.mode != "approval":
                messages.append(f"{project.key}: would promote '{step.title}' (dry run)")
                continue
            if self.settings.auto_promote_next:
                try:
                    self.agent.apply_ops([op], "auto", f"promote-{project.key}-{new_marker()}",
                                         f"Promoted next step in {project.key}: {step.title}")
                except Exception as exc:  # a locked file retries on the next scan
                    messages.append(f"CHECK: {project.key}: could not promote the next step ({exc})")
                    state[project.key] = before
                    continue
                self.agent.log(f"Auto · {project.key}: next step is now '{step.title}'")
                state[project.key] = [step.norm]
                messages.append(f"{project.key}: next step promoted")
            else:
                key = f"promote|{project.key}|{step.norm}"
                self._propose("promotion", key, {
                    "kind": "promotion", "title": step.title, "summary": f"Tag #next in {project.key} › Steps",
                    "reason": "You ticked the last #next step. This is the first open step.", "ops": [op]})
        self.ledger.save_next_state(state)
        return messages

    # ------------------------------------------------------------ tidy finished steps
    def tidy(self, vault: Vault) -> list[str]:
        """Move steps ticked before today under '### Done'. Any order; nothing is deleted. Logged and undoable."""
        if not self.settings.tidy_done_steps:
            return []
        today = self.agent.today()
        if self.ledger.kv_get("tidy_failed") == today.isoformat():
            return []  # tried and failed today; try again tomorrow instead of every scan
        seen = self.ledger.seen_rows()
        targets = [(p.rel, section, p.key, p.tasks) for p in vault.projects.values() if not p.archived
                   for section in ("Steps", "Deadlines")]
        targets += [(a.rel, section, a.key, a.tasks) for a in vault.areas.values()
                    for section in ("Recurring", "Deadlines")]
        targets += [(self.settings.single_actions, "Deadlines", "SINGLE_ACTIONS",
                     [t for t in vault.tasks if t.path == self.settings.single_actions])]
        ops: list[dict[str, Any]] = []
        labels: list[str] = []
        for rel, section, label, tasks in targets:
            moved = 0
            for task in tasks:
                if task.section.casefold() != section.casefold() or task.heading.casefold() == "done" or task.indent:
                    continue
                if not (task.is_done or task.is_cancelled):
                    continue
                finished = task.done or task.dates.get("cancelled")
                if finished is None:
                    row = seen.get(digest(f"{task.path}\n{task.norm}")[:20])
                    finished = date.fromisoformat(row["done_seen"]) if row and row["done_seen"] else None
                if finished is None or finished >= today:
                    continue
                note_lines = vault.notes[rel].text.split("\n") if rel in vault.notes else []
                following = note_lines[task.line_no] if task.line_no < len(note_lines) else ""
                if following.startswith((" ", "\t")) and following.strip():
                    continue  # nested sub-steps: leave for you to move
                ops.append({"op": "move_to_done", "path": rel, "section": section, "match": task.raw})
                moved += 1
            if moved and label not in labels:
                labels.append(label)
        if not ops:
            return []
        summary = f"Moved {len(ops)} finished step{'s' if len(ops) != 1 else ''} under Done in {', '.join(labels)}"
        if self.settings.mode != "approval":
            return [f"{summary} (dry run)"]
        try:
            self.agent.apply_ops(ops, "auto", f"tidy-{new_marker()}", summary)
        except Exception as exc:  # a locked or edited file: report once, retry tomorrow
            self.ledger.kv_set("tidy_failed", today.isoformat())
            return [f"CHECK: could not tidy finished steps ({exc}). Will retry tomorrow."]
        self.agent.log(f"Auto · {summary}")
        return [summary]

    # ------------------------------------------------------------ follow-ups
    def _contact_email(self, vault: Vault, person: str) -> str:
        """The first address of the contact this person's name belongs to, or ''."""
        folded = person.casefold().strip()
        for contact in contact_notes(vault, self.settings):
            names = [name.casefold() for name in contact["names"]]
            if folded and contact["emails"] and (folded in names or folded in {n.split()[0] for n in names if n}):
                return contact["emails"][0]
        return ""

    def _follow_key(self, task: Task) -> str:
        return f"followups|{digest(task.path + chr(10) + task.norm)[:20]}"

    def follow_count(self, task: Task) -> int:
        """How many times the user has followed up on this wait (each tick of its follow-up action)."""
        return int(self.ledger.kv_get(self._follow_key(task)) or 0)

    def followups(self, vault: Vault) -> list[str]:
        """A wait whose follow-up date arrived gets a follow-up action it waits on (⛔ its 🆔). The wait moves 7 days
        when you tick that action (settle_waits), and after FOLLOW_UP_LIMIT unanswered ones TODAY flags it."""
        today = self.agent.today()
        open_ids = {t.task_id for t in vault.tasks if t.task_id and t.is_open}
        valid: set[str] = set()
        created = 0
        for task in vault.waiting():
            if task.due is None or task.due > today or task.has("#replied"):
                continue  # a replied wait waits on your reply, not on them
            if any(task_id in open_ids for task_id in task.depends_on) or self.follow_count(task) >= FOLLOW_UP_LIMIT:
                continue  # a follow-up is already on its way, or TODAY flags the wait
            project = vault.project_for_path(task.path)
            if project is not None and project.status in {"someday", "done", "dropped"}:
                continue
            key = f"followup|{task.path}|{task.norm}|{task.due.isoformat()}"
            valid.add(key)
            person, what = task.waiting_parts()
            text = f"Follow up with {person} re: {what}" if person else f"Follow up re: {what}"
            display = text
            area_links = [link for link in task.links if link in vault.areas]
            tags = [self.settings.followup_context]
            if project is not None:
                tags.append("#next")
            elif area_links:
                text += f" [[{area_links[0]}]]"
            if task.has("#private"):
                tags.append("#private")
            marker = new_marker()
            follow_id = f"fu-{marker[:8]}"
            line = with_id(format_task(text[:480], tags, dates={"due": today, "created": today},
                                       block_id=f"gtd-{marker}"), follow_id)
            if project is not None:
                add = {"op": "add_task", "path": project.rel, "section": "Steps", "position": "steps", "line": line,
                       "marker": f"^gtd-{marker}"}
            else:
                add = {"op": "add_task", "path": task.path, "section": CONTEXT_HEADING[self.settings.followup_context],
                       "position": "end", "line": line, "marker": f"^gtd-{marker}"}
            link = {"op": "replace_line", "path": task.path, "match": task.raw,
                    "line": with_dependency(task.raw, follow_id)}
            where = project.key if project is not None else "Single actions"
            count = self.follow_count(task)
            ops: list[dict[str, Any]] = [add, link]
            drafts = []
            if self.ledger.get_by_key(key) is None and not vault.is_private_task(task):
                to = mail_address(person, self._contact_email(vault, person))
                draft = write_draft(self.agent, vault, "follow_up", {
                    "to_name": person, "what": what, "follow_ups_so_far": count,
                    "waiting_since": task.dates["created"].isoformat() if task.dates.get("created") else ""})
                if draft is not None:
                    bridge = self.settings.mail_source == "bridge" and self.settings.mail_enabled
                    if bridge:
                        ops.append(draft_op(draft, to))
                    drafts.append({"title": display, "to": to, "saved": bridge, **draft})
            if self._propose("followup", key, {
                    "kind": "follow_up", "title": display, "drafts": drafts,
                    "summary": f"Follow-up action in {where}; the wait moves 7 days once you tick it",
                    "reason": f"Follow-up date {task.due.isoformat()} "
                              + ("is today" if task.due == today else "has passed")
                              + (f". You have followed up {count} time{'s' if count != 1 else ''} already" if count
                                 else ""), "ops": ops}):
                created += 1
        self._expire("followup", valid)
        return [f"Queued {created} follow-up proposals"] if created else []

    def settle_waits(self, vault: Vault) -> list[str]:
        """Ticks that settle a wait, as logged, undoable bookkeeping. A #replied wait waits (⛔) on its 'Reply to'
        action: once that is ticked, the wait closes. A wait waiting on a ticked follow-up action moves its follow-up
        date 7 days from today and counts the follow-up."""
        if self.settings.mode != "approval":
            return []
        done = {t.task_id for t in vault.tasks if t.task_id and t.is_done}
        today = self.agent.today()
        messages = []
        for task in vault.waiting():
            finished = [task_id for task_id in task.depends_on if task_id in done]
            if not finished:
                continue
            where = self._where(vault, task)
            person, _ = task.waiting_parts()
            if task.has("#replied") and len(finished) == len(task.depends_on):
                op = {"op": "complete_task", "path": task.path, "match": task.raw, "done": today.isoformat()}
                summary, note = f"Closed '{task.title}': you replied", f"closed '{task.title}' after your reply"
                count = None
            else:
                followed = [task_id for task_id in finished if task_id.startswith("fu-")]
                if not followed:
                    continue
                line = task.raw
                for task_id in followed:
                    line = without_dependency(line, task_id)
                nxt = today + timedelta(days=7)
                op = {"op": "replace_line", "path": task.path, "match": task.raw, "line": with_date(line, "due", nxt)}
                count = self.follow_count(task) + len(followed)
                summary = f"Followed up on '{task.title}'; next check {nxt.isoformat()}"
                note = (f"followed up with {person or task.title}; next check {nxt:%a %b} {nxt.day} "
                        f"(follow-up {count})")
            try:
                self.agent.apply_ops([op], "auto", f"wait-{new_marker()}", summary)
            except Exception as exc:  # a locked file retries on the next scan
                messages.append(f"CHECK: could not update the wait '{task.title}' ({exc})")
                continue
            if count is not None:
                self.ledger.kv_set(self._follow_key(task), str(count))
            self.agent.log(f"Auto · {where}: {note}")
            messages.append(f"{where}: {note}")
        return messages

    # ------------------------------------------------------------ drop reasons
    def _drop_home(self, vault: Vault, task: Task) -> dict[str, Any]:
        """Where a drop reason goes: the project's decisions and Log; else the area note (the task's own, or the
        one a single action links); else the note the task lives in."""
        project = vault.project_for_path(task.path)
        if project is not None:
            target, separate = decisions_note(vault, project)
            return {"decisions": target, "section": None if separate else "Decisions", "ensure": not separate,
                    "log": project.rel, "where": project.key}
        area = vault.area_for_path(task.path) or next((vault.areas[link] for link in task.links if link in vault.areas),
                                                      None)
        if area is not None:
            return {"decisions": area.rel, "section": "Decisions", "ensure": True, "log": area.rel, "where": area.key}
        stem = task.path.rsplit("/", 1)[-1][:-3]
        return {"decisions": task.path, "section": "Decisions", "ensure": True, "log": task.path, "where": stem}

    def drops(self, vault: Vault) -> list[str]:
        """A task you cancel raises one question in APPROVAL: why? Cancels from before this feature, of tasks the
        agent never saw open, or made by the agent itself ask nothing. A step that waited on it (⛔) gets a
        proposal to go ahead without it."""
        if self.settings.mode != "approval":
            return []
        today = self.agent.today()
        since = self.ledger.kv_get("drops_since")
        if since is None:
            since = today.isoformat()
            self.ledger.kv_set("drops_since", since)
        since_day = date.fromisoformat(since)
        seen = self.ledger.seen_rows()
        valid: set[str] = set()
        knock_valid: set[str] = set()
        asked = 0
        for task in vault.tasks:
            if not task.is_cancelled:
                continue
            key = digest(f"{task.path}\n{task.norm}")[:20]
            cancelled = task.dates.get("cancelled")
            row = seen.get(key)
            if (cancelled is not None and cancelled < since_day) or (cancelled is None and not (row and row["first_open"])):
                continue
            if self.ledger.kv_get(f"agentcancel|{key}"):
                continue
            dedupe = f"drop|{key}"
            valid.add(dedupe)
            existing = self.ledger.get_by_key(dedupe)
            if existing is None or existing["status"] == "stale":
                home = self._drop_home(vault, task)
                title = task.title
                self.ledger.upsert("drop", dedupe, {
                    "kind": "drop_reason", "title": title, "engine_version": PROPOSAL_VERSION,
                    "summary": f"{home['where']} › Decisions and Log",
                    "reason": "You cancelled this. One line on why keeps the decision findable later.",
                    "drop": dict(home, title=title, date=(cancelled or today).isoformat()), "ops": []})
                asked += 1
            if task.task_id:
                for other in vault.tasks:
                    if not other.is_open or task.task_id not in other.depends_on:
                        continue
                    knock = f"dropknock|{other.path}|{other.norm}|{task.task_id}"
                    knock_valid.add(knock)
                    where = (vault.project_for_path(other.path) or vault.area_for_path(other.path))
                    self._propose("drop_knock", knock, {
                        "kind": "drop_knock", "title": f"{other.title} waited on a dropped step",
                        "summary": f"Remove its link to '{task.title}' in {where.key if where else other.path}",
                        "reason": f"You dropped '{task.title}', and '{other.title}' waited on it (⛔). Approve to let "
                                  "it go ahead; reject to keep the link.",
                        "ops": [{"op": "remove_dependency", "path": other.path, "match": other.raw,
                                 "id": task.task_id}]})
        self._expire("drop", valid)
        self._expire("drop_knock", knock_valid)
        return [f"Asked why for {asked} dropped task{'s' if asked != 1 else ''}"] if asked else []

    # ------------------------------------------------------------ phone alerts for timed duties
    def timed_duties(self, vault: Vault) -> list[str]:
        """A recurring duty with a clock time ('by 1:30pm') gets one proposal: a repeating calendar event with an
        alert, so the phone reminds you. Asked once per duty; a rejected one stays rejected."""
        s = self.settings
        if s.mode != "approval":
            return []
        if not (s.calendar_enabled and s.calendar_source == "folder"):
            self._expire("calendar", set())  # Proton Calendar is read only for the agent: its alerts end
            return []
        name = s.calendar_name or "the calendar"
        valid: set[str] = set()
        created = 0
        for task in vault.tasks:
            if not task.is_open or not task.recurrence or task.due is None or task.section.casefold() != "recurring":
                continue
            if vault.area_for_path(task.path) is None and task.path != s.single_actions:
                continue
            clock = find_time(task.title)
            rrule = rrule_from_tasks(task.recurrence, task.due) if clock else None
            if clock is None or rrule is None:
                continue
            key = f"timedduty|{task.path}|{task.norm}|{task.recurrence.casefold()}"
            valid.add(key)
            start = datetime.combine(task.due, clock)
            first = f"{start:%a %b} {start.day}"
            op = {"op": "calendar_add", "path": "calendar", "calendar": name, "uid": f"gtd-duty-{digest(key)[:16]}",
                  "summary": task.title, "start": start.isoformat(timespec="minutes"),
                  "end": start.isoformat(timespec="minutes"), "alert_minutes": s.calendar_alert_minutes,
                  "rrule": rrule, "repeat": task.recurrence, "when": f"{first} {start:%H:%M}"}
            if self._propose("calendar", key, {
                    "kind": "calendar_event", "title": task.title,
                    "summary": f"Repeating event in {name}: {task.recurrence} at {start:%H:%M} from {first}, alert "
                               f"{s.calendar_alert_minutes} min before",
                    "reason": "A recurring duty with a clock time gets a repeating calendar event, so your phone "
                              "alerts you. If the duty's time or rhythm changes, edit or delete the event on your phone.",
                    "ops": [op]}):
                created += 1
        self._expire("calendar", valid)
        return [f"Queued {created} calendar alert proposal{'s' if created != 1 else ''}"] if created else []

    # ------------------------------------------------------------ vault check
    def _quiet(self, vault: Vault, rel: str) -> bool:
        """True once a note has sat untouched edit_quiet_minutes, so the check never edits under your cursor."""
        note = vault.notes.get(rel)
        minutes = self.settings.edit_quiet_minutes
        return note is None or minutes <= 0 or time.time() - note.mtime >= minutes * 60

    def _fix(self, key: str, ops: list[dict[str, Any]], label: str) -> str:
        """Apply a safe fix once, logged and undoable. Returns fixed, undone (a fix you reversed is not redone),
        later (the line changed first; the next check retries), failed or dry."""
        if self.ledger.kv_get(f"lintfix|{key}"):
            return "undone"
        if self.settings.mode != "approval":
            return "dry"
        try:
            self.agent.apply_ops(ops, "auto", f"lint-{new_marker()}", label)
        except OpError:
            return "later"
        except Exception as exc:  # a locked or unreadable file: keep it listed; the log has the details
            self.agent.health.trace("vault fix", exc)
            return "failed"
        self.ledger.kv_set(f"lintfix|{key}", self.agent.today().isoformat())
        self.agent.log(f"Auto · Vault fix: {label}")
        return "fixed"

    def _ask(self, key: str, proposal: dict[str, Any]) -> str | None:
        """Queue a one-tick question in APPROVAL. Returns its id; '' in dry run; None after you chose Leave it."""
        row = self.ledger.get_by_key(key)
        if row is not None and row["status"] == "left":
            return None
        if row is not None and row["status"] == "pending":
            return row["id"]
        if self.settings.mode != "approval":
            return ""
        proposal.update(kind="lint_choice", engine_version=PROPOSAL_VERSION)
        return self.ledger.upsert("lint", key, proposal)

    @staticmethod
    def _note(rel: str) -> str:
        return rel.rsplit("/", 1)[-1][:-3]

    def lint(self, vault: Vault) -> list[str]:
        """Vault check. Safe rules fix on their own (logged, undoable in LOG); choice rules ask one question in
        APPROVAL, linked from TODAY. LINT.md lists what is left. Nothing happens in a note you edited in the last
        edit_quiet_minutes."""
        today = self.agent.today()
        s = self.settings
        skip = (s.archive_dir + "/", s.agent_dir + "/", s.reference_dir + "/SYSTEM/TEMPLATES/")
        sections: list[tuple[str, list[str]]] = []
        valid: set[str] = set()
        counts = {"fixed": 0, "asked": 0}
        left = {"undone": " · you undid the agent's fix", "later": " · the fix retries on the next check",
                "failed": " · the fix failed (see agent.log)", "dry": " · fixed in approval mode"}

        def safe(line: str, rel: str, key: str, ops: list[dict[str, Any]], label: str) -> list[str]:
            """The LINT line to keep for a safe rule: none once fixed."""
            if not self._quiet(vault, rel):
                return [line + " · fixed once the note sits untouched"]
            outcome = self._fix(key, ops, label)
            if outcome == "fixed":
                counts["fixed"] += 1
                return []
            return [line + left[outcome]]

        def choice(line: str, rel: str, key: str, proposal: dict[str, Any]) -> list[str]:
            """The LINT line to keep for a choice rule, linked to its question; none after Leave it."""
            if not self._quiet(vault, rel):
                return [line]
            if key in valid:
                return []  # an identical line already asked this question
            valid.add(key)
            row = self.ledger.get_by_key(key)
            asked = self._ask(key, proposal)
            if asked is None:
                return []
            if row is None or row["status"] != "pending":
                counts["asked"] += 1
            return [line + (f" → [[APPROVAL#^p-{asked}|answer]]" if asked else " · asked in approval mode")]

        aliases = vault.alias_index()
        broken: list[str] = []
        for rel, target in vault.broken_links(skip):
            fix = aliases.get(target.casefold())
            if fix and len(fix) == 1:
                new = next(iter(fix))
                broken += safe(f"- [[{self._note(rel)}]] links `{target}`, which matches [[{new}]]", rel,
                               f"lintlink|{rel}|{target}|{new}",
                               [{"op": "replace_link", "path": rel, "old": target, "new": new}],
                               f"[[{target}]] → [[{new}]] in {self._note(rel)}")
            else:
                broken.append(f"- [[{self._note(rel)}]] links `{target}`, which does not exist")
        sections.append(("Broken links", broken))

        actionable = vault.actionable(today)
        contexts: list[str] = []
        for task in actionable:
            if task.context is not None:
                continue
            contexts += choice(self._task_line(vault, task), task.path, f"lintctx|{task.path}|{task.norm}", {
                "title": f"Pick a context · {task.title}", "summary": f"Tag the action in {self._note(task.path)}",
                "reason": "Every action needs one context tag, so it shows on a Next Actions list.",
                "preview": f"{task.path}\n· {task.raw.strip()}",
                "choices": [{"label": f"Tag {tag}", "ops": [{"op": "add_tag", "path": task.path, "match": task.raw,
                                                               "tag": tag}]} for tag in CONTEXTS]})
        sections.append(("Actions without a context tag", contexts))

        groups: dict[str, list[Task]] = {}
        for task in actionable:
            groups.setdefault(task.norm, []).append(task)
        duplicates: list[str] = []
        for items in groups.values():
            if len(items) < 2:
                continue
            first, second = items[0], items[1]
            line = self._task_line(vault, first, f" (×{len(items)})")
            if all(t.path == first.path and t.raw.rstrip() == first.raw.rstrip() for t in items):
                duplicates += safe(line, first.path, f"lintdup|{first.path}|{digest(first.raw)[:16]}|{len(items)}",
                                   [{"op": "remove_line", "path": first.path, "match": first.raw, "occurrence": 2}],
                                   f"Removed a duplicate of '{first.title}' in {self._note(first.path)}")
            elif len(items) == 2:
                names = ([f"Keep the one in {self._note(first.path)}", f"Keep the one in {self._note(second.path)}"]
                         if first.path != second.path else ["Keep the first", "Keep the second"])
                duplicates += choice(line, second.path, "lintdupe|" + digest(f"{first.path}\n{first.raw}\n"
                                                                              f"{second.path}\n{second.raw}")[:20], {
                    "title": f"Duplicate · {first.title}", "summary": "The same action in two places",
                    "reason": "Keep one copy so the action shows once.",
                    "preview": f"First: {first.path}\n· {first.raw.strip()}\n\nSecond: {second.path}\n· {second.raw.strip()}",
                    "choices": [{"label": names[0], "ops": [{"op": "remove_line", "path": second.path,
                                                             "match": second.raw, "occurrence": 1}]},
                                {"label": names[1], "ops": [{"op": "remove_line", "path": first.path,
                                                             "match": first.raw, "occurrence": 1}]}]})
            else:
                duplicates.append(line)
        sections.append(("Duplicate actions", duplicates))

        projects: list[str] = []
        for project in sorted(vault.projects.values(), key=lambda p: p.key):
            if project.archived:
                continue
            for problem in project.problems:
                projects.append(f"- [[{project.key}]]: {problem}")
            if project.status == "active" and not vault.open_next(project):
                step = vault.first_open_step(project)
                options = []
                if step is not None:
                    options.append({"label": "Tag first step #next", "ops": [
                        {"op": "add_tag", "path": project.rel, "match": step.raw, "tag": "#next"}]})
                if any(t.is_open and t.is_waiting for t in project.tasks):
                    options.append({"label": "Set status waiting", "ops": [
                        {"op": "set_property", "path": project.rel, "key": "status", "value": "waiting"}]})
                options += [{"label": f"Set status {value}", "ops": [
                    {"op": "set_property", "path": project.rel, "key": "status", "value": value}]}
                    for value in ("someday", "done")]
                projects += choice(f"- [[{project.key}]]: active, but no `#next` step", project.rel,
                                   f"lintnonext|{project.key}|{step.norm if step else ''}", {
                    "title": f"No next step · {project.key}", "summary": f"{project.key} is active",
                    "reason": "An active project needs one #next step, or a status that says why not.",
                    "preview": (f"First open step in {project.rel}\n· {step.raw.strip()}" if step else
                                f"{project.rel} has no open step. Add one by hand, or change the status."),
                    "choices": options})
            if project.status == "active" and project.due and project.due < today:
                week, month = today + timedelta(days=7), today + timedelta(days=30)
                projects += choice(f"- [[{project.key}]]: due {project.due.isoformat()} has passed", project.rel,
                                   f"lintdue|{project.key}|{project.due.isoformat()}", {
                    "title": f"Past due · {project.key}", "summary": f"{project.key} was due {project.due.isoformat()}",
                    "reason": "The due date has passed. Move it, clear it, or finish the project.",
                    "preview": f"{project.rel} › properties\n· due: {project.due.isoformat()}\n\n"
                               f"Due in a week → due: {week.isoformat()}\nDue in a month → due: {month.isoformat()}",
                    "choices": [{"label": "Due in a week", "ops": [{"op": "set_property", "path": project.rel,
                                                                    "key": "due", "value": week.isoformat()}]},
                                {"label": "Due in a month", "ops": [{"op": "set_property", "path": project.rel,
                                                                     "key": "due", "value": month.isoformat()}]},
                                {"label": "Clear the due date", "ops": [{"op": "set_property", "path": project.rel,
                                                                         "key": "due", "value": ""}]},
                                {"label": "Set status done", "ops": [{"op": "set_property", "path": project.rel,
                                                                      "key": "status", "value": "done"}]}]})
            parked = vault.open_next(project) if project.status in {"someday", "done", "dropped"} else []
            if parked:
                count = len(parked)
                projects += safe(f"- [[{project.key}]]: status {project.status}, but it still has `#next` steps",
                                 project.rel, f"lintpark|{project.key}|{project.status}|"
                                 + "|".join(sorted(t.norm for t in parked)),
                                 [{"op": "remove_tag", "path": project.rel, "match": t.raw, "tag": "#next"}
                                  for t in parked],
                                 f"Took #next off {count} step{'s' if count != 1 else ''} in {project.key} "
                                 f"(status {project.status})")
            if not project.area:
                projects.append(f"- [[{project.key}]]: no area")
        for rel in vault.stray_projects:
            projects.append(f"- `{rel}` looks like a project note but has no `type: project` property")
        sections.append(("Projects", projects))

        waits: list[str] = []
        for task in vault.waiting():
            if task.due is not None:
                continue
            waits += choice(self._task_line(vault, task), task.path, f"lintwait|{task.path}|{task.norm}", {
                "title": f"Follow-up date · {task.title}", "summary": f"Wait in {self._note(task.path)}",
                "reason": "A wait needs a follow-up date (📅), so it comes back when the answer is late.",
                "preview": f"{task.path}\n· {task.raw.strip()}",
                "choices": [{"label": label, "ops": [{"op": "set_date", "path": task.path, "match": task.raw,
                                                      "field": "due", "value": (today + timedelta(days=days)).isoformat()}]}
                            for label, days in (("Follow up in 3 days", 3), ("Follow up in a week", 7),
                                                ("Follow up in 2 weeks", 14))]})
        sections.append(("Waiting items without a follow-up date", waits))

        unreadable: list[str] = []
        for task in vault.tasks:
            if task.metadata_ok:
                continue
            line = f"- `{task.raw.strip()[:120]}` · [[{self._note(task.path)}]]"
            fixed = text_before_dates(task.raw)
            if fixed is None:
                unreadable.append(line)
                continue
            unreadable += safe(line, task.path, f"lintdate|{task.path}|{digest(task.raw)[:16]}",
                               [{"op": "replace_line", "path": task.path, "match": task.raw, "line": fixed}],
                               f"Moved text before the dates: '{task.title}' in {self._note(task.path)}")
        sections.append(("Dates the Tasks plugin cannot read (text after a date)", unreadable))

        stray = sorted(rel for rel in vault.files
                       if not rel.startswith(s.archive_dir + "/")
                       and (("." not in rel.rsplit("/", 1)[-1]) or any(rel.lower().endswith(x) for x in STRAY_SUFFIXES)))
        sections.append(("Stray files (backups or downloads inside the vault)", [f"- `{rel}`" for rel in stray]))
        self._expire("lint", valid)
        total = sum(len(lines) for _, lines in sections)
        body = ["# LINT", "", f"Vault check · {_fmt_day(today)} {today.year}. {total} item{'s' if total != 1 else ''} "
                "left. Safe fixes happen on their own and are listed in [[LOG]], with Undo. Questions wait in "
                "[[APPROVAL]]. Rules: [[VAULT_RULES]].", ""]
        for title, lines in sections:
            body += [f"## {title} ({len(lines)})", *(lines or ["- None"]), ""]
        write_owned(self.settings.vault_path(s.lint_note), "\n".join(body), s.state_dir)
        messages = []
        if counts["fixed"]:
            messages.append(f"Fixed {counts['fixed']} vault issue{'s' if counts['fixed'] != 1 else ''}")
        if counts["asked"]:
            messages.append(f"Asked {counts['asked']} vault question{'s' if counts['asked'] != 1 else ''}")
        return messages

    # ------------------------------------------------------------ current state
    def _events_near(self, now: datetime) -> tuple[list[Event], str | None]:
        """Calendar events from AFTER_DAYS before now to UPCOMING_DAYS after, read once per run."""
        if self._near is None:
            self._near = self.calendar_events(now - timedelta(days=AFTER_DAYS), now + timedelta(days=UPCOMING_DAYS))
        return self._near

    def current_states(self, vault: Vault, now: datetime | None = None) -> list[str]:
        """Keep each project's Current state as bookkeeping (see current_state.py for who owns the section)."""
        today = self.agent.today()
        now = now or datetime.now()
        upcoming: dict[str, str] | None = {}
        if self.settings.calendar_enabled:
            events, warning = self._events_near(now)
            upcoming = None if warning and not events else next_events(vault, events, now)  # None: unreadable
        written = 0
        for project in sorted(vault.projects.values(), key=lambda p: p.key):
            note = vault.notes.get(project.rel)
            if project.archived or project.status in {"done", "dropped"} or note is None:
                continue
            body = section_body(note.text)
            if body is None or not self._quiet(vault, project.rel):
                continue
            key = project.key
            kept = next((line for line in body.split("\n") if line.startswith(NEXT_EVENT)), None)
            computed = state_text(vault, project, kept if upcoming is None else upcoming.get(key))
            owner = self.ledger.kv_get(f"state_owner|{key}")
            if body in {"", PLACEHOLDER}:
                written += self._write_state(project, computed, body)  # empty or handed back: the agent's
                continue
            if owner is None:
                row = self.ledger.get_by_key(f"state|{key}")
                if row is None:
                    self._propose("state", f"state|{key}", {
                        "kind": "current_state", "title": key,
                        "summary": f"Replace the section in {key}",
                        "reason": "From now on the agent can keep this section current: next step, waits, blockers, "
                                  "open questions, due date and last change. Approve to hand it over. Reject to keep "
                                  "yours: the agent will not ask again or write it.",
                        "ops": [{"op": "set_section", "path": project.rel, "section": SECTION, "text": computed,
                                 "old": body}]})
                    continue
                if row["status"] == "pending":
                    continue
                if row["status"] != "committed":
                    self.ledger.kv_set(f"state_owner|{key}", "you")
                    continue
                owner = "agent"
                self.ledger.kv_set(f"state_owner|{key}", owner)
                self.ledger.kv_set(f"state_written|{key}", digest(body))
            if owner != "agent":
                continue
            last = self.ledger.kv_get(f"state_written|{key}") or digest(body)
            if digest(body) != last:
                self.ledger.kv_set(f"state_owner|{key}", f"you|{today.isoformat()}")
                self.agent.log(f"Current state of {key} is yours now: you edited it, so the agent stopped updating it")
                continue
            written += self._write_state(project, computed, body)
        return [f"Updated the current state of {written} project{'s' if written != 1 else ''}"] if written else []

    def after_events(self, vault: Vault, now: datetime | None = None) -> list[str]:
        """Once a project's event ends, APPROVAL asks what came out of it: once per event, from the day this
        started. An answer you saved gets GLM's proposed steps here, at the next scan."""
        s = self.settings
        if not s.calendar_enabled or s.mode != "approval":
            return []
        now = now or datetime.now()
        messages = self._steps_after_events(vault)
        since = self.ledger.kv_get("after_events_since")
        if since is None:
            since = datetime.combine(now.date(), datetime.min.time()).isoformat(timespec="minutes")
            self.ledger.kv_set("after_events_since", since)
        events, _ = self._events_near(now)
        start = max(datetime.fromisoformat(since), now - timedelta(days=AFTER_DAYS))
        asked = 0
        for key, found in events_by_project(vault, events).items():
            project = vault.projects[key]
            for event in found:
                if event.end > now or event.end <= start:
                    continue
                dedupe = f"after|{key}|{event.uid or digest(event.summary)[:16]}|{event.start.isoformat()}"
                when = event.start.date().isoformat() if event.all_day else f"{event.start:%Y-%m-%d %H:%M}"
                private = vault.is_private_project(project)
                asked += self._propose("event", dedupe, {
                    "kind": "after_event", "title": event.summary, "summary": f"{key} › Log",
                    "reason": f"{event.summary} ({when}) has ended. One line on what came out of it keeps {key} "
                              "current" + ("." if private else "; GLM then proposes steps from it."),
                    "event": {"project": key, "rel": project.rel, "title": event.summary[:150],
                              "day": event.start.date().isoformat(), "private": private},
                    "ops": []})
        if asked:
            messages.append(f"Asked what came out of {asked} event{'s' if asked != 1 else ''}")
        return messages

    def _steps_after_events(self, vault: Vault) -> list[str]:
        """Steps for each answer you saved about an event: one GLM call, then a proposal with a box per step."""
        messages: list[str] = []
        today = self.agent.today()
        for flag, state in self.ledger.kv_items("aftersteps|").items():
            if state != "waiting":
                continue
            row = self.ledger.get(flag.split("|", 1)[1])
            question = json.loads(row["proposal_json"]) if row is not None else {}
            event = question.get("event") or {}
            project = vault.projects.get(event.get("project", ""))
            if project is None or project.status in {"done", "dropped"} or vault.is_private_project(project):
                self.ledger.kv_set(flag, "skipped")
                continue
            if not self.settings.remote_inference or not self.agent._provider_ready() \
                    or not self.agent.budget.allows("after_event"):
                continue  # asked again once the model can answer
            steps = [t for t in project.tasks if t.is_open and not t.is_waiting and t.section.casefold() == "steps"
                     and t.heading.casefold() != "done" and not t.indent]
            try:
                answer = self.agent.provider.after_event(project.key, event["title"], question["answer"],
                                                         [step.title for step in steps], today)
            except (ProviderError, ValueError, RuntimeError) as exc:
                self.ledger.kv_set(flag, "failed")
                messages.append(f"CHECK: no steps proposed after {event['title']} ({exc}); add them by hand")
                continue
            self.ledger.kv_set(flag, "done")
            items: list[dict[str, Any]] = []
            gave_next = False
            for step in (answer.get("steps") or [])[:3] if isinstance(answer, dict) else []:
                try:
                    title = clean_text(str(step.get("title") or ""), 150) if isinstance(step, dict) else ""
                    if not title or "[[" in title or TAG_RE.search(title):
                        continue
                    context = step.get("context") if step.get("context") in CONTEXTS else "#computer"
                    draft = {"route": "next_action", "title": title, "context": context, "project": project,
                             "dates": {}, "extra_tags": [], "force_queue": gave_next}
                    ops, summary, shown = compile_ops(vault, draft, capture=question["answer"], reference=today,
                                                      marker=new_marker())
                except (ValueError, RuntimeError):
                    continue
                gave_next = gave_next or any(op.get("position") == "steps" for op in ops)
                items.append({"label": f"Step · {shown} → {summary}", "ops": ops, "checked": True})
            if not items:
                continue
            explanation = re.sub(r"\s+", " ", str(answer.get("explanation") or ""))[:300]
            self._propose("event", f"aftersteps|{row['id']}", {
                "kind": "after_event_steps", "title": event["title"],
                "summary": f"{project.key} › Steps", "base_ops": [], "items": items,
                "reason": " ".join(part for part in [
                    f"From what came out of it: {question['answer'].rstrip('.')}.", explanation,
                    "Untick any you don't want."] if part),
                "ops": [op for item in items for op in item["ops"]]})
        return messages

    def _write_state(self, project: Project, computed: str, body: str) -> int:
        key = project.key
        if body == computed or self.settings.mode != "approval":
            return 0
        op = {"op": "set_section", "path": project.rel, "section": SECTION, "text": computed, "old": body}
        try:
            self.agent.apply_ops([op], "state", f"state-{key}-{new_marker()}", f"Current state of {key}")
        except OpError:
            return 0  # the note changed since it was read; the next check tries again
        except Exception as exc:  # a locked file: the next check tries again; the log has the details
            self.agent.health.trace("current state", exc)
            return 0
        self.ledger.supersede_undo("state", f"state-{key}-")
        self.ledger.kv_set(f"state_owner|{key}", "agent")
        self.ledger.kv_set(f"state_written|{key}", digest(computed))
        self.agent.log(f"Auto · Current state of {key} updated")
        return 1

    # ------------------------------------------------------------ calendar
    def calendar_events(self, start: datetime, end: datetime) -> tuple[list[Event], str | None]:
        if not self.settings.calendar_enabled:
            return [], None
        if self.settings.calendar_source == "folder":
            s = self.settings
            try:
                collection = radicale.choose(radicale.collections(Path(s.calendar_folder), s.calendar_user),
                                             s.calendar_name)
                return parse_events(radicale.read_items(collection), start, end, s.timezone), None
            except radicale.CalendarError as exc:
                return [], str(exc)
            except (ValueError, KeyError) as exc:
                return [], f"Calendar could not be read ({type(exc).__name__})"
        url = get_secret("calendar", self.settings.state_dir)
        if not url:
            return [], "Calendar is enabled, but no link is saved. Run: run.ps1 set-secret calendar"
        text, warning = fetch_ics(url, self.settings.state_dir / "calendar" / "cache.ics",
                                  self.settings.calendar_cache_minutes)
        if text is None:
            return [], warning
        try:
            return parse_events(text, start, end, self.settings.timezone), warning
        except (ValueError, KeyError) as exc:
            return [], f"Calendar could not be read ({type(exc).__name__})"

    # ------------------------------------------------------------ daily digest
    def report_lines(self, vault: Vault, now: datetime, today: date) -> list[str]:
        """The short report: today's calendar and free time, the Inbox and APPROVAL counts, stuck projects."""
        s = self.settings
        lines: list[str] = []
        actionable = vault.actionable(today)
        if not s.calendar_enabled:
            lines.append("- Calendar off. Turn it on under `[calendar]` in config.toml (see README).")
        else:
            day_start = datetime.combine(today, datetime.min.time())
            next_day = day_start + timedelta(days=1)
            events, warning = self.calendar_events(day_start, next_day)

            def span(event: Event) -> str:  # a timed event can start before today or end after it
                if event.start < day_start and event.end > next_day:
                    return "all day"
                if event.start < day_start:
                    return f"until {event.end:%H:%M}"
                if event.end > next_day:
                    return f"{event.start:%H:%M} until {event.end:%a %H:%M}"
                return f"{event.start:%H:%M}–{event.end:%H:%M}"
            parts = [f"all day {_review_text(e.summary)}" for e in events if e.all_day]
            parts += [f"{span(e)} {_review_text(e.summary)}" for e in events if not e.all_day]
            lines.append("- Calendar: " + (" · ".join(parts) if parts else "nothing scheduled today")
                         + (f" _({warning})_" if warning else ""))
            gaps = free_gaps(events, today, s.day_start, s.day_end, now if now.date() == today else None,
                             s.min_free_minutes)
            sized = sorted((t for t in actionable if t.minutes), key=lambda t: (t.due or date.max, -t.minutes))
            shown = []
            for begin, finish in gaps[:3]:
                minutes = int((finish - begin).total_seconds() // 60)
                fits = [t for t in sized if t.minutes <= minutes][:2]
                shown.append(f"{begin:%H:%M}–{finish:%H:%M}" + (" fits " + ", ".join(
                    f"{_review_text(t.title)} ({t.minutes}m)" for t in fits) if fits else ""))
            if shown:
                lines.append("- Free: " + " · ".join(shown))
        inbox_path = s.vault_path(s.inbox)
        inbox_count = len(parse_captures(read_snapshot(inbox_path), s.max_capture_chars)) if inbox_path.exists() else 0
        pending = self.ledger.count_pending()
        lines.append(f"- [[INBOX]]: {inbox_count} line{'s' if inbox_count != 1 else ''} · [[APPROVAL]]: {pending} waiting")
        if s.mail_enabled:
            lines.append("- Mail today: " + mail_today(self.ledger, today))
        no_next = [p.key for p in vault.active_projects() if not vault.open_next(p)]
        past_due = [p.key for p in vault.active_projects() if p.due and p.due < today]
        if no_next:
            lines.append("- Active projects without a next step: " + ", ".join(f"[[{k}]]" for k in no_next))
        if past_due:
            lines.append("- Projects past due: " + ", ".join(f"[[{k}]]" for k in past_due))
        week_ago = (today - timedelta(days=7)).isoformat()
        yours = sorted(key.split("|", 1)[1] for key, value in self.ledger.kv_items("state_owner|").items()
                       if value.startswith("you|") and value[4:] >= week_ago)
        if yours:
            lines.append("- Current state: you edited it in " + ", ".join(f"[[{k}]]" for k in yours) + ", so the "
                         f"agent stopped updating it there. To hand it back, write `{PLACEHOLDER}` in the section.")
        return lines

    def _query(self, *lines: str) -> list[str]:
        filters = [line for line in lines if not line.startswith("sort by")]
        sorting = [line for line in lines if line.startswith("sort by")]
        return ["```tasks", "not done", *filters, not_in_views(self.settings), *sorting, "```"]

    def needs_you(self, vault: Vault, today: date) -> list[tuple[str, list[str]]]:
        """Open items that are due or ask for a decision, each class in its own section (empty ones left out)."""
        s = self.settings
        sections: list[tuple[str, list[str]]] = []
        due = [t for t in vault.tasks if t.is_open and not t.is_waiting and t.due and t.due <= today]
        if due:
            sections.append((f"Overdue and due today ({len(due)})",
                             self._query("due before tomorrow", "tags do not include #waiting", "sort by due")))
        open_ids = {t.task_id for t in vault.tasks if t.task_id and t.is_open}
        follow = [t for t in vault.tasks if t.is_open and t.is_waiting and not t.has("#replied") and t.due
                  and t.due <= today and not any(task_id in open_ids for task_id in t.depends_on)]
        if follow:
            sections.append((f"Follow-ups due ({len(follow)})",
                             self._query("tags include #waiting", "tags do not include #replied", "is not blocked",
                                         "due before tomorrow", "sort by due")))
        stalled = [t for t in vault.waiting() if not t.has("#replied") and self.follow_count(t) >= FOLLOW_UP_LIMIT]
        if stalled:
            sections.append((f"No answer after {FOLLOW_UP_LIMIT} follow-ups ({len(stalled)})", [
                f"- {_review_text(t.title)} in {self._where(vault, t)}: call, escalate or drop it." for t in stalled]))
        questions = []
        for project in sorted(vault.active_projects(), key=lambda p: p.key):
            target, separate = decisions_note(vault, project)
            for question in open_questions(vault, project, target, separate):
                note = question["path"].rsplit("/", 1)[-1][:-3]
                questions.append(f"- [[{note}#{question['source']}|{project.key}]]: {_review_text(question['text'])}")
        if questions:
            sections.append((f"Open questions ({len(questions)})", questions))
        everything = self.ledger.pending()
        pending = [row for row in everything if row["origin"] not in {"drop", "lint", "state"}]
        handovers = [row for row in everything if row["origin"] == "state"]
        drops = [row for row in everything if row["origin"] == "drop"]
        choices = [row for row in everything if row["origin"] == "lint"]
        if pending:
            rows = []
            for row in pending[:APPROVAL_SHOWN]:
                proposal = json.loads(row["proposal_json"])
                label = ROUTE_LABEL.get(proposal["kind"], proposal["kind"].replace("_", " ").capitalize())
                title = _link_text(proposal.get("title") or proposal.get("source") or proposal["kind"])
                where = f" → {_review_text(proposal['summary'])}" if proposal.get("summary") else ""
                rows.append(f"- [[APPROVAL#^p-{row['id']}|{_link_text(label)} · {title}]]{where}")
            if len(pending) > APPROVAL_SHOWN:
                rows.append(f"- …and {len(pending) - APPROVAL_SHOWN} more in [[APPROVAL]]")
            sections.append((f"Waiting in APPROVAL ({len(pending)})", rows))
        if drops:
            sections.append((f"Drop reasons asked ({len(drops)})", [
                f"- [[APPROVAL#^p-{row['id']}|Why drop · {_link_text(json.loads(row['proposal_json'])['title'])}]]"
                for row in drops]))
        if handovers:
            sections.append((f"Current state handovers ({len(handovers)})", [
                "- Approve to let the agent keep the section, reject to keep yours: " + " · ".join(
                    f"[[APPROVAL#^p-{row['id']}|{_link_text(json.loads(row['proposal_json'])['title'])}]]"
                    for row in handovers)]))
        if choices:
            sections.append((f"Vault choices ({len(choices)})", [
                f"- [[APPROVAL#^p-{row['id']}|{_link_text(json.loads(row['proposal_json'])['title'])}]]"
                for row in choices[:APPROVAL_SHOWN]]
                + ([f"- …and {len(choices) - APPROVAL_SHOWN} more in [[APPROVAL]]"]
                   if len(choices) > APPROVAL_SHOWN else [])))
        reviews = [f"- [[{p.key}]] · review date {_fmt_day(p.review)}"
                   for p in sorted(vault.projects.values(), key=lambda p: (p.review or date.max, p.key))
                   if not p.archived and p.status == "someday" and p.review and p.review <= today]
        for row in self.someday_lines(vault):
            if row["review"] and row["review"] <= today:
                words = re.sub(r"\s*·\s*review\s+\d{4}-\d{2}-\d{2}", "", row["text"])
                reviews.append(f"- {_review_text(words)} · review {_fmt_day(row['review'])} · [[SOMEDAY_MAYBE]]")
        if reviews:
            sections.append((f"Someday reviews due ({len(reviews)})", reviews))
        return sections

    def next_action_sections(self, vault: Vault, today: date) -> list[tuple[str, list[str]]]:
        """Every next action by context, as live Tasks queries you can tick in place."""
        single = self.settings.single_actions.removesuffix(".md")
        actionable = vault.actionable(today)
        scope = ["starts before tomorrow", "tags do not include #waiting",
                 f"(tags include #next) OR (path includes {single})"]
        sections = []
        for context in CONTEXTS:
            count = sum(1 for t in actionable if t.context == context)
            if count:
                sections.append((f"@{context[1:]} ({count})",
                                 self._query(f"tags include {context}", *scope, "sort by due", "sort by path")))
        loose = [t for t in actionable if t.context is None]
        if loose:
            sections.append((f"No context tag ({len(loose)})",
                             self._query(*(f"tags do not include {c}" for c in CONTEXTS), *scope)))
        return sections

    def digest(self, vault: Vault, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        today = self.agent.today()
        s = self.settings
        lines = ["## Report", *self.report_lines(vault, now, today), "", "## Needs you"]
        needs = self.needs_you(vault, today)
        for title, items in needs:
            lines += [f"### {title}", *items, ""]
        if not needs:
            lines += ["Nothing due and nothing to decide.", ""]
        lines.append("## Next actions")
        actions = self.next_action_sections(vault, today)
        for title, items in actions:
            lines += [f"### {title}", *items, ""]
        if not actions:
            lines += ["Nothing to do right now. Add a `#next` step to an active project.", ""]
        body = "\n".join(lines)
        budget_line = self.agent.budget.report_line()
        health = self.health_lines(now) + ([budget_line] if budget_line else [])
        bucket = now.replace(minute=now.minute - now.minute % CHECK_REFRESH_MINUTES, second=0, microsecond=0)
        stamp_key = "digest_hash"
        signature = digest(today.isoformat() + bucket.isoformat() + "\n".join(health) + body)
        path = s.vault_path(s.today_note)
        if self.ledger.kv_get(stamp_key) == signature and path.exists():
            return []
        header = [f"# TODAY", "", f"{now:%A, %B} {now.day} · Checked {now:%H:%M} by the GTD agent. Tick tasks here "
                  "and they change where they live; the rest of this note is rewritten.", *health, ""]
        write_owned(path, "\n".join(header) + "\n" + body, s.state_dir)
        self.ledger.kv_set(stamp_key, signature)
        return []

    def health_lines(self, now: datetime) -> list[str]:
        """The agent's last error in plain words, until it recovers (then for a day)."""
        error = self.agent.health.snapshot()["last_error"]
        if not error:
            return []
        try:
            at = datetime.fromisoformat(error["at"])
            recovered = datetime.fromisoformat(error["recovered_at"]) if error.get("recovered_at") else None
        except (KeyError, TypeError, ValueError):
            return []
        if recovered is not None and now - recovered > timedelta(hours=RECOVERED_SHOWN_HOURS):
            return []
        text = str(error.get("text", "")).rstrip(".")
        line = f"Last error {_fmt_when(at, now)} in {error.get('label', 'the agent')}: {_review_text(text)}."
        if error.get("count", 1) > 1:
            line += f" Repeated {error['count']} times."
        line += f" Recovered {_fmt_when(recovered, now)}." if recovered else " Details: `state/logs/agent.log`."
        return [line]

    # ------------------------------------------------------------ weekly review
    def _vague(self, vault: Vault, week: str, today: date) -> list[str]:
        if not (self.settings.vague_check and self.settings.remote_inference and self.agent._provider_ready()):
            return []
        cached = self.ledger.kv_get(f"vague:{week}")
        if cached is not None:
            return json.loads(cached)
        items = [t for t in vault.actionable(today) if not vault.is_private_task(t)][:40]
        if not items:
            return []
        payload = [{"id": t.ref, "text": t.title} for t in items]
        try:
            flags = self.agent.provider.vague_check(payload)
        except (ProviderError, ValueError, RuntimeError, AttributeError):
            return []
        by_ref = {t.ref: t for t in items}
        lines = [self._task_line(vault, by_ref[f["id"]],
                                 f" → {'a project' if f['issue'] == 'project' else 'vague'}; try: "
                                 f"{_review_text(f['suggestion'])}") for f in flags if f["id"] in by_ref]
        self.ledger.kv_set(f"vague:{week}", json.dumps(lines))
        return lines

    # ------------------------------------------------------------ someday list
    SOMEDAY_REVIEW_RE = re.compile(r"·\s*review\s+(\d{4}-\d{2}-\d{2})")

    def someday_lines(self, vault: Vault) -> list[dict[str, Any]]:
        """Bullets on the Someday list with their section, review date and privacy."""
        path = self.settings.vault_path(self.settings.someday)
        if not path.exists():
            return []
        rows, section, in_code = [], "", False
        for line in read_snapshot(path).text.split("\n"):
            if line.strip().startswith(("```", "~~~")):
                in_code = not in_code
                continue
            if in_code:
                continue
            heading = re.match(r"^##\s+(.*)", line)
            if heading:
                section = heading.group(1).strip()
                continue
            if not line.startswith("- ") or re.match(r"- \[.\] ", line):
                continue
            text = line[2:].strip()
            review = self.SOMEDAY_REVIEW_RE.search(text)
            links = re.findall(r"\[\[([^\]|#]+)", text)
            private = "#private" in text.casefold() or any(
                (k in vault.projects and vault.is_private_project(vault.projects[k]))
                or (k in vault.areas and vault.areas[k].private) for k in links)
            rows.append({"line": line, "text": text, "section": section, "private": private,
                         "review": date.fromisoformat(review.group(1)) if review else None,
                         "id": digest(line)[:8]})
        return rows

    def someday_topics(self, vault: Vault, week: str) -> list[str]:
        """Weekly: topics with 3+ Someday lines (at least one from the Inbox) become a proposed someday note."""
        s = self.settings
        if not (s.someday_topics and s.remote_inference and self.agent._provider_ready()):
            return []
        rows = [r for r in self.someday_lines(vault) if not r["private"]]
        if len(rows) < 3 or not any(r["section"] == "Added from the Inbox" for r in rows):
            return []
        cached = self.ledger.kv_get(f"someday:{week}")
        if cached is None:
            try:
                groups = self.agent.provider.someday_topics([{"id": r["id"], "text": r["text"]} for r in rows[:80]])
            except (ProviderError, ValueError, RuntimeError, AttributeError):
                return []
            self.ledger.kv_set(f"someday:{week}", json.dumps(groups))
        else:
            groups = json.loads(cached)
        by_id = {r["id"]: r for r in rows}
        notes = []
        valid: set[str] = set()
        today = self.agent.today()
        for group in groups:
            members = [by_id[i] for i in group["ids"] if i in by_id]
            if len(members) < 3 or not any(m["section"] == "Added from the Inbox" for m in members):
                continue
            key = project_key(group["name"])
            if key.casefold() in vault.by_stem:
                continue
            reviews = sorted(m["review"] for m in members if m["review"])
            body = [re.sub(r"\s*·\s*review\s+\d{4}-\d{2}-\d{2}", "", m["text"]) for m in members]
            text = project_note_text(
                key, "someday", None, None, "_Not decided yet. What would done look like?_", today,
                review=reviews[0] if reviews else None,
                notes=[f"### Gathered from [[SOMEDAY_MAYBE]] on {today.isoformat()}", *[f"- {b}" for b in body]],
                log=f"Gathered {len(members)} Someday lines into this note.")
            ops = [{"op": "create_file", "path": f"{s.projects_dir}/{key}/{key}.md", "text": text}]
            ops += [{"op": "remove_line", "path": s.someday, "match": m["line"], "occurrence": 1} for m in members]
            dedupe = f"someday_topic|{key}|" + "|".join(sorted(m["id"] for m in members))
            valid.add(dedupe)
            self._propose("someday", dedupe, {
                "kind": "someday_topic", "title": f"Gather {len(members)} Someday lines into {key}",
                "summary": f"New someday project {key}; the lines leave the Someday list",
                "reason": f"You have {len(members)} Someday lines about {group['name']}.", "ops": ops})
            notes.append(f"- {group['name']}: {len(members)} lines. Proposal in [[APPROVAL]]")
        self._expire("someday", valid)
        return notes

    # ------------------------------------------------------------ research reports
    def research(self, vault: Vault) -> list[str]:
        """File reports from the research folder (approval), and close requests whose report is filed."""
        s = self.settings
        messages: list[str] = []
        research_notes = {rel: note for rel, note in vault.notes.items() if rel.startswith(s.research_dir + "/")}
        filed_for: set[str] = set()
        for note in research_notes.values():
            for value in (note.frontmatter.get("project") or []) if isinstance(note.frontmatter.get("project"), list) \
                    else [note.frontmatter.get("project")]:
                if isinstance(value, str):
                    filed_for.add(value.strip("[]\" "))
        requests_dir = s.vault_path(f"{s.agent_dir}/RESEARCH_REQUESTS")
        if requests_dir.is_dir():
            for path in sorted(requests_dir.glob("*.md")):
                text = read_snapshot(path).text
                if path.stem in filed_for and "\nstatus: open\n" in text:
                    write_owned(path, text.replace("\nstatus: open\n", "\nstatus: done\n", 1), s.state_dir)
        if not s.research_reports_dir:
            return messages
        folder = Path(s.research_reports_dir)
        folder = folder if folder.is_absolute() else s.root / folder
        if not folder.is_dir():
            if self.ledger.kv_get("research_folder_missing") != str(folder):
                self.ledger.kv_set("research_folder_missing", str(folder))
                messages.append(f"CHECK: research reports folder not found: {folder}")
            return messages
        today = self.agent.today()
        valid: set[str] = set()
        for path in sorted(folder.glob("*.md"))[:50]:
            raw = path.read_bytes()
            if len(raw) > 300_000:
                continue
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            front = parse_frontmatter(text)
            key = str(front.get("request") or front.get("project") or "").strip("[]\" ")
            if not key:
                key = next((k for k in vault.projects if path.stem.upper().startswith(k)), "")
            project = vault.projects.get(key)
            if project is None or vault.is_private_project(project):
                continue
            _, body_start = split_frontmatter(text)
            body = "\n".join(text.split("\n")[body_start:]).strip()
            body = re.sub(r"<!--\s*gtd-agent.*?-->", "", body, flags=re.S)
            stamp = today.isoformat()
            rel = f"{s.research_dir}/{key}_RESEARCH_{stamp}.md"
            number = 2
            while s.vault_path(rel).exists():
                rel = f"{s.research_dir}/{key}_RESEARCH_{stamp}_{number}.md"
                number += 1
            note = "\n".join(["---", "type: reference", f"project: [\"[[{key}]]\"]", "origin: agent",
                               "trust: external", f"source: \"{path.name}\"", f"filed: {stamp}", "---",
                               f"# {rel.rsplit('/', 1)[-1][:-3]}", "", body, ""])
            dedupe = f"research|{key}|{digest(text)[:16]}"
            valid.add(dedupe)
            self._propose("research", dedupe, {
                "kind": "research_report", "title": f"File research report for {key}",
                "summary": f"New reference note {rel}, linked from {key}",
                "reason": f"{path.name} arrived in the research folder. It is outside content: skim it before approving.",
                "ops": [{"op": "create_file", "path": rel, "text": note},
                        {"op": "append_log", "path": project.rel,
                         "entry": f"{stamp} Research report filed: [[{rel.rsplit('/', 1)[-1][:-3]}]]"}]})
        self._expire("research", valid)
        return messages

    def review_block(self, vault: Vault, monday: date, today: date) -> str:
        sunday = monday + timedelta(days=6)
        year, week, _ = monday.isocalendar()
        week_name = f"{year}-W{week:02d}"
        seen = self.ledger.seen_rows()
        done = []
        for task in vault.tasks:
            if not task.is_done:
                continue
            when = task.done
            if when is None:
                row = seen.get(digest(f"{task.path}\n{task.norm}")[:20])
                when = date.fromisoformat(row["done_seen"]) if row and row["done_seen"] else None
            if when and monday <= when <= sunday:
                done.append((when, task))
        done.sort(key=lambda pair: pair[0])
        active = vault.active_projects()
        by_status: dict[str, int] = {}
        for project in vault.projects.values():
            if not project.archived:
                by_status[project.status] = by_status.get(project.status, 0) + 1
        untouched = [p for p in active
                     if (today - vault.mtime_date(p)).days >= self.settings.untouched_project_days]
        review_due = [p for p in vault.projects.values() if not p.archived and p.review and p.review <= sunday]
        stale_cutoff = today - timedelta(days=self.settings.stale_action_days)
        actionable = vault.actionable(today)
        stale = []
        for task in actionable:
            first = self.first_seen(task)
            if first and first <= stale_cutoff:
                stale.append((first, task))
        stale.sort(key=lambda pair: pair[0])
        overdue_waits = sorted((t for t in vault.waiting() if t.due and t.due <= today and not t.has("#replied")),
                               key=lambda t: t.due)
        no_date_waits = [t for t in vault.waiting() if t.due is None]
        s = self.settings
        inbox_path = s.vault_path(s.inbox)
        inbox_count = len(parse_captures(read_snapshot(inbox_path), s.max_capture_chars)) if inbox_path.exists() else 0
        horizon_end = today + timedelta(days=14)
        events, warning = self.calendar_events(datetime.combine(today, datetime.min.time()),
                                               datetime.combine(horizon_end, datetime.min.time()))
        upcoming = sorted((t for t in vault.tasks if t.is_open and t.due and today < t.due <= horizon_end
                           and vault.status_for(t.path) not in {"someday", "done", "dropped"}), key=lambda t: t.due)
        first_of_month = today.day <= 7
        someday = [p for p in vault.projects.values() if not p.archived and p.status == "someday"
                   and (first_of_month or (p.review and p.review <= sunday))]
        someday.sort(key=lambda p: (p.review or date.max, p.key))
        someday_lines_due = [r for r in self.someday_lines(vault) if r["review"] and r["review"] <= sunday]
        review_due = [p for p in review_due if p.status != "someday"]
        lines = [AUTO_BEGIN, f"## This week at a glance (agent)",
                 f"Week {week_name}: {_fmt_day(monday)} to {_fmt_day(sunday)}. Built {datetime.now():%a %H:%M} from "
                 "your vault. The agent may refresh this block until you edit it.", ""]
        lines += [f"### Done this week ({len(done)})",
                  *([self._task_line(vault, t, f" · {_fmt_day(when)}") for when, t in done] or ["- Nothing ticked yet"]), ""]
        lines += ["### Projects",
                  "- " + " · ".join(f"{k} {v}" for k, v in sorted(by_status.items())) + " · [[PROJECTS_INDEX]]",
                  "- No next step: " + (", ".join(f"[[{p.key}]]" for p in active if not vault.open_next(p)) or "none"),
                  "- Past due: " + (", ".join(f"[[{p.key}]] ({p.due.isoformat()})" for p in active
                                                if p.due and p.due < today) or "none"),
                  f"- Untouched {self.settings.untouched_project_days}+ days: "
                  + (", ".join(f"[[{p.key}]]" for p in untouched) or "none"),
                  "- Review date reached: " + (", ".join(f"[[{p.key}]]" for p in review_due) or "none"), ""]
        lines += ["### Waiting for",
                  *([self._task_line(vault, t, f" · follow up since {_fmt_day(t.due)}") for t in overdue_waits]
                    or ["- No follow-ups due"]),
                  *[self._task_line(vault, t, " · no follow-up date") for t in no_date_waits], ""]
        lines += [f"### Next actions ({len(actionable)} available)",
                  *([self._task_line(vault, t, f" · on the list since {_fmt_day(first)}") for first, t in stale[:12]]
                    or [f"- None older than {self.settings.stale_action_days} days"]), ""]
        vague = self._vague(vault, week_name, today)
        if vague:
            lines += ["### Possibly vague (model suggestions)", *vague, ""]
        lines += ["### Inbox and approvals",
                  f"- [[INBOX]]: {inbox_count} · [[APPROVAL]]: {self.ledger.count_pending()} waiting", ""]
        lines += ["### Coming up (14 days)"]
        if warning:
            lines.append(f"- _{warning}_")
        lines += [f"- {_fmt_day(e.start.date())} {'' if e.all_day else e.start.strftime('%H:%M') + ' '}"
                  f"{_review_text(e.summary)}" for e in events]
        lines += [self._task_line(vault, t, f" · due {_fmt_day(t.due)}") for t in upcoming]
        if not events and not upcoming:
            lines.append("- Nothing dated")
        lines.append("")
        if someday or someday_lines_due:
            lines += ["### Someday to reconsider",
                      *[f"- [[{p.key}]]" + (f" · review date {_fmt_day(p.review)}" if p.review else " · monthly look")
                        for p in someday],
                      *[f"- {_review_text(r['text'])} · [[SOMEDAY_MAYBE]]" for r in someday_lines_due], ""]
        topics = self.someday_topics(vault, week_name)
        if topics:
            lines += ["### Someday topics with 3 or more lines", *topics, ""]
        area_lines = []
        for area in sorted(vault.areas.values(), key=lambda a: a.key):
            count = sum(1 for p in active if p.area == area.key)
            area_lines.append(f"- [[{area.key}]]: {count} active project{'s' if count != 1 else ''}")
        lines += ["### Areas", *area_lines, ""]
        inner = "\n".join(lines).rstrip("\n")
        return f"{inner}\n\n{AUTO_END} sha={block_sha(inner)} -->"

    def weekly(self, vault: Vault, now: datetime | None = None, force: bool = False) -> list[str]:
        now = now or datetime.now()
        today = self.agent.today()
        s = self.settings
        if not force and (now.weekday() != s.review_weekday or now.hour < s.review_hour):
            return []
        monday = today - timedelta(days=today.weekday())
        year, week, _ = today.isocalendar()
        name = f"{year}-W{week:02d}"
        path = s.vault_path(f"{s.reviews_dir}/{name}.md")
        if path.exists():
            text = read_snapshot(path).text
            start = text.find(AUTO_BEGIN)
            match = AUTO_END_RE.search(text, start) if start >= 0 else None
            if start < 0 or match is None:
                return []
            inner = text[start:match.start()].rstrip("\n")
            if not match.group(1) or block_sha(inner) != match.group(1):
                return []  # you edited the agent block; leave it alone
            last = self.ledger.kv_get(f"review_refreshed:{name}")
            if not force and last and datetime.fromisoformat(last) > now - timedelta(minutes=60):
                return []
            block = self.review_block(vault, monday, today)
            if text[start:match.end()] == block:
                return []
            snapshot = read_snapshot(path)
            atomic_replace(path, text[:start] + block + text[match.end():], s.state_dir, snapshot.sha256,
                           snapshot.newline, backup=False)
            self.ledger.kv_set(f"review_refreshed:{name}", now.isoformat())
            return [f"Refreshed weekly review {name}"]
        block = self.review_block(vault, monday, today)
        template_path = s.vault_path(s.review_template)
        template = read_snapshot(template_path).text if template_path.exists() else ""
        _, body_start = split_frontmatter(template)
        template_body = "\n".join(template.split("\n")[body_start:]).strip()
        template_body = template_body.replace("{{date}}", today.isoformat()).replace("{{title}}", name)
        text = "\n".join(["---", "type: review", f"week: {name}", "---", f"# Weekly review {name}", "",
                          "Work top to bottom. The first block is facts from the agent; the rest is yours.", "",
                          block, "", template_body, ""])
        atomic_replace(path, text, s.state_dir, None, "\n", backup=False)
        self.ledger.kv_set(f"review_refreshed:{name}", now.isoformat())
        self.agent.log(f"Weekly review note created: {name}")
        return [f"Created weekly review {name}"]

    # ------------------------------------------------------------ all
    def ensure_agent_notes(self) -> None:
        """Create the agent's own notes so links to them resolve from the first run."""
        s = self.settings
        stubs = {
            s.today_note: "# TODAY\n\nThe daily digest appears here once the agent runs.\n",
            s.lint_note: "# LINT\n\nThe vault check appears here once the agent runs.\n",
            s.log_note: "# LOG\n\nWhat the GTD agent changed, newest first. Undo boxes for recent changes appear "
                        "here once the agent runs.\n\n## History\n",
        }
        for rel, text in stubs.items():
            path = s.vault_path(rel)
            if not path.exists():
                write_owned(path, text, s.state_dir)
        if s.mode == "approval" and not s.vault_path(s.review).exists():
            self.agent.render_reviews()

    def _step(self, label: str, operation: Any, *args: Any) -> Any:
        """Run one upkeep step. If it fails, record why and let the other steps run anyway."""
        name = f"Housekeeping › {label}"
        try:
            result = operation(*args)
        except Exception as exc:  # noqa: BLE001 - one broken step must not block TODAY, LINT or APPROVAL
            self.agent.health.error(name, exc)
            return None
        self.agent.health.recovered(name)
        return result

    def run(self, now: datetime | None = None) -> list[str]:
        self._near = None
        messages: list[str] = []
        self._step("agent notes", self.ensure_agent_notes)
        vault = self._step("reading the vault", self.agent.vault)
        if vault is None:
            return messages
        self._step("task history", self.track_tasks, vault)
        messages += self._step("next steps", self.promote, vault) or []
        tidied = self._step("tidy finished steps", self.tidy, vault) or []
        messages += tidied
        if any("promoted" in message for message in messages) or tidied:
            vault = self._step("reading the vault", self.agent.vault) or vault
        messages += self._step("follow-ups", self.followups, vault) or []
        messages += self._step("settling waits", self.settle_waits, vault) or []
        messages += self._step("drop reasons", self.drops, vault) or []
        messages += self._step("watch edits", Watcher(self).run, vault) or []
        messages += self._step("calendar alerts", self.timed_duties, vault) or []
        messages += self._step("research reports", self.research, vault) or []
        messages += self._step("mail", MailIntake(self).run, vault, now) or []
        messages += self._step("Inbox files", DocumentIntake(self).run, vault, now) or []
        checked = self._step("vault check", self.lint, vault) or []
        messages += checked
        if any(message.startswith("Fixed ") for message in checked):
            vault = self._step("reading the vault", self.agent.vault) or vault
        messages += self._step("current state", self.current_states, vault, now) or []
        messages += self._step("after events", self.after_events, vault, now) or []
        messages += self._step("TODAY", self.digest, vault, now) or []
        messages += self._step("weekly review", self.weekly, vault, now) or []
        if self.settings.mode == "approval":
            messages += [f"Trusted: {outcome}" for outcome in self._step("trusted kinds", self.agent.apply_trusted) or []]
            self._step("APPROVAL", self.agent.render_reviews)
        return messages
