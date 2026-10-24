"""Durable metadata-only submission ledger. No prompt, credentials or audio is stored."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

TERMINAL = frozenset({"success", "error", "not_active", "resolved"})
STATES = TERMINAL | {"intent", "uncertain", "pending", "running"}
CLIENT_STATES = frozenset({"active", "abandoned", "delivered"})
CACHE_ELIGIBLE = frozenset({"pending_idle", "countdown", "waiting_remote"})
CACHE_STATES = CACHE_ELIGIBLE | {"releasing", "requested", "completed", "failed", "uncertain", "stale"}


def _code(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)):
        raise ValueError("invalid_error_code")


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
            CREATE TABLE IF NOT EXISTS cache_cycles (
                model TEXT PRIMARY KEY, backend_url TEXT NOT NULL, preset_id TEXT NOT NULL,
                template_version TEXT NOT NULL, config_version TEXT NOT NULL, voice TEXT NOT NULL,
                method TEXT NOT NULL, seconds INTEGER NOT NULL, use_generation INTEGER NOT NULL,
                state TEXT NOT NULL, last_use REAL NOT NULL, updated REAL NOT NULL,
                error_code TEXT, operation_id TEXT
            );
            CREATE TABLE IF NOT EXISTS cache_use_rollback (
                task_id TEXT PRIMARY KEY, previous_json TEXT
            );
            CREATE TABLE IF NOT EXISTS cache_free_operations (
                operation_id TEXT PRIMARY KEY, backend_url TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS cache_free_members (
                operation_id TEXT NOT NULL, model TEXT NOT NULL, use_generation INTEGER NOT NULL,
                PRIMARY KEY(operation_id, model)
            );
        """)
        # Additive migration: old rows remain speech, old uncertain jobs remain unresolved.
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        additions = {"purpose": "TEXT NOT NULL DEFAULT 'speech'", "model": "TEXT", "voice": "TEXT",
                     "preset_id": "TEXT", "cache_generation": "INTEGER", "deadline": "REAL",
                     "expired": "INTEGER NOT NULL DEFAULT 0"}
        with self.db:
            for name, declaration in additions.items():
                if name not in existing:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('instance_id', ?)", (str(uuid.uuid4()),))
        self.instance_id = self.db.execute("SELECT value FROM metadata WHERE key='instance_id'").fetchone()[0]

    def intent(self, *, task_id: str, request_id: str, backend_url: str,
               template_version: str, config_version: str, purpose: str = "speech",
               model: str | None = None, voice: str | None = None, preset_id: str | None = None,
               cache_generation: int | None = None, deadline: float | None = None,
               cache_use: dict | None = None) -> dict:
        # Task intent AND dirty-cycle/cleanup ownership commit before the single POST.
        uuid.UUID(task_id)
        if purpose not in {"speech", "cache_cleanup"}:
            raise ValueError("invalid_task_purpose")
        now = time.time()
        with self.db:
            if cache_use is not None:
                if purpose != "speech":
                    raise ValueError("invalid_cache_use")
                old = self.db.execute("SELECT * FROM cache_cycles WHERE model=?", (cache_use.get("model"),)).fetchone()
                self.db.execute("INSERT INTO cache_use_rollback VALUES (?,?)",
                                (task_id, json.dumps(dict(old), sort_keys=True) if old else None))
                cache_generation = self._mark_cache_use(cache_use, now)
            if purpose == "cache_cleanup":
                changed = self.db.execute("""UPDATE cache_cycles SET state='releasing',operation_id=?,updated=?,error_code=NULL
                    WHERE model=? AND use_generation=? AND state IN ('pending_idle','countdown','waiting_remote')""",
                    (task_id, now, model, cache_generation)).rowcount
                if changed != 1:
                    raise ValueError("cache_cycle_changed")
            self.db.execute("""INSERT INTO tasks
                (task_id,request_id,instance_id,backend_url,template_version,config_version,
                 remote_id,state,client_state,created,updated,purpose,model,voice,preset_id,cache_generation,deadline)
                VALUES (?,?,?,?,?,?,?,'intent','active',?,?,?,?,?,?,?,?)""",
                (task_id, request_id, self.instance_id, backend_url, template_version, config_version,
                 task_id, now, now, purpose, model, voice, preset_id, cache_generation, deadline))
        return self.get(task_id)

    def _mark_cache_use(self, metadata: dict, now: float) -> int:
        required = {"model", "backend_url", "preset_id", "template_version", "config_version", "voice", "method", "seconds"}
        if set(metadata) != required or metadata["method"] not in {"free", "workflow"}:
            raise ValueError("invalid_cache_use")
        if type(metadata["seconds"]) is not int or not 1 <= metadata["seconds"] <= 86400:
            raise ValueError("invalid_cache_use")
        for field in ("model", "voice"):
            if not isinstance(metadata[field], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", metadata[field]):
                raise ValueError("invalid_cache_use")
        uuid.UUID(metadata["preset_id"])
        old = self.db.execute("SELECT use_generation FROM cache_cycles WHERE model=?", (metadata["model"],)).fetchone()
        counter_key = 'cache_use_generation_sequence'
        counter = self.db.execute("SELECT value FROM metadata WHERE key=?", (counter_key,)).fetchone()
        generation = max(int(counter[0]) if counter else 0, old[0] if old else 0) + 1
        self.db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (counter_key, str(generation)))
        self.db.execute("""INSERT INTO cache_cycles
            (model,backend_url,preset_id,template_version,config_version,voice,method,seconds,
             use_generation,state,last_use,updated,error_code,operation_id)
            VALUES (?,?,?,?,?,?,?,?,?,'pending_idle',?,?,NULL,NULL)
            ON CONFLICT(model) DO UPDATE SET backend_url=excluded.backend_url,preset_id=excluded.preset_id,
             template_version=excluded.template_version,config_version=excluded.config_version,voice=excluded.voice,
             method=excluded.method,seconds=excluded.seconds,use_generation=excluded.use_generation,
             state='pending_idle',last_use=excluded.last_use,updated=excluded.updated,error_code=NULL,operation_id=NULL""",
            (*(metadata[key] for key in ("model", "backend_url", "preset_id", "template_version", "config_version", "voice", "method", "seconds")),
             generation, now, now))
        return generation

    def get(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("unknown_task")
        return dict(row)

    def update(self, task_id: str, **fields) -> dict:
        allowed = {"state", "client_state", "remote_id", "admitted", "delete_requested", "absent_count", "error_code", "expired"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("invalid_journal_update")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError("invalid_task_state")
        if "client_state" in fields and fields["client_state"] not in CLIENT_STATES:
            raise ValueError("invalid_client_state")
        _code(fields.get("error_code"))
        fields["updated"] = time.time()
        with self.db:
            self.db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE task_id=?", (*fields.values(), task_id))
            row = self.get(task_id)
            self._sync_cleanup_cycle(row)
            if row['purpose'] == 'speech' and row['state'] == 'error' and row['error_code'] == 'submission_rejected':
                self._rollback_rejected_use(row)
        return row

    def _rollback_rejected_use(self, row):
        backup = self.db.execute('SELECT previous_json FROM cache_use_rollback WHERE task_id=?', (row['task_id'],)).fetchone()
        current = self.db.execute('SELECT use_generation FROM cache_cycles WHERE model=?', (row['model'],)).fetchone()
        if backup is None or current is None or current[0] != row['cache_generation']:
            return
        if backup[0] is None:
            self.db.execute('DELETE FROM cache_cycles WHERE model=? AND use_generation=?', (row['model'], row['cache_generation']))
            return
        previous = json.loads(backup[0])  # Only our own metadata-only cycle snapshot is stored here.
        if previous['state'] in CACHE_ELIGIBLE:
            previous['state'] = 'pending_idle'
        fields = ('backend_url', 'preset_id', 'template_version', 'config_version', 'voice', 'method', 'seconds',
                  'use_generation', 'state', 'last_use', 'updated', 'error_code', 'operation_id')
        self.db.execute('UPDATE cache_cycles SET ' + ','.join(name + '=?' for name in fields) + ' WHERE model=?',
                        (*(previous[name] for name in fields), row['model']))

    def allocate_cleanup_seed(self) -> int:
        # The first write takes SQLite's writer lock before the read. Separate
        # connections cannot allocate the same value; crashes may burn, never reuse.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('cleanup_seed_sequence','0')")
            value = self.db.execute("SELECT value FROM metadata WHERE key='cleanup_seed_sequence'").fetchone()[0]
            if not re.fullmatch(r'0|[1-9][0-9]{0,9}', value) or not 0 <= int(value) < 4294967295:
                raise ValueError('cache_seed_exhausted')
            seed = int(value) + 1
            self.db.execute("UPDATE metadata SET value=? WHERE key='cleanup_seed_sequence'", (str(seed),))
        return seed

    def _sync_cleanup_cycle(self, row: dict) -> None:
        # Runs inside the SAME transaction as the task transition, including admin resolution.
        if row["purpose"] != "cache_cleanup":
            return
        mapping = {"success": ("completed", None), "error": ("failed", "cache_cleanup_failed"),
                   "not_active": ("failed", "cache_not_active"), "resolved": ("uncertain", "cache_admin_resolved"),
                   "uncertain": ("uncertain", "submission_uncertain")}
        state, code = mapping.get(row["state"], ("releasing", None))
        if row["expired"] and row["state"] not in TERMINAL:
            code = "cache_deadline_exceeded"
        self.db.execute("""UPDATE cache_cycles SET state=?,error_code=?,updated=?
            WHERE model=? AND use_generation=? AND operation_id=? AND state!='stale'""",
            (state, code, time.time(), row["model"], row["cache_generation"], row["task_id"]))

    def cache_cycles(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM cache_cycles ORDER BY model")]

    def set_cache_state(self, model: str, generation: int, state: str, error_code: str | None = None):
        if state not in CACHE_STATES:
            raise ValueError("invalid_cache_state")
        _code(error_code)
        with self.db:
            self.db.execute("UPDATE cache_cycles SET state=?,error_code=?,updated=? WHERE model=? AND use_generation=?",
                            (state, error_code, time.time(), model, generation))

    def free_intent(self, operation_id: str, backend_url: str, members: list[dict]) -> None:
        uuid.UUID(operation_id)
        if not members:
            raise ValueError("empty_cache_operation")
        now = time.time()
        with self.db:
            self.db.execute("INSERT INTO cache_free_operations VALUES (?,?, 'intent',?,?,NULL)", (operation_id, backend_url, now, now))
            for row in members:
                changed = self.db.execute("""UPDATE cache_cycles SET state='releasing',operation_id=?,updated=?,error_code=NULL
                    WHERE model=? AND use_generation=? AND backend_url=? AND method='free'
                    AND state IN ('pending_idle','countdown','waiting_remote')""",
                    (operation_id, now, row["model"], row["use_generation"], backend_url)).rowcount
                if changed != 1:
                    raise ValueError("cache_cycle_changed")
                self.db.execute("INSERT INTO cache_free_members VALUES (?,?,?)", (operation_id, row["model"], row["use_generation"]))

    def finish_free(self, operation_id: str, state: str, error_code: str | None = None):
        if state not in {"requested", "failed", "uncertain"}:
            raise ValueError("invalid_free_state")
        _code(error_code)
        with self.db:
            changed = self.db.execute("UPDATE cache_free_operations SET state=?,error_code=?,updated=? WHERE operation_id=? AND state='intent'",
                                      (state, error_code, time.time(), operation_id)).rowcount
            if changed != 1:
                return  # A terminal response/uncertainty cannot be rewritten by a late callback.
            # Generation+operation ownership prevents late completion from clearing new use.
            self.db.execute("""UPDATE cache_cycles SET state=?,error_code=?,updated=? WHERE operation_id=?
                AND EXISTS (SELECT 1 FROM cache_free_members m WHERE m.operation_id=?
                            AND m.model=cache_cycles.model AND m.use_generation=cache_cycles.use_generation)""",
                (state, error_code, time.time(), operation_id, operation_id))

    def recover_free(self):
        # /free has no prompt history. A crashed intent is never replayed or permanent-gated.
        for row in self.db.execute("SELECT operation_id FROM cache_free_operations WHERE state='intent'").fetchall():
            self.finish_free(row[0], "uncertain", "cache_free_uncertain")

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
            self._sync_cleanup_cycle(self.get(task_id))

    def prune(self, protected_ids=()) -> None:
        marks = ",".join("?" for _ in TERMINAL)
        protected = tuple(protected_ids)
        guard = " AND task_id NOT IN (" + ",".join("?" for _ in protected) + ")" if protected else ""
        with self.db:
            self.db.execute(f"DELETE FROM tasks WHERE state IN ({marks}) AND updated<?" + guard,
                            (*TERMINAL, time.time() - 7 * 86400, *protected))
            self.db.execute(f"DELETE FROM tasks WHERE task_id IN (SELECT task_id FROM tasks WHERE state IN ({marks})" + guard + " ORDER BY updated DESC LIMIT -1 OFFSET 1000)", (*TERMINAL, *protected))
            self.db.execute("DELETE FROM audit WHERE task_id NOT IN (SELECT task_id FROM tasks)")
            self.db.execute("DELETE FROM cache_use_rollback WHERE task_id NOT IN (SELECT task_id FROM tasks)")
            self.db.execute("""DELETE FROM cache_free_operations WHERE state!='intent'
                AND operation_id NOT IN (SELECT operation_id FROM cache_cycles WHERE operation_id IS NOT NULL)
                AND (updated<? OR operation_id IN (SELECT operation_id FROM cache_free_operations ORDER BY updated DESC LIMIT -1 OFFSET 1000))""",
                (time.time() - 7 * 86400,))
            self.db.execute("DELETE FROM cache_free_members WHERE operation_id NOT IN (SELECT operation_id FROM cache_free_operations)")

    def prune_retired_cache(self, configured_models):
        configured = tuple(configured_models)
        guard = ' AND model NOT IN (' + ','.join('?' for _ in configured) + ')' if configured else ''
        eligible = """state='stale' AND NOT EXISTS
            (SELECT 1 FROM tasks WHERE tasks.task_id=cache_cycles.operation_id
             AND tasks.state NOT IN ('success','error','not_active','resolved'))
            AND NOT EXISTS (SELECT 1 FROM cache_free_operations f
                            WHERE f.operation_id=cache_cycles.operation_id AND f.state='intent')""" + guard
        with self.db:
            self.db.execute('DELETE FROM cache_cycles WHERE ' + eligible + ' AND updated<?',
                            (*configured, time.time() - 7 * 86400))
            self.db.execute('DELETE FROM cache_cycles WHERE model IN (SELECT model FROM cache_cycles WHERE '
                            + eligible + ' ORDER BY updated DESC LIMIT -1 OFFSET 1000)', configured)

    def close(self) -> None:
        self.db.close()
