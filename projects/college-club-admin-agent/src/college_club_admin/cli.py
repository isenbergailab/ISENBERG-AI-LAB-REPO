"""Private runner commands. All public publication remains presidential."""

import argparse
import json
import sys
from pathlib import Path

from .campaigns import build_campaign, write_package
from .ledger import save_campaign, transition

ASSETS = Path(__file__).resolve().parents[2] / "assets"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="college-club-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft", help="Generate reviewable campaign package")
    draft.add_argument("--kind", choices=("weekly", "merge", "recap", "monthly"), required=True)
    draft.add_argument("--input", type=Path, required=True)
    draft.add_argument("--output", type=Path, required=True)
    draft.add_argument("--model", action="store_true", help="Use only Qwen3.5 Flash; retry once, then skip")
    draft.add_argument("--upload", action="store_true", help="Upload package into private Drive")
    state = commands.add_parser("transition", help="Advance campaign after presidential action")
    state.add_argument("package", type=Path)
    state.add_argument("state", choices=("awaiting_approval", "approved", "scheduled", "published", "skipped", "archived"))
    state.add_argument("--actor", default="")
    state.add_argument("--x-permalink", default="")
    approval = commands.add_parser("request-approval", help="Post private Slack buttons")
    approval.add_argument("package", type=Path)
    topic = commands.add_parser("topic-thread", help="Post Monday prompts")
    topic.add_argument("--date", required=True)
    transcript = commands.add_parser("transcript", help="Produce private summary proposal")
    transcript.add_argument("file", type=Path)
    transcript.add_argument("--output", type=Path, required=True)
    retention = commands.add_parser("retention", help="Inspect or apply private Drive retention")
    retention.add_argument("--apply", action="store_true")
    zoom = commands.add_parser("zoom-import", help="Import authorized cloud recording")
    zoom.add_argument("meeting_uuid")
    scan = commands.add_parser("scan-transcripts", help="Screen My Notes exports from private Drive")
    scan.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "draft":
            brief = _read(args.input)
            campaign = build_campaign(args.kind, brief)
            if args.model:
                from .model import draft_hook
                hook = draft_hook({"kind": args.kind, "title": campaign["title"],
                                   "claims": [s["claim"] for s in campaign["sources"]]})
                campaign = build_campaign(args.kind, brief, model_hook=hook)
            folder = write_package(campaign, args.output, ASSETS)
            if args.upload:
                from .drive import upload_package
                campaign["private_package_url"] = upload_package(folder)
            save_campaign(folder, campaign)
            print(folder)
        elif args.command == "transition":
            file = args.package / "campaign.json"
            campaign = transition(_read(file), args.state, actor=args.actor, permalink=args.x_permalink)
            save_campaign(args.package, campaign)
            print(f"{campaign['id']}: {campaign['state']}")
        elif args.command == "request-approval":
            from .slack import request_approval
            campaign = _read(args.package / "campaign.json")
            if campaign["state"] != "draft":
                raise ValueError("Only fresh drafts can request approval")
            request_approval(campaign)
            save_campaign(args.package, transition(campaign, "awaiting_approval"))
            print(campaign["id"])
        elif args.command == "topic-thread":
            from .slack import monday_thread
            monday_thread(args.date)
        elif args.command == "transcript":
            from .transcripts import review_transcript
            args.output.mkdir(parents=True, exist_ok=True)
            result = review_transcript(args.file.read_text(encoding="utf-8"))
            output = args.output / f"{args.file.stem}-review.json"
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(output)
        elif args.command == "retention":
            from .drive import enforce_retention
            for entry in enforce_retention(apply=args.apply):
                print(entry)
        elif args.command == "zoom-import":
            from .zoom import import_recording
            for file_id in import_recording(args.meeting_uuid):
                print(file_id)
        elif args.command == "scan-transcripts":
            from .drive import scan_transcripts
            for review in scan_transcripts(args.output):
                print(review)
    except Exception as error:
        print(f"Campaign skipped or command failed: {error}", file=sys.stderr)
        return 1
    return 0
