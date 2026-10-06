"""Private SQLite ledger: proposals, undo records, learned examples, task history."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DB_NAME = "gtd-v2.sqlite3"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / DB_NAME
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                origin TEXT NOT NULL,
                dedupe_key TEXT UNIQUE NOT NULL,
                capture_id TEXT,
                source_hash TEXT,
                proposal_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cleared_captures (
                capture_id TEXT PRIMARY KEY, cleared_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS undo_records (
                id TEXT PRIMARY KEY, origin_type TEXT NOT NULL, origin_id TEXT NOT NULL,
                summary TEXT NOT NULL, changes_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(origin_type, origin_id)
            );
            CREATE TABLE IF NOT EXISTS learned_cases (
                approval_id TEXT PRIMARY KEY, example_json TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback_requests (
                id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                status TEXT NOT NULL, message TEXT NOT NULL, result_json TEXT,
                created_at TEXT NOT NULL, UNIQUE(approval_id, request_hash)
            );
            CREATE TABLE IF NOT EXISTS task_seen (
                task_key TEXT PRIMARY KEY, path TEXT NOT NULL, text TEXT NOT NULL,
                first_seen TEXT NOT NULL, first_open TEXT, done_seen TEXT
            );
            CREATE TABLE IF NOT EXISTS next_state (
                project_key TEXT PRIMARY KEY, open_next_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, month TEXT NOT NULL, job TEXT NOT NULL,
                model TEXT NOT NULL, cost REAL NOT NULL, cost_known INTEGER NOT NULL, tokens_in INTEGER,
                tokens_out INTEGER
            );
            CREATE INDEX IF NOT EXISTS model_calls_month ON model_calls(month);
            CREATE TABLE IF NOT EXISTS watched (
                path TEXT PRIMARY KEY, text TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    # ------------------------------------------------------------ proposals
    def get(self, proposal_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()

    def get_by_key(self, dedupe_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM proposals WHERE dedupe_key=?", (dedupe_key,)).fetchone()

    def for_capture(self, capture_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM proposals WHERE capture_id=? ORDER BY dedupe_key", (capture_id,)))

    def upsert(self, origin: str, dedupe_key: str, proposal: dict[str, Any], *, capture_id: str | None = None,
               source_hash: str | None = None, status: str = "pending") -> str:
        """Create a proposal, or replace an existing one under a fresh ID (old checkboxes cannot apply it)."""
        proposal_id = uuid.uuid4().hex[:12]
        payload = json.dumps(proposal, ensure_ascii=False)
        with self.connection:
            existing = self.get_by_key(dedupe_key)
            if existing is None:
                self.connection.execute(
                    "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?)",
                    (proposal_id, origin, dedupe_key, capture_id, source_hash, payload, status, _now(), _now()))
            else:
                self.connection.execute(
                    "UPDATE proposals SET id=?, origin=?, capture_id=?, source_hash=?, proposal_json=?, status=?, "
                    "updated_at=? WHERE dedupe_key=?",
                    (proposal_id, origin, capture_id, source_hash, payload, status, _now(), dedupe_key))
        return proposal_id

    def update_json(self, proposal_id: str, proposal: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute("UPDATE proposals SET proposal_json=?, updated_at=? WHERE id=?",
                                    (json.dumps(proposal, ensure_ascii=False), _now(), proposal_id))

    def set_status(self, proposal_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE proposals SET status=?, updated_at=? WHERE id=?",
                                    (status, _now(), proposal_id))
            if status == "committed":
                row = self.get(proposal_id)
                example = json.loads(row["proposal_json"]).get("learning") if row else None
                if example:
                    self.connection.execute("INSERT OR IGNORE INTO learned_cases VALUES (?,?,1,?)",
                                            (proposal_id, json.dumps(example, ensure_ascii=False), _now()))

    def list(self, status: str, origin: str | None = None) -> list[sqlite3.Row]:
        if origin:
            return list(self.connection.execute(
                "SELECT * FROM proposals WHERE status=? AND origin=? ORDER BY created_at, dedupe_key", (status, origin)))
        return list(self.connection.execute(
            "SELECT * FROM proposals WHERE status=? ORDER BY created_at, dedupe_key", (status,)))

    def pending(self) -> list[sqlite3.Row]:
        return self.list("pending")

    def count_pending(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]

    def replace_after_feedback(self, proposal_id: str, request_id: str, proposal: dict[str, Any]) -> str:
        revised = uuid.uuid4().hex[:12]
        with self.connection:
            result = self.connection.execute(
                "UPDATE proposals SET id=?, proposal_json=?, updated_at=? WHERE id=? AND status='pending'",
                (revised, json.dumps(proposal, ensure_ascii=False), _now(), proposal_id))
            if result.rowcount != 1:
                raise RuntimeError("Original proposal is no longer pending")
            self.connection.execute("UPDATE feedback_requests SET status='done', message=? WHERE id=?",
                                    (f"Revised proposal: {revised}", request_id))
        return revised

    # ------------------------------------------------------------ captures
    def was_cleared(self, capture_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM cleared_captures WHERE capture_id=?",
                                       (capture_id,)).fetchone() is not None

    def mark_cleared(self, capture_id: str) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO cleared_captures VALUES (?,?)", (capture_id, _now()))

    def forget_cleared(self, capture_id: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM cleared_captures WHERE capture_id=?", (capture_id,))

    # ------------------------------------------------------------ learning
    def list_learning(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM learned_cases WHERE active=1 ORDER BY created_at DESC"))

    def forget_learning(self, approval_id: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE learned_cases SET active=0 WHERE approval_id=?", (approval_id,))

    def import_learning(self, approval_id: str, example: dict[str, Any], created_at: str) -> None:
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO learned_cases VALUES (?,?,1,?)",
                                    (approval_id, json.dumps(example, ensure_ascii=False), created_at))

    def import_v1(self, path: Path) -> int:
        """Copy approved lessons from the version 1 ledger (read only). Safe to repeat."""
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        count = 0
        try:
            for row in source.execute("SELECT * FROM learned_cases WHERE active=1"):
                example = json.loads(row["example_json"])
                if example.get("route") == "existing_project":
                    example["route"] = "project_note"
                self.import_learning(f"v1-{row['approval_id']}", example, row["created_at"])
                count += 1
        finally:
            source.close()
        return count

    def import_v1_once(self, state_dir: Path) -> str | None:
        """First run only: bring version 1 lessons across so the agent keeps what you taught it."""
        if self.kv_get("v1_imported"):
            return None
        old = state_dir / "agent.sqlite3"
        message = None
        if old.exists():
            try:
                message = f"Imported {self.import_v1(old)} learned examples from version 1"
            except Exception as exc:  # best effort: never block startup
                message = (f"Could not read version 1 lessons ({type(exc).__name__}: {exc}). "
                           f"Run `learning import-v1 state/agent.sqlite3` later")
        self.kv_set("v1_imported", _now())
        return message

    def learning_examples(self, capture: str, limit: int = 5) -> list[dict[str, Any]]:
        def words(value: str) -> set[str]:
            return set(re.findall(r"[a-z0-9]+", value.lower())) - {
                "i", "a", "the", "to", "and", "for", "of", "with", "in", "my", "it", "is"}
        query = words(capture)
        ranked = []
        for row in self.connection.execute(
                "SELECT example_json FROM learned_cases WHERE active=1 ORDER BY created_at DESC LIMIT 200"):
            example = json.loads(row["example_json"])
            terms = words(example.get("capture", "") + " " + example.get("lesson", ""))
            overlap = query & terms
            if overlap:
                ranked.append((len(overlap) / max(1, len(query | terms)), example))
        ranked.sort(key=lambda item: -item[0])
        return [example for _, example in ranked[:limit]]

    # ------------------------------------------------------------ feedback
    def feedback_request(self, approval_id: str, request_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM feedback_requests WHERE approval_id=? AND request_hash=?",
            (approval_id, request_hash)).fetchone()

    def latest_feedback(self, approval_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM feedback_requests WHERE approval_id=? ORDER BY rowid DESC LIMIT 1",
            (approval_id,)).fetchone()

    def begin_feedback(self, approval_id: str, request_hash: str) -> str:
        identity = uuid.uuid4().hex[:12]
        with self.connection:
            self.connection.execute(
                "INSERT INTO feedback_requests VALUES (?,?,?,'processing',?,NULL,?)",
                (identity, approval_id, request_hash,
                 "Submitted. If interrupted, edit the correction and check Teacher feedback to retry.", _now()))
        return identity

    def finish_feedback(self, request_id: str, status: str, message: str, proposal: dict | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE feedback_requests SET status=?, message=?, result_json=? WHERE id=?",
                (status, message, json.dumps(proposal, ensure_ascii=False) if proposal else None, request_id))

    # ------------------------------------------------------------ events and undo
    def event(self, kind: str, payload: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO events VALUES (?,?,?,?)",
                                    (uuid.uuid4().hex, kind, json.dumps(payload, ensure_ascii=False), _now()))

    def events_since(self, since: str) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM events WHERE created_at>=? ORDER BY created_at",
                                            (since,)))

    def add_undo(self, origin_type: str, origin_id: str, summary: str, changes: list[dict[str, Any]]) -> str:
        identity = uuid.uuid4().hex[:12]
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO undo_records VALUES (?,?,?,?,?,?,?)",
                                    (identity, origin_type, origin_id, summary,
                                     json.dumps(changes, ensure_ascii=False), "available", _now()))
        return identity

    def list_undo(self, limit: int = 8) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM undo_records WHERE status='available' ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,)))

    def get_undo(self, undo_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM undo_records WHERE id=?", (undo_id,)).fetchone()

    def list_undo_attention(self, limit: int = 8) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM undo_records WHERE status IN ('stale','interrupted') ORDER BY created_at DESC LIMIT ?",
            (limit,)))

    def undo_status(self, undo_id: str, status: str) -> None:
        record = self.get_undo(undo_id)
        if record is None:
            raise ValueError("Unknown Undo record")
        with self.connection:
            self.connection.execute("UPDATE undo_records SET status=? WHERE id=?", (status, undo_id))
            if status == "undone" and record["origin_type"] == "proposal":
                self.connection.execute("UPDATE learned_cases SET active=0 WHERE approval_id=?",
                                        (record["origin_id"],))

    def supersede_undo(self, origin_type: str, prefix: str) -> None:
        """Keep only the newest available Undo of one series, such as rewrites of one project's Current state."""
        rows = list(self.connection.execute(
            "SELECT id FROM undo_records WHERE status='available' AND origin_type=? AND substr(origin_id, 1, ?) = ? "
            "ORDER BY created_at DESC, rowid DESC", (origin_type, len(prefix), prefix)))
        with self.connection:
            for row in rows[1:]:
                self.connection.execute("UPDATE undo_records SET status='expired' WHERE id=?", (row["id"],))

    def expire_undo(self, keep: int = 30) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE undo_records SET status='expired' WHERE status='available' AND id NOT IN "
                "(SELECT id FROM undo_records WHERE status='available' ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (keep,))

    # ------------------------------------------------------------ task history
    def seen_rows(self) -> dict[str, sqlite3.Row]:
        return {row["task_key"]: row for row in self.connection.execute("SELECT * FROM task_seen")}

    def record_seen(self, updates: list[tuple[str, str, str, str, str | None, str | None]]) -> None:
        """updates: (task_key, path, text, first_seen, first_open, done_seen)."""
        with self.connection:
            self.connection.executemany(
                "INSERT INTO task_seen VALUES (?,?,?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET "
                "path=excluded.path, text=excluded.text, "
                "first_open=COALESCE(task_seen.first_open, excluded.first_open), "
                "done_seen=COALESCE(task_seen.done_seen, excluded.done_seen)", updates)

    def next_state(self) -> dict[str, list[str]]:
        return {row["project_key"]: json.loads(row["open_next_json"])
                for row in self.connection.execute("SELECT * FROM next_state")}

    def save_next_state(self, state: dict[str, list[str]]) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM next_state")
            self.connection.executemany("INSERT INTO next_state VALUES (?,?,?)",
                                        [(key, json.dumps(value), _now()) for key, value in state.items()])

    # ------------------------------------------------------------ watched notes
    def watched(self, path: str) -> str | None:
        """The text of a watched note as the agent last read it (your edits since then are unread)."""
        row = self.connection.execute("SELECT text FROM watched WHERE path=?", (path,)).fetchone()
        return None if row is None else row["text"]

    def set_watched(self, path: str, text: str) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO watched VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET text=excluded.text, "
                                    "updated_at=excluded.updated_at", (path, text, _now()))

    # ------------------------------------------------------------ model costs
    def record_model_call(self, month: str, job: str, model: str, cost: float, known: bool,
                          tokens_in: int | None, tokens_out: int | None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO model_calls (at, month, job, model, cost, cost_known, tokens_in, tokens_out) "
                "VALUES (?,?,?,?,?,?,?,?)", (_now(), month, job, model, cost, 1 if known else 0, tokens_in, tokens_out))

    def model_spend(self, month: str) -> float:
        return float(self.connection.execute("SELECT COALESCE(SUM(cost), 0) FROM model_calls WHERE month=?",
                                             (month,)).fetchone()[0])

    def unknown_cost_calls(self, month: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM model_calls WHERE month=? AND cost_known=0",
                                           (month,)).fetchone()[0])

    # ------------------------------------------------------------ key/value
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_items(self, prefix: str) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.connection.execute(
            "SELECT key, value FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))}

    def kv_set(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, value, _now()))
