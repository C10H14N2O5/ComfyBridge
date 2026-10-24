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


def cache_metadata(**updates):
    metadata = dict(model='model', voice='voice', backend_url='http://mock.invalid',
                    preset_id=str(uuid.uuid4()), template_version='a' * 64, config_version='b' * 64,
                    method='workflow', seconds=10)
    metadata.update(updates)
    return metadata


def business(journal, metadata, **kwargs):
    return add(journal, model=metadata['model'], voice=metadata['voice'], preset_id=metadata['preset_id'], cache_use=metadata, **kwargs)


def cleanup(journal, metadata, generation=1):
    return add(journal, purpose='cache_cleanup', model=metadata['model'], voice=metadata['voice'],
               preset_id=metadata['preset_id'], cache_generation=generation, deadline=time.time() + 100)


def test_additive_migration_preserves_legacy_unresolved_speech(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(path)
    task_id, instance_id = str(uuid.uuid4()), str(uuid.uuid4())
    connection.executescript('''
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            backend_url TEXT NOT NULL, template_version TEXT NOT NULL, config_version TEXT NOT NULL,
            remote_id TEXT NOT NULL, state TEXT NOT NULL, client_state TEXT NOT NULL,
            admitted INTEGER NOT NULL DEFAULT 0, delete_requested INTEGER NOT NULL DEFAULT 0,
            absent_count INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, error_code TEXT
        );
    ''')
    with connection:
        connection.execute("INSERT INTO metadata VALUES ('instance_id',?)", (instance_id,))
        connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,'uncertain','abandoned',0,0,0,1,1,NULL)",
                           (task_id, str(uuid.uuid4()), instance_id, 'http://old.invalid', 'a' * 64, 'b' * 64, task_id))
    connection.close()
    journal = Journal(path)
    row = journal.get(task_id)
    assert journal.instance_id == instance_id
    assert row['purpose'] == 'speech' and row['state'] == 'uncertain'
    assert row['backend_url'] == 'http://old.invalid' and row['expired'] == 0
    journal.prune()
    assert len(journal.unresolved()) == 1 and journal.cache_cycles() == []
    journal.close()
    Journal(path).close()  # repeated migration is harmless


def test_business_intent_and_dirty_generation_are_one_transaction(tmp_path):
    journal = Journal(tmp_path / 'tasks.sqlite3')
    metadata = cache_metadata()
    first = business(journal, metadata)
    assert journal.cache_cycles()[0]['use_generation'] == first['cache_generation'] == 1
    with pytest.raises(sqlite3.IntegrityError):
        business(journal, metadata, task_id=first['task_id'])
    assert journal.cache_cycles()[0]['use_generation'] == 1
    journal.close()


def test_cleanup_terminal_and_cycle_commit_atomically_without_callback(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    journal = Journal(path)
    metadata = cache_metadata()
    business(journal, metadata)
    task = cleanup(journal, metadata)
    journal.db.execute("""CREATE TRIGGER block_cycle BEFORE UPDATE ON cache_cycles
        WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        journal.update(task['task_id'], state='success')
    assert journal.get(task['task_id'])['state'] == 'intent'
    assert journal.cache_cycles()[0]['state'] == 'releasing'
    journal.db.execute('DROP TRIGGER block_cycle')
    journal.update(task['task_id'], state='success')
    journal.close()  # simulate crash before any in-memory scheduler finalization
    recovered = Journal(path)
    assert recovered.get(task['task_id'])['state'] == 'success'
    assert recovered.cache_cycles()[0]['state'] == 'completed'
    recovered.close()


def test_cleanup_result_and_admin_resolution_cannot_clear_new_generation(tmp_path):
    journal = Journal(tmp_path / 'tasks.sqlite3')
    metadata = cache_metadata()
    business(journal, metadata)
    task = cleanup(journal, metadata)
    journal.update(task['task_id'], state='uncertain')
    journal.acknowledge(task['task_id'])
    assert journal.cache_cycles()[0]['state'] == 'uncertain'
    assert journal.cache_cycles()[0]['error_code'] == 'cache_admin_resolved'
    business(journal, metadata)
    journal.update(task['task_id'], state='success')
    assert journal.cache_cycles()[0]['state'] == 'pending_idle'
    assert journal.cache_cycles()[0]['use_generation'] == 2
    journal.close()


def test_free_members_intent_is_atomic_and_coalesced_result_is_guarded(tmp_path):
    journal = Journal(tmp_path / 'tasks.sqlite3')
    first, second = cache_metadata(method='free'), cache_metadata(model='second', method='free')
    business(journal, first)
    business(journal, second)
    rows = journal.cache_cycles()
    operation_id = str(uuid.uuid4())
    invalid = [rows[0], {**rows[1], 'use_generation': 999}]
    with pytest.raises(ValueError, match='cache_cycle_changed'):
        journal.free_intent(operation_id, first['backend_url'], invalid)
    assert all(row['state'] == 'pending_idle' for row in journal.cache_cycles())
    assert not journal.db.execute('SELECT * FROM cache_free_operations').fetchall()
    journal.free_intent(operation_id, first['backend_url'], rows)
    business(journal, first)  # hypothetical new use must not be cleared by an old callback
    journal.finish_free(operation_id, 'uncertain', 'cache_free_uncertain')
    journal.finish_free(operation_id, 'requested')  # first terminal result wins
    states = {row['model']: row for row in journal.cache_cycles()}
    assert states['model']['state'] == 'pending_idle' and states['model']['use_generation'] > rows[0]['use_generation']
    assert states['second']['state'] == 'uncertain'
    journal.close()


def test_free_terminal_result_atomic_and_crash_intent_becomes_unknown_not_retry(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    journal = Journal(path)
    metadata = cache_metadata(method='free')
    business(journal, metadata)
    operation_id = str(uuid.uuid4())
    journal.free_intent(operation_id, metadata['backend_url'], journal.cache_cycles())
    journal.db.execute("""CREATE TRIGGER block_free_cycle BEFORE UPDATE ON cache_cycles
        WHEN NEW.state='requested' BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        journal.finish_free(operation_id, 'requested')
    assert journal.db.execute('SELECT state FROM cache_free_operations').fetchone()[0] == 'intent'
    assert journal.cache_cycles()[0]['state'] == 'releasing'
    journal.db.execute('DROP TRIGGER block_free_cycle')
    journal.close()
    recovered = Journal(path)
    recovered.recover_free()
    assert recovered.cache_cycles()[0]['state'] == 'uncertain'
    assert recovered.db.execute('SELECT state FROM cache_free_operations').fetchone()[0] == 'uncertain'
    recovered.close()


def test_cache_metadata_rejects_raw_text_and_logs(tmp_path):
    journal = Journal(tmp_path / 'tasks.sqlite3')
    metadata = cache_metadata()
    with pytest.raises(ValueError, match='invalid_cache_use'):
        business(journal, {**metadata, 'text': 'PRIVATE'})
    assert not journal.cache_cycles() and not journal.unresolved()
    with pytest.raises(ValueError):
        journal.set_cache_state('model', 1, 'failed', 'PRIVATE RAW EXCEPTION')
    journal.close()


def test_cleanup_seed_durable_nonwrapping_and_not_pruned(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    journal = Journal(path)
    assert journal.allocate_cleanup_seed() == 1
    journal.prune()
    journal.close()
    journal = Journal(path)
    assert journal.allocate_cleanup_seed() == 2
    with journal.db:
        journal.db.execute("UPDATE metadata SET value='4294967294' WHERE key='cleanup_seed_sequence'")
    assert journal.allocate_cleanup_seed() == 4294967295
    with pytest.raises(ValueError, match='cache_seed_exhausted'):
        journal.allocate_cleanup_seed()
    journal.close()
    recovered = Journal(path)
    with pytest.raises(ValueError, match='cache_seed_exhausted'):
        recovered.allocate_cleanup_seed()
    recovered.close()


def test_cleanup_seed_concurrent_connections_never_duplicate(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'tasks.sqlite3'
    Journal(path).close()
    def allocate(_):
        journal = Journal(path)
        try:
            return [journal.allocate_cleanup_seed() for _ in range(10)]
        finally:
            journal.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = [value for batch in pool.map(allocate, range(2)) for value in batch]
    assert sorted(values) == list(range(1, 21))


def test_rejected_business_restores_previous_cycle_without_rearming_failed_model(tmp_path):
    journal = Journal(tmp_path / 'tasks.sqlite3')
    metadata = cache_metadata()
    first = business(journal, metadata)
    journal.update(first['task_id'], state='error', error_code='submission_rejected')
    assert journal.cache_cycles() == []
    second = business(journal, metadata)
    assert second['cache_generation'] > first['cache_generation']
    journal.update(second['task_id'], state='success')
    previous = journal.cache_cycles()[0]
    journal.set_cache_state('model', previous['use_generation'], 'failed', 'cache_cleanup_failed')
    rejected = business(journal, metadata)
    journal.update(rejected['task_id'], state='error', error_code='submission_rejected')
    restored = journal.cache_cycles()[0]
    assert restored['state'] == 'failed' and restored['error_code'] == 'cache_cleanup_failed'
    assert restored['use_generation'] == previous['use_generation']
    assert business(journal, metadata)['cache_generation'] > rejected['cache_generation']
    journal.close()


def test_stale_retention_protects_configured_and_unresolved_cache_ownership(tmp_path):
    journal = Journal(tmp_path / 'tasks.sqlite3')
    active_metadata = cache_metadata(model='active')
    business(journal, active_metadata)
    active = cleanup(journal, active_metadata)
    journal.set_cache_state('active', 1, 'stale', 'cache_config_stale')
    configured_metadata = cache_metadata(model='configured')
    configured = business(journal, configured_metadata)
    journal.set_cache_state('configured', configured['cache_generation'], 'stale', 'cache_config_stale')
    row = journal.cache_cycles()[0]
    columns = list(row)
    with journal.db:
        for index in range(1002):
            retired = {**row, 'model': 'retired' + str(index), 'operation_id': None, 'state': 'stale'}
            journal.db.execute('INSERT INTO cache_cycles (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                               tuple(retired[name] for name in columns))
    journal.prune_retired_cache(['configured'])
    assert len(journal.cache_cycles()) == 1002  # 1000 retired + configured + unresolved ownership
    with journal.db:
        journal.db.execute('UPDATE cache_cycles SET updated=?', (time.time() - 8 * 86400,))
    journal.prune_retired_cache(['configured'])
    assert {row['model'] for row in journal.cache_cycles()} == {'active', 'configured'}
    assert journal.get(active['task_id'])['state'] == 'intent'
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
