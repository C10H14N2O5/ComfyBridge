import sqlite3
import time
import uuid

import pytest

from comfybridge.journal import Journal


def add(journal, **kwargs):
    values = dict(task_id=str(uuid.uuid4()), request_id=str(uuid.uuid4()), backend_url="http://mock.invalid", template_version="a" * 64, config_version="b" * 64)
    values.update(kwargs)
    return journal.intent(**values)


def test_intent_committed_before_return_and_restart(tmp_path):
    path = tmp_path / "runtime" / "tasks.sqlite3"
    journal = Journal(path)
    row = add(journal)
    second = sqlite3.connect(path)
    assert second.execute("SELECT state FROM tasks").fetchone() == ("intent",)
    second.close()
    instance = journal.instance_id
    journal.close()
    journal = Journal(path)
    assert journal.instance_id == instance
    assert journal.unresolved()[0]["task_id"] == row["task_id"]
    journal.close()


def test_metadata_only_and_explicit_acknowledgment(tmp_path):
    journal = Journal(tmp_path / "tasks.sqlite3")
    row = add(journal)
    with pytest.raises(ValueError):
        journal.update(row["task_id"], input="secret")
    with pytest.raises(ValueError):
        journal.update(row["task_id"], error_code="RAW ERROR input=secret")
    journal.update(row["task_id"], state="uncertain")
    journal.acknowledge(row["task_id"])
    assert journal.get(row["task_id"])["state"] == "resolved"
    assert not journal.unresolved()
    assert journal.db.execute("SELECT event FROM audit").fetchone()[0] == "uncertain_acknowledged"
    journal.close()


def test_prune_never_removes_nonterminal(tmp_path):
    journal = Journal(tmp_path / "tasks.sqlite3")
    active = add(journal)
    terminal = add(journal)
    journal.update(terminal["task_id"], state="success")
    with journal.db:
        journal.db.execute("UPDATE tasks SET updated=?", (time.time() - 8 * 86400,))
    journal.prune()
    assert journal.get(active["task_id"])["state"] == "intent"
    with pytest.raises(KeyError):
        journal.get(terminal["task_id"])
    journal.close()


def test_terminal_retention_count(tmp_path):
    journal = Journal(tmp_path / "tasks.sqlite3")
    active = add(journal)
    for _ in range(1002):
        row = add(journal)
        journal.update(row["task_id"], state="error")
    journal.prune()
    assert journal.db.execute("SELECT count(*) FROM tasks WHERE state='error'").fetchone()[0] == 1000
    assert journal.get(active["task_id"])["state"] == "intent"
    journal.close()
