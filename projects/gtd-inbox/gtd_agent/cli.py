from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from . import mail, mailbox, pdf, radicale
from .budget import Budget
from .core import MODEL_JOBS, Settings, is_openrouter, parse_captures, process_lock, read_snapshot
from .ledger import Ledger
from .credentials import NAMES, has_secret, secret_names, set_secret
from .export import export_project, find_project, notes_for
from .housekeeping import Housekeeper
from .providers import OpenRouter, ProviderError
from .runner import Control, Runner, loop_operations
from .telegram import TelegramCapture, TelegramError
from .vault import Vault
from .workflow import Agent, ApprovalError

HERE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = HERE / "config.toml"
REQUEST_WAIT_SECONDS = 60


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gtd-agent", description="Review-first GTD agent for an Obsidian vault")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check configuration, vault layout and saved secrets without writing")
    sub.add_parser("scan", help="Process the Inbox and run housekeeping once")
    sub.add_parser("run", help="Keep running: approvals every poll, Inbox and housekeeping every scan")
    sub.add_parser("stop", help="Ask a running agent (hidden or not) to stop")
    sub.add_parser("restart", help="Restart a running agent with the current code, or start one hidden")
    sub.add_parser("sync-reviews", help="Apply checked boxes in APPROVAL.md once")
    sub.add_parser("reconcile", help="Check interrupted writes and settle them")
    sub.add_parser("probe-models", help="Send synthetic requests to Jev and GLM (paid; no vault data)")
    sub.add_parser("lint", help="Run the vault check (safe fixes included), write _agent/LINT.md and print it")
    sub.add_parser("digest", help="Write the daily digest to _agent/TODAY.md")
    export = sub.add_parser("export", help="Bundle one project into a single Markdown file in state/exports/")
    export.add_argument("key", help="The project key, such as DEMO_PROJECT")
    weekly = sub.add_parser("weekly-review", help="Create or refresh this week's review note now")
    weekly.add_argument("--refresh", action="store_true", help="Rebuild the agent block if you have not edited it")
    capture = sub.add_parser("capture", help="Add a line to the Inbox")
    capture.add_argument("text", nargs="+")
    secret = sub.add_parser("set-secret", help="Save a secret outside the vault (hidden prompt)")
    secret.add_argument("name", help=f"{', '.join(sorted(NAMES))}, or a name from [endpoint_keys]")
    telegram = sub.add_parser("telegram", help="Telegram capture helpers")
    telegram.add_argument("action", choices=["setup", "poll"])
    calendar = sub.add_parser("calendar", help="Show today's calendar events, or export Radicale's events once")
    calendar.add_argument("action", nargs="?", choices=["today", "export"], default="today")
    calendar.add_argument("folder", nargs="?", type=Path, default=Path("radicale-export"),
                          help="export: where the .ics files go (default: radicale-export)")
    learning = sub.add_parser("learning", help="Inspect, forget or import approved interpretation examples")
    learning_sub = learning.add_subparsers(dest="learning_command", required=True)
    learning_sub.add_parser("list")
    forget = learning_sub.add_parser("forget")
    forget.add_argument("id")
    imported = learning_sub.add_parser("import-v1", help="Copy lessons from the version 1 ledger")
    imported.add_argument("path", type=Path)
    review = sub.add_parser("review", help="Inspect or decide pending proposals")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_sub.add_parser("list")
    for name in ("show", "approve", "reject"):
        review_sub.add_parser(name).add_argument("id")
    return parser


def _doctor(settings: Settings) -> int:
    failures = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"{'OK' if ok else 'CHECK'}: {label}{(' · ' + detail) if detail else ''}")

    print(f"Python {sys.version.split()[0]} · mode {settings.mode} · remote inference "
          f"{'on' if settings.remote_inference else 'off'}")
    print(f"Vault: {settings.root}")
    print(f"State: {settings.state_dir} (outside the vault)")
    check(settings.root.is_dir(), "Vault folder exists")
    for label, rel in (("Inbox", settings.inbox), ("Single actions", settings.single_actions),
                       ("Someday list", settings.someday)):
        path = settings.vault_path(rel)
        check(path.is_file(), label, rel)
    for label, rel in (("Projects folder", settings.projects_dir), ("Areas folder", settings.areas_dir)):
        check(settings.vault_path(rel).is_dir(), label, rel)
    if settings.root.is_dir():
        vault = Vault(settings)
        inbox = settings.vault_path(settings.inbox)
        captures = len(parse_captures(read_snapshot(inbox), settings.max_capture_chars)) if inbox.exists() else 0
        active = len(vault.active_projects())
        print(f"Found {len(vault.projects)} projects ({active} active), {len(vault.areas)} areas, "
              f"{len(vault.tasks)} tasks, {captures} Inbox lines")
        problems = [f"{p.key}: {problem}" for p in vault.projects.values() for problem in p.problems]
        check(not problems, "Project properties", "; ".join(problems[:5]))
    plugins = settings.root / ".obsidian" / "plugins"
    try:
        enabled = json.loads((settings.root / ".obsidian" / "community-plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        enabled = []
    for folder, name, needed_for in (("obsidian-tasks-plugin", "Tasks", "NEXT_ACTIONS and WAITING_FOR views"),
                                     ("dataview", "Dataview", "project tables")):
        check((plugins / folder / "manifest.json").exists() and folder in enabled,
              f"Obsidian {name} plugin installed and enabled", needed_for)
    key = has_secret("openrouter", settings.state_dir)
    if settings.remote_inference:
        check(key, "OpenRouter key", "saved" if key else "missing: run set-secret openrouter")
        for job in MODEL_JOBS:
            url = settings.job_endpoint(job)
            if not is_openrouter(url):
                print(f"NOTE: {job}: {settings.job_model(job)} at {url} (not OpenRouter: no privacy fields are sent; "
                      "trust this endpoint yourself)")
                name = settings.job_key_name(job)
                if name:
                    check(has_secret(name, settings.state_dir), f"Key '{name}' for {job}",
                          "" if has_secret(name, settings.state_dir) else f"missing: run set-secret {name}")
    ledger = Ledger(settings.state_dir)
    try:
        budget = Budget(ledger, settings.budget_monthly_usd, settings.budget_warn_percent)
        print(f"Model budget: ${budget.spent():.2f} of ${settings.budget_monthly_usd:.2f} used this month")
    finally:
        ledger.close()
    if settings.calendar_enabled and settings.calendar_source == "folder":
        try:
            collection = radicale.choose(radicale.collections(Path(settings.calendar_folder), settings.calendar_user),
                                         settings.calendar_name)
            count = radicale.read_items(collection).count("BEGIN:VEVENT")
            check(True, f"Radicale calendar '{collection.displayname}' ({count} events)")
        except radicale.CalendarError as exc:
            check(False, "Radicale calendar", str(exc))
        saved = has_secret("radicale", settings.state_dir)
        check(saved, "Radicale password saved", "" if saved else "missing: run set-secret radicale (needed to add events)")
    elif settings.calendar_enabled:
        check(has_secret("calendar", settings.state_dir), "Calendar link saved")
    if settings.documents_enabled or settings.mail_enabled:
        found = pdf.version()
        check(found is not None, f"PDF reader (pypdf {found})" if found else "PDF reader",
              "" if found else f"pypdf is not installed: run {pdf.INSTALL}")
    if settings.telegram_enabled:
        check(has_secret("telegram", settings.state_dir), "Telegram bot token saved")
        check(bool(settings.telegram_chat_id), "Telegram chat_id set")
    if settings.mail_enabled:
        saved = has_secret(mailbox.SECRET, settings.state_dir)
        if settings.mail_source == "bridge":
            check(saved, "Bridge password saved", "" if saved else "missing: copy it from Bridge (your account › "
                  f"Mailbox details › Password), then run set-secret {mailbox.SECRET}")
        else:
            check(saved, "Agent inbox password saved", "" if saved else f"missing: run set-secret {mailbox.SECRET}")
        if saved:
            where = "Proton via Bridge" if settings.mail_source == "bridge" else "Agent inbox"
            try:
                detail, problem = mail.inbox_summary(settings)
                alias = f" as {settings.mail_address}" if settings.mail_address else ""
                check(True, f"{where} {settings.mail_user}{alias} ({detail})")
                if problem:
                    check(False, "Mail from you", problem)
            except mailbox.MailError as exc:
                check(False, where, str(exc))
    return 1 if failures else 0


def _probe(settings: Settings) -> int:
    ledger = Ledger(settings.state_dir)
    try:
        return _probe_with(settings, OpenRouter(settings, budget=Budget(ledger, settings.budget_monthly_usd,
                                                                         settings.budget_warn_percent)))
    finally:
        ledger.close()


def _probe_with(settings: Settings, provider: OpenRouter) -> int:
    if not provider.available():
        raise RuntimeError("Save an OpenRouter key before probing models")
    projects = [{"key": "BIOLOGY_101", "name": "Biology 101", "aliases": [], "status": "active", "area": "AREA_SCHOOL"}]
    areas = [{"key": "AREA_SCHOOL", "aliases": ["School"]}]
    print("Jev:", provider.decide("Call the dentist to schedule a cleaning"))
    print("GLM action:", provider.interpret("Call the dentist to schedule a cleaning", "Synthetic probe",
                                            projects, areas, [], []))
    print("GLM waiting:", provider.interpret("waiting for Jordan Ellis to respond to message from 9/23 (reply by 9/30)",
                                             "Synthetic probe", projects, areas, [], [], reference_date=date(2026, 9, 27)))
    print("GLM split:", provider.split("Email Sam about the budget and buy printer ink"))
    print("GLM vague check:", provider.vague_check([{"id": "a1", "text": "Handle taxes"},
                                                   {"id": "a2", "text": "Email Sam the Q3 budget draft"}]))
    print(f"Documents ({settings.document_model}):", provider.document(
        "# BIO 101 syllabus\nLab report 1 due Oct 14.\nMidterm exam: Tuesday, October 20.\n", "bio syllabus.md",
        date(2026, 9, 27), projects, areas, ["04_REFERENCE/COURSES"]))
    return 0


def _run_loop(agent: Agent, settings: Settings) -> str:
    """Run until Ctrl+C or a stop or restart request. Errors are logged and the loop carries on (runner.py)."""
    health = agent.health
    try:
        with process_lock(settings.state_dir, "runner.lock"):
            health.info(f"GTD agent started (process {os.getpid()}, Python {sys.version.split()[0]})")
            outcome = "interrupted"
            try:
                outcome = Runner(health, loop_operations(agent), settings.poll_seconds,
                                 settings.scan_interval_seconds, control=Control(settings.state_dir)).run()
            finally:
                if outcome == "stop":
                    health.info("Stop requested")
                elif outcome == "restart":
                    health.info("Restarting with the current code")
                health.info("GTD agent stopped")
            return outcome
    except RuntimeError as exc:
        if "runner.lock" not in str(exc):
            raise
        health.info("The agent is already running; this copy exits")
        raise RuntimeError("The GTD agent is already running (it starts hidden at sign-in). "
                           "Restart it: run.ps1 restart. Stop it: run.ps1 stop.") from exc


def _startup_log(config: Path, exc: BaseException) -> None:
    """A hidden agent has no window: record why it could not start, in state/logs/agent.log beside its config."""
    try:
        path = Path(config).resolve().parent / "state" / "logs" / "agent.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERROR The agent could not start: {exc}\n")
            stream.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


def _agent_running(settings: Settings) -> bool:
    try:
        with process_lock(settings.state_dir, "runner.lock"):
            return False
    except RuntimeError:
        return True


def relaunch_command(config: Path) -> list[str]:
    """The command that starts the agent with no window: pythonw on Windows, the same Python elsewhere."""
    executable = Path(sys.executable)
    if os.name == "nt" and executable.name.lower() == "python.exe" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    return [str(executable), str(HERE / "run.py"), "--config", str(config), "run"]


def start_hidden(config: Path) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(relaunch_command(config), cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                     start_new_session=os.name != "nt")


def _stop(settings: Settings) -> int:
    if not _agent_running(settings):
        print("No agent is running.")
        return 0
    Control(settings.state_dir).request("stop")
    deadline = time.monotonic() + REQUEST_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if not _agent_running(settings):
            print("Agent stopped.")
            return 0
    print("Stop requested. The agent stops after its current step (it may be waiting on a model reply).")
    return 0


def _restart(settings: Settings) -> int:
    if not _agent_running(settings):
        start_hidden(settings.config_path)
        print("Started the agent (hidden). TODAY shows when it last checked; errors go to state/logs/agent.log.")
        return 0
    request = Control(settings.state_dir).request("restart")
    deadline = time.monotonic() + REQUEST_WAIT_SECONDS
    while time.monotonic() < deadline and request.exists():
        time.sleep(0.5)
    print("Restart requested; the agent starts again hidden." if not request.exists() else
          "Restart requested. The agent restarts after its current step (it may be waiting on a model reply).")
    return 0


def _import_v1(agent: Agent, path: Path) -> int:
    print(f"Imported {agent.ledger.import_v1(path)} approved examples from version 1")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.load(args.config)
        if args.command == "doctor":
            return _doctor(settings)
        if args.command == "probe-models":
            return _probe(settings)
        if args.command == "stop":
            return _stop(settings)
        if args.command == "restart":
            return _restart(settings)
        if args.command == "export":
            path = export_project(settings, args.key, datetime.now())
            vault = Vault(settings)
            count = len(notes_for(vault, find_project(vault, args.key)))
            print(f"Exported {count} notes to {path}")
            return 0
        if args.command == "set-secret":
            secret_names(args.name)  # rejects a malformed name before asking for the value
            prompt = {"openrouter": "Paste the OpenRouter API key",
                      "telegram": "Paste the Telegram bot token from @BotFather",
                      "calendar": "Paste the private ICS link (Proton Calendar › Share › link)",
                      "radicale": "Type the Radicale calendar password",
                      mailbox.SECRET: "Paste the agent inbox's app password"}.get(
                args.name, f"Paste the secret for {args.name}")
            value = getpass.getpass(prompt + " (hidden): ")
            print(set_secret(args.name, value, settings.state_dir))
            return 0
        agent = Agent(settings)
        try:
            if args.command in ("run", "scan"):
                imported = agent.ledger.import_v1_once(settings.state_dir)
                if imported:
                    print(imported)
            if args.command == "capture":
                agent.append_to_inbox([" ".join(args.text)])
                print("Added to the Inbox")
                return 0
            if args.command == "learning":
                if args.learning_command == "forget":
                    agent.ledger.forget_learning(args.id)
                    print(f"Disabled learned example {args.id}; vault notes unchanged")
                elif args.learning_command == "import-v1":
                    return _import_v1(agent, args.path)
                else:
                    for row in agent.ledger.list_learning():
                        example = json.loads(row["example_json"])
                        print(f"{row['approval_id']}: {example.get('route')}: {example.get('lesson')}")
                return 0
            if args.command == "telegram":
                telegram = TelegramCapture(agent)
                for message in (telegram.setup() if args.action == "setup" else telegram.poll()):
                    print(message)
                return 0
            if args.command == "calendar" and args.action == "export":
                try:
                    written = radicale.export(Path(settings.calendar_folder), settings.calendar_user, args.folder)
                except radicale.CalendarError as exc:
                    print(f"CHECK: {exc}. Set [calendar] folder and user to Radicale's storage first.")
                    return 1
                for path, count in written:
                    print(f"{path.resolve()}: {count} event{'s' if count != 1 else ''}")
                print("Import each file in Proton Calendar › Settings › Import/export › Import from ICS." if written
                      else "No events found")
                return 0
            if args.command == "calendar":
                housekeeper = Housekeeper(agent)
                start = datetime.combine(date.today(), datetime.min.time())
                events, warning = housekeeper.calendar_events(start, start + timedelta(days=1))
                if warning:
                    print("CHECK:", warning)
                for event in events:
                    when = "all day" if event.all_day else f"{event.start:%H:%M}-{event.end:%H:%M}"
                    print(f"{when}  {event.summary}")
                if not events and not warning:
                    print("No events today" if settings.calendar_enabled else "Calendar is off in config.toml")
                return 0
            if args.command in {"lint", "digest", "weekly-review"}:
                housekeeper = Housekeeper(agent)
                vault = agent.vault()
                if args.command == "lint":
                    housekeeper.lint(vault)
                    if settings.mode == "approval":
                        agent.render_reviews()
                    print(read_snapshot(settings.vault_path(settings.lint_note)).text)
                elif args.command == "digest":
                    housekeeper.digest(vault)
                    print(f"Wrote {settings.today_note}")
                else:
                    housekeeper.track_tasks(vault)
                    for message in housekeeper.weekly(vault, force=True) or ["Review note unchanged (edited or current)"]:
                        print(message)
                return 0
            if args.command == "scan":
                if settings.mode == "approval":
                    for message in agent.sync_review_requests():
                        print(message)
                result = agent.scan(wait_for_quiet=False)  # run by hand: you are at the terminal, not typing
                for record in result.new_proposals:
                    proposal = record["proposal"]
                    print(f"{record['id']}: {proposal['kind']}: {proposal.get('title') or proposal.get('reason')}")
                print(f"{len(result.new_proposals)} proposals, {result.skipped} already handled")
                for error in result.errors:
                    print(f"CHECK: {error}", file=sys.stderr)
                for message in Housekeeper(agent).run():
                    print(message)
                return 1 if result.errors else 0
            if args.command == "sync-reviews":
                for message in agent.sync_review_requests():
                    print(message)
                return 0
            if args.command == "reconcile":
                for message in agent.reconcile() or ["Nothing to reconcile"]:
                    print(message)
                return 0
            if args.command == "run":
                print(f"GTD agent running. Approvals every {settings.poll_seconds}s; Inbox and housekeeping every "
                      f"{settings.scan_interval_seconds}s. Close this window or press Ctrl+C to stop.")
                try:
                    outcome = _run_loop(agent, settings)
                except KeyboardInterrupt:
                    print("Agent stopped")
                    return 0
                if outcome == "restart":
                    start_hidden(settings.config_path)
                print("Agent restarting (hidden)" if outcome == "restart" else "Agent stopped")
                return 0
            if args.command == "review":
                if args.review_command == "list":
                    for row in agent.ledger.pending():
                        proposal = json.loads(row["proposal_json"])
                        print(f"{row['id']}: {row['origin']}: {proposal['kind']}: "
                              f"{proposal.get('title') or proposal.get('reason')}")
                    return 0
                if args.review_command == "show":
                    row = agent.ledger.get(args.id)
                    if row is None:
                        raise RuntimeError("Proposal not found")
                    print(json.dumps({"id": row["id"], "status": row["status"], "origin": row["origin"],
                                      "proposal": json.loads(row["proposal_json"])}, indent=2, ensure_ascii=False))
                    return 0
                if args.review_command == "approve":
                    print(agent.approve(args.id))
                    return 0
                agent.reject(args.id)
                print("Rejected", args.id)
                return 0
        finally:
            agent.close()
    except (OSError, UnicodeError, ValueError, KeyError, RuntimeError, ProviderError, ApprovalError,
            TelegramError) as exc:
        print(f"CHECK: {exc}", file=sys.stderr)
        if args.command == "run" and "already running" not in str(exc):
            _startup_log(args.config, exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - a hidden agent must leave a trace, whatever stopped it
        if args.command != "run":
            raise
        print(f"CHECK: the agent could not start: {exc}", file=sys.stderr)
        _startup_log(args.config, exc)
        return 1
    return 0
