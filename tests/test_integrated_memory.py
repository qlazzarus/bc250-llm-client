from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from backend.app.db import connect, initialize
from backend.app.integrated_memory import MemoryWriteConflict, create_initial_memory
from backend.app.wiki_repository import search_documents


def setup_job(path, generation='g', value='Fedora', store='integrated'):
    with connect(path) as db:
        db.execute("INSERT OR IGNORE INTO conversations(id,title,created_at,updated_at,mode) VALUES ('c','t','t','t','quick')")
        db.execute("INSERT INTO messages VALUES (?, 'c','user',?,'t')", ('u' + generation, f'BC250 서버 운영체제는 {value}'))
        db.execute("""INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at)
                   VALUES (?,'c',?,'completed','t','t','t')""", (generation, 'u' + generation))
        db.execute("""INSERT INTO wiki_memory_jobs(generation_id,conversation_id,status,created_at,updated_at,store_id)
                   VALUES (?,'c','pending','t','t',?)""", (generation, store))


def test_atomic_create_and_idempotent_result_survives_reopen(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path)
    first = create_initial_memory(path, 'g')
    initialize(path)
    replay = create_initial_memory(path, 'g')
    assert first['document_id'] == replay['document_id'] and replay['replayed']
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM wiki_documents").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM wiki_documents_fts WHERE wiki_documents_fts MATCH 'Fedora'").fetchone()[0] == 1
        assert db.execute("SELECT state FROM memory_records").fetchone()[0] == 'active'
        assert db.execute("SELECT state FROM memory_operations").fetchone()[0] == 'applied'
        assert db.execute("SELECT status FROM wiki_memory_jobs").fetchone()[0] == 'completed'
        assert search_documents(db, store_id='integrated', query='Fedora') == []


def test_failures_roll_back_every_piece(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path)
    with connect(path) as db:
        db.execute("CREATE TRIGGER reject_activation BEFORE UPDATE OF state ON memory_records WHEN NEW.state='active' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        create_initial_memory(path, 'g')
    with connect(path) as db:
        for table in ('wiki_documents', 'wiki_documents_fts', 'memory_records', 'memory_operations', 'memory_document_quarantine'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert db.execute('SELECT status FROM wiki_memory_jobs').fetchone()[0] == 'pending'


def test_legacy_and_conflicting_slots_cannot_write(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'legacy', store='legacy_external')
    with pytest.raises(MemoryWriteConflict):
        create_initial_memory(path, 'legacy')
    setup_job(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    create_initial_memory(path, 'one')
    with pytest.raises(MemoryWriteConflict):
        create_initial_memory(path, 'two')
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1
        assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='legacy'").fetchone()[0] == 'pending'


def test_two_jobs_same_slot_commit_at_most_one(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    def run(job):
        try:
            create_initial_memory(path, job)
            return 'created'
        except MemoryWriteConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, ('one', 'two'))) == ['conflict', 'created']
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM memory_records WHERE state='active'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1


def test_replay_after_concurrent_identical_calls_creates_one_document(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create_initial_memory(path, 'g'), range(2)))
    assert results[0]['document_id'] == results[1]['document_id']
    assert sorted(result['replayed'] for result in results) == [False, True]
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1


def test_deleted_source_and_reused_operation_do_not_write(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path)
    with connect(path) as db:
        db.execute("DELETE FROM messages WHERE id='ug'")
    with pytest.raises(MemoryWriteConflict):
        create_initial_memory(path, 'g')
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 0


def test_legacy_unknown_operation_is_never_replayed_as_integrated(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'legacy', store='legacy_external')
    with connect(path) as db:
        db.execute("UPDATE wiki_memory_jobs SET status='unknown' WHERE generation_id='legacy'")
    with pytest.raises(MemoryWriteConflict):
        create_initial_memory(path, 'legacy')
    with connect(path) as db:
        assert db.execute("SELECT store_id,status FROM wiki_memory_jobs").fetchone()[:] == ('legacy_external', 'unknown')
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 0


def test_committed_source_redaction_quarantines_and_prevents_replay(tmp_path):
    from backend.app.memory_repository import redact_conversation_memory
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path)
    result = create_initial_memory(path, 'g')
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        redact_conversation_memory(db, 'c', 'later')
        db.execute("DELETE FROM conversations WHERE id='c'")
    with pytest.raises(MemoryWriteConflict):
        create_initial_memory(path, 'g')
    with connect(path) as db:
        record = db.execute('SELECT state,value FROM memory_records').fetchone()
        assert tuple(record) == ('redacted', None)
        assert db.execute("SELECT reason FROM memory_document_quarantine WHERE store_id='integrated' AND wiki_document_id=?", (result['document_id'],)).fetchone()[0] == 'source_deleted'
        assert search_documents(db, store_id='integrated', query='Fedora') == []


def test_replay_rejects_changed_source(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path)
    create_initial_memory(path, 'g')
    with connect(path) as db:
        db.execute("UPDATE messages SET content='BC250 서버 운영체제는 Ubuntu' WHERE id='ug'")
    with pytest.raises(MemoryWriteConflict):
        create_initial_memory(path, 'g')
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1


def test_prepare_and_approve_conflict_once_with_immutable_versions(tmp_path):
    from backend.app.integrated_memory import prepare_conflict, resolve_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    first = create_initial_memory(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    pending = prepare_conflict(path, 'two')
    assert pending['status'] == 'awaiting_confirmation'
    with connect(path) as db:
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Fedora'
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1
    result = resolve_conflict(path, 'c', 'two', pending['confirmation_token'], approve=True)
    assert result['document_id'] != first['document_id']
    assert resolve_conflict(path, 'c', 'two', pending['confirmation_token'], approve=True)['replayed']
    with connect(path) as db:
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Ubuntu'
        assert db.execute("SELECT value FROM memory_records WHERE state='superseded'").fetchone()[0] == 'Fedora'
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 2


def test_duplicate_and_cancel_do_not_create_new_documents(tmp_path):
    from backend.app.integrated_memory import prepare_conflict, resolve_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'duplicate')
    assert prepare_conflict(path, 'duplicate')['reason'] == 'duplicate'
    setup_job(path, 'two', 'Ubuntu')
    pending = prepare_conflict(path, 'two')
    assert resolve_conflict(path, 'c', 'two', pending['confirmation_token'], approve=False)['status'] == 'skipped'
    with pytest.raises(MemoryWriteConflict):
        resolve_conflict(path, 'c', 'two', pending['confirmation_token'], approve=True)
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Fedora'


@pytest.mark.parametrize('change', ['source', 'target', 'expired', 'wrong_token'])
def test_stale_confirmation_never_updates_wiki(tmp_path, change):
    from backend.app.integrated_memory import prepare_conflict, resolve_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    token = prepare_conflict(path, 'two')['confirmation_token']
    with connect(path) as db:
        if change == 'source':
            db.execute("UPDATE messages SET content='BC250 서버 운영체제는 Debian' WHERE id='utwo'")
        elif change == 'target':
            db.execute("UPDATE wiki_documents SET revision=revision+1 WHERE id=1")
        elif change == 'expired':
            db.execute("UPDATE memory_operations SET created_at='2000-01-01T00:00:00+00:00' WHERE id='two'")
        else:
            token = 'wrong'
    with pytest.raises(MemoryWriteConflict):
        resolve_conflict(path, 'c', 'two', token, approve=True)
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Fedora'


def test_concurrent_opposite_decisions_allow_only_one(tmp_path):
    from backend.app.integrated_memory import prepare_conflict, resolve_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    token = prepare_conflict(path, 'two')['confirmation_token']
    def decide(approve):
        try:
            return resolve_conflict(path, 'c', 'two', token, approve=approve)['status']
        except MemoryWriteConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, [True, False]))
    assert results.count('conflict') == 1
    assert len(set(results) & {'completed', 'skipped'}) == 1
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM memory_records WHERE state='active'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] in (1, 2)


def test_expired_confirmation_can_be_kept_to_release_slot(tmp_path):
    from backend.app.integrated_memory import prepare_conflict, resolve_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    token = prepare_conflict(path, 'two')['confirmation_token']
    with connect(path) as db:
        db.execute("UPDATE memory_operations SET created_at='2000-01-01T00:00:00+00:00' WHERE id='two'")
    assert resolve_conflict(path, 'c', 'two', token, approve=False)['status'] == 'skipped'
    with connect(path) as db:
        assert db.execute("SELECT state FROM memory_operations WHERE id='two'").fetchone()[0] == 'cancelled'


@pytest.mark.asyncio
async def test_confirmation_http_requires_auth_and_conversation_binding(tmp_path):
    import os
    import httpx
    from types import SimpleNamespace
    os.environ.setdefault('APP_API_KEY', 'test')
    os.environ.setdefault('LLM_GATE_API_KEY', 'test')
    from backend.app import main
    from backend.app.config import get_settings
    from backend.app.integrated_memory import prepare_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    token = prepare_conflict(path, 'two')['confirmation_token']
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', app_auth_mode='key', app_api_key='test', max_request_bytes=65536)
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            url = '/api/v1/conversations/c/wiki-memory/two/confirmation'
            payload = {'token': token, 'decision': 'approve'}
            assert (await client.post(url, json=payload)).status_code == 401
            client.headers['Authorization'] = 'Bearer test'
            history = await client.get('/api/v1/conversations/c/wiki-memory')
            assert next(row for row in history.json()['data'] if row['generation_id'] == 'two')['confirmation_token'] == token
            assert (await client.post('/api/v1/conversations/other/wiki-memory/two/confirmation', json=payload)).status_code == 409
            assert (await client.post(url, json=payload)).status_code == 200
            replay = await client.post(url, json=payload)
            assert replay.status_code == 200 and replay.json()['replayed']
    finally:
        main.app.dependency_overrides.pop(get_settings, None)


def test_failed_conflict_commit_rolls_back_all_changes(tmp_path):
    from backend.app.integrated_memory import prepare_conflict, resolve_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    token = prepare_conflict(path, 'two')['confirmation_token']
    with connect(path) as db:
        db.execute("CREATE TRIGGER reject_new_active BEFORE UPDATE OF state ON memory_records WHEN NEW.state='active' AND NEW.value='Ubuntu' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        resolve_conflict(path, 'c', 'two', token, approve=True)
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Fedora'
        assert db.execute("SELECT state FROM memory_operations WHERE id='two'").fetchone()[0] == 'prepared'
        assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='two'").fetchone()[0] == 'awaiting_confirmation'


def test_worker_create_duplicate_conflict_and_explicit_correction(tmp_path):
    from backend.app.integrated_memory import process_integrated_job, search_active_memories
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    process_integrated_job(path, 'one')
    assert search_active_memories(path, 'BC-250 서버 운영체제는 뭐야?')[0]['value'] == 'Fedora'
    assert search_active_memories(path, '다른 기기 운영체제는 뭐야?') == []
    setup_job(path, 'duplicate')
    process_integrated_job(path, 'duplicate')
    setup_job(path, 'conflict', 'Ubuntu')
    process_integrated_job(path, 'conflict')
    with connect(path) as db:
        assert db.execute("SELECT reason_code FROM wiki_memory_jobs WHERE generation_id='duplicate'").fetchone()[0] == 'duplicate'
        assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='conflict'").fetchone()[0] == 'awaiting_confirmation'
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Fedora'
    # A waiting confirmation must be resolved before a new correction can claim the slot.
    from backend.app.integrated_memory import confirmation_token, resolve_conflict
    token = confirmation_token(path, 'c', 'conflict')
    resolve_conflict(path, 'c', 'conflict', token, approve=False)
    setup_job(path, 'correct', 'Ubuntu')
    with connect(path) as db:
        db.execute("UPDATE messages SET content='정정: BC250 서버 운영체제는 Ubuntu' WHERE id='ucorrect'")
    process_integrated_job(path, 'correct')
    assert search_active_memories(path, 'BC250 OS')[0]['value'] == 'Ubuntu'
    with connect(path) as db:
        assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='correct'").fetchone()[0] == 'completed'


def test_worker_rejects_correction_without_target_and_preserves_legacy(tmp_path):
    from backend.app.integrated_memory import process_integrated_job
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'legacy', store='legacy_external')
    process_integrated_job(path, 'legacy')
    setup_job(path, 'correct', 'Ubuntu')
    with connect(path) as db:
        db.execute("UPDATE messages SET content='정정: BC250 서버 운영체제는 Ubuntu' WHERE id='ucorrect'")
    with pytest.raises(MemoryWriteConflict):
        process_integrated_job(path, 'correct')
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 0
        assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='legacy'").fetchone()[0] == 'pending'


def test_active_memory_retrieval_refuses_document_revision_change(tmp_path):
    from backend.app.integrated_memory import search_active_memories
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path)
    create_initial_memory(path, 'g')
    with connect(path) as db:
        db.execute('UPDATE wiki_documents SET revision=revision+1 WHERE id=1')
    assert search_active_memories(path, 'BC250 운영체제') == []


def test_generation_completion_creates_integrated_job_and_worker_saves_it(tmp_path):
    import asyncio
    import os
    from types import SimpleNamespace
    os.environ.setdefault('APP_API_KEY', 'test')
    os.environ.setdefault('LLM_GATE_API_KEY', 'test')
    from backend.app import main
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at,mode) VALUES ('c','title','t','t','quick')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','BC250 서버 운영체제는 Fedora','t')")
        db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('g','c','u','pending','t','t','t')")
    settings = SimpleNamespace(database_path=path, wiki_store='integrated')
    assert main.complete_generation(settings, 'g', '답변', 'stop', None, True)
    asyncio.run(main.run_wiki_memory_job(settings, 'g'))
    with connect(path) as db:
        assert tuple(db.execute("SELECT store_id,status FROM wiki_memory_jobs WHERE generation_id='g'").fetchone()) == ('integrated', 'completed')
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Fedora'


@pytest.mark.asyncio
async def test_restart_recovers_only_integrated_pending_and_keeps_conflicts(tmp_path, monkeypatch):
    import asyncio
    import os
    from types import SimpleNamespace
    os.environ.setdefault('APP_API_KEY', 'test')
    os.environ.setdefault('LLM_GATE_API_KEY', 'test')
    from backend.app import main
    from backend.app.integrated_memory import prepare_conflict
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'conflict', 'Ubuntu')
    prepare_conflict(path, 'conflict')
    setup_job(path, 'pending', 'Fedora')
    setup_job(path, 'legacy', store='legacy_external')
    with connect(path) as db:
        db.execute("UPDATE wiki_memory_jobs SET status='unknown' WHERE generation_id='legacy'")
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', max_concurrent_generations=1,
                               ensure_data_directory=lambda: None)
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    async with main.lifespan(main.app):
        for _ in range(200):
            with connect(path) as db:
                status = db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='pending'").fetchone()[0]
            if status != 'pending':
                break
            await asyncio.sleep(.01)
        assert status == 'skipped'
        with connect(path) as db:
            assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='conflict'").fetchone()[0] == 'awaiting_confirmation'
            assert tuple(db.execute("SELECT store_id,status FROM wiki_memory_jobs WHERE generation_id='legacy'").fetchone()) == ('legacy_external', 'unknown')
            assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1


def test_legacy_active_and_unknown_are_not_selected_for_integrated_worker(tmp_path):
    from backend.app.integrated_memory import process_integrated_job
    from backend.app.memory_repository import claim_memory, transition_operation, apply_confirmed
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'old', store='legacy_external')
    text = 'BC250 서버 운영체제는 Fedora'
    start = text.index('Fedora')
    claim_memory(path, record_id='legacy-record', operation_id='legacy-operation', subject_key='bc250_server',
                 predicate_key='operating_system', kind='fact', conversation_id='c', message_id='uold', start=start,
                 end=start + 6, value='Fedora', request_hash='legacy')
    transition_operation(path, 'legacy-operation', 'claimed', 'prepared')
    transition_operation(path, 'legacy-operation', 'prepared', 'sending')
    transition_operation(path, 'legacy-operation', 'sending', 'confirmed', document_id=1, revision='legacy-v1')
    apply_confirmed(path, 'legacy-operation')
    setup_job(path, 'pending-legacy', store='legacy_external')
    with connect(path) as db:
        db.execute("UPDATE wiki_memory_jobs SET status='unknown' WHERE generation_id='pending-legacy'")
    setup_job(path, 'new', 'Ubuntu')
    process_integrated_job(path, 'new')
    with connect(path) as db:
        assert tuple(db.execute("SELECT store_id,state,wiki_document_id FROM memory_records WHERE id='legacy-record'").fetchone()) == ('legacy_external', 'active', 1)
        assert tuple(db.execute("SELECT store_id,status FROM wiki_memory_jobs WHERE generation_id='pending-legacy'").fetchone()) == ('legacy_external', 'unknown')
        assert db.execute("SELECT value FROM memory_records WHERE store_id='integrated' AND state='active'").fetchone()[0] == 'Ubuntu'
        assert db.execute("SELECT count(*) FROM wiki_documents").fetchone()[0] == 1


def test_modified_source_cannot_turn_pending_confirmation_into_correction(tmp_path):
    from backend.app.integrated_memory import prepare_conflict, process_integrated_job
    path = str(tmp_path / 'client.db')
    initialize(path)
    setup_job(path, 'one')
    create_initial_memory(path, 'one')
    setup_job(path, 'two', 'Ubuntu')
    with connect(path) as db:
        db.execute("UPDATE messages SET content='    BC250 서버 운영체제는 Ubuntu' WHERE id='utwo'")
    prepare_conflict(path, 'two')
    with connect(path) as db:
        db.execute("UPDATE messages SET content='정정: BC250 서버 운영체제는 Ubuntu' WHERE id='utwo'")
    with pytest.raises(MemoryWriteConflict):
        process_integrated_job(path, 'two')
    with connect(path) as db:
        assert db.execute("SELECT value FROM memory_records WHERE state='active'").fetchone()[0] == 'Fedora'
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_integrated_worker_serializes_local_processing(monkeypatch, tmp_path):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace
    from backend.app import main
    active = 0
    peak = 0
    mutex = threading.Lock()
    def process(_path, _job):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(peak, active)
        time.sleep(.03)
        with mutex:
            active -= 1
    monkeypatch.setattr(main, 'process_integrated_job', process)
    monkeypatch.setattr(main.app.state, 'wiki_memory_lock', asyncio.Lock(), raising=False)
    settings = SimpleNamespace(database_path=str(tmp_path / 'unused.db'), wiki_store='integrated')
    await asyncio.gather(*(main.run_wiki_memory_job(settings, str(index)) for index in range(8)))
    assert peak == 1


@pytest.mark.asyncio
async def test_recovery_worker_processes_more_than_one_bounded_batch(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from backend.app import main
    path = str(tmp_path / 'client.db')
    initialize(path)
    for index in range(35):
        setup_job(path, f'batch{index}')
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', max_concurrent_generations=1,
                               ensure_data_directory=lambda: None)
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    async with main.lifespan(main.app):
        for _ in range(500):
            with connect(path) as db:
                pending = db.execute("SELECT count(*) FROM wiki_memory_jobs WHERE store_id='integrated' AND status='pending'").fetchone()[0]
            if not pending:
                break
            await asyncio.sleep(.01)
        assert pending == 0
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM wiki_memory_jobs WHERE reason_code='duplicate'").fetchone()[0] == 34
