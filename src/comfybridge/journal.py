"""Durable metadata-only submission ledger. No prompt, credentials or audio is stored."""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

TERMINAL = frozenset({"success", "error", "not_active", "resolved"})
STATES = TERMINAL | {"intent", "uncertain", "pending", "running"}
CLIENT_STATES = frozenset({"active", "abandoned", "delivered"})


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                backend_url TEXT NOT NULL, template_version TEXT NOT NULL,
                config_version TEXT NOT NULL, remote_id TEXT NOT NULL,
                state TEXT NOT NULL, client_state TEXT NOT NULL,
                admitted INTEGER NOT NULL DEFAULT 0, delete_requested INTEGER NOT NULL DEFAULT 0,
                absent_count INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                task_id TEXT NOT NULL, event TEXT NOT NULL, created REAL NOT NULL
            );
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('instance_id', ?)", (str(uuid.uuid4()),))
        self.instance_id = self.db.execute("SELECT value FROM metadata WHERE key='instance_id'").fetchone()[0]

    def intent(self, *, task_id: str, request_id: str, backend_url: str,
               template_version: str, config_version: str) -> dict:
        # This transaction commits synchronously before the caller may perform a POST.
        uuid.UUID(task_id)
        now = time.time()
        with self.db:
            self.db.execute("""INSERT INTO tasks
                (task_id,request_id,instance_id,backend_url,template_version,config_version,
                 remote_id,state,client_state,created,updated)
                VALUES (?,?,?,?,?,?,?,'intent','active',?,?)""",
                (task_id, request_id, self.instance_id, backend_url, template_version,
                 config_version, task_id, now, now))
        return self.get(task_id)

    def get(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("unknown_task")
        return dict(row)

    def update(self, task_id: str, **fields) -> dict:
        allowed = {"state", "client_state", "remote_id", "admitted", "delete_requested", "absent_count", "error_code"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("invalid_journal_update")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError("invalid_task_state")
        if "client_state" in fields and fields["client_state"] not in CLIENT_STATES:
            raise ValueError("invalid_client_state")
        if fields.get("error_code") is not None:
            code = fields["error_code"]
            if not isinstance(code, str) or len(code) > 64 or not all(c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in code):
                raise ValueError("invalid_error_code")
        fields["updated"] = time.time()
        with self.db:
            self.db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE task_id=?", (*fields.values(), task_id))
        return self.get(task_id)

    def known_task_ids(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT task_id FROM tasks")]

    def unresolved(self) -> list[dict]:
        marks = ",".join("?" for _ in TERMINAL)
        return [dict(r) for r in self.db.execute(f"SELECT * FROM tasks WHERE state NOT IN ({marks}) ORDER BY created", tuple(TERMINAL))]

    def acknowledge(self, task_id: str) -> None:
        row = self.get(task_id)
        if row["state"] not in {"uncertain", "intent"}:
            raise ValueError("task_not_uncertain")
        with self.db:
            self.db.execute("UPDATE tasks SET state='resolved',client_state='abandoned',updated=?,error_code='admin_acknowledged' WHERE task_id=?", (time.time(), task_id))
            self.db.execute("INSERT INTO audit VALUES (?,?,?)", (task_id, "uncertain_acknowledged", time.time()))

    def prune(self, protected_ids=()) -> None:
        # Nonterminal records are never aged out. Live audio callbacks still need their row.
        marks = ",".join("?" for _ in TERMINAL)
        protected = tuple(protected_ids)
        guard = " AND task_id NOT IN (" + ",".join("?" for _ in protected) + ")" if protected else ""
        with self.db:
            self.db.execute(f"DELETE FROM tasks WHERE state IN ({marks}) AND updated<?" + guard,
                            (*TERMINAL, time.time() - 7 * 86400, *protected))
            self.db.execute(f"DELETE FROM tasks WHERE task_id IN (SELECT task_id FROM tasks WHERE state IN ({marks})" + guard + " ORDER BY updated DESC LIMIT -1 OFFSET 1000)", (*TERMINAL, *protected))
            self.db.execute("DELETE FROM audit WHERE task_id NOT IN (SELECT task_id FROM tasks)")

    def close(self) -> None:
        self.db.close()
