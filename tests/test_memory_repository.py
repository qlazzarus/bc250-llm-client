import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app.db import connect, initialize
from backend.app.memory_repository import apply_confirmed, claim_memory, get_active_memory, transition_operation


def test_upgrade_is_repeatable_and_preserves_legacy_data(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','legacy','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','원문','t')")
        db.execute("DELETE FROM schema_migrations WHERE version=16")
        db.execute("DROP TABLE memory_operations")
        db.execute("DROP TABLE memory_records")
        db.execute("DROP TABLE memory_document_quarantine")
    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert db.execute("SELECT content FROM messages WHERE id='u'").fetchone()[0] == '원문'
        assert db.execute("SELECT count(*) FROM schema_migrations WHERE version=16").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM memory_records").fetchone()[0] == 0


def test_active_lookup_excludes_inactive_and_slot_is_unique(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','원문','t')")
        insert = """INSERT INTO memory_records
            (id,scope_key,subject_key,predicate_key,kind,value,normalized_value,fingerprint,
             source_conversation_id,source_message_id,source_start,source_end,wiki_document_id,wiki_revision,state,created_at,updated_at)
            VALUES (?,'shared','object','property','fact','원문','원문',?,'c','u',0,2,?, 'rev',?,'t','t')"""
        db.execute(insert, ('old', 'hash-old', 1, 'superseded'))
        db.execute(insert, ('current', 'hash-current', 2, 'active'))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, ('other', 'hash-other', 3, 'active'))
    assert get_active_memory(path, 'shared', 'object', 'property')['id'] == 'current'
    assert get_active_memory(path, 'shared', 'object', 'missing') is None


def test_direct_source_deletion_fails_closed(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','원문','t')")
        db.execute("""INSERT INTO memory_records
            (id,scope_key,subject_key,predicate_key,kind,value,normalized_value,fingerprint,
             source_conversation_id,source_message_id,source_start,source_end,state,created_at,updated_at)
            VALUES ('r','shared','object','property','fact','원문','원문','hash','c','u',0,2,'candidate','t','t')""")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM conversations WHERE id='c'")
        assert db.execute("SELECT count(*) FROM conversations WHERE id='c'").fetchone()[0] == 1


def test_claim_confirmation_and_conversation_deletion(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app.main import delete_conversation

    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','서버는 Fedora 사용','t')")
    args = dict(record_id='r', operation_id='o', subject_key='bc250_server', predicate_key='operating_system',
                kind='fact', conversation_id='c', message_id='u', start=4, end=10, value='Fedora', request_hash='hash')
    claim_memory(path, **args)
    with pytest.raises(sqlite3.IntegrityError):
        claim_memory(path, **{**args, 'record_id': 'r2', 'operation_id': 'o2'})
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM memory_records WHERE id='r2'").fetchone()[0] == 0
    transition_operation(path, 'o', 'claimed', 'prepared')
    transition_operation(path, 'o', 'prepared', 'sending')
    transition_operation(path, 'o', 'sending', 'unknown')
    with pytest.raises(ValueError):
        transition_operation(path, 'o', 'unknown', 'sending')
    transition_operation(path, 'o', 'unknown', 'confirmed', document_id=42, revision='revision')
    apply_confirmed(path, 'o')
    assert get_active_memory(path, 'shared', 'bc250_server', 'operating_system')['value'] == 'Fedora'
    delete_conversation('c', SimpleNamespace(database_path=path))
    with connect(path) as db:
        record = db.execute("SELECT * FROM memory_records WHERE id='r'").fetchone()
        assert record['state'] == 'redacted' and record['value'] is None and record['source_message_id'] is None
        assert db.execute('SELECT reason FROM memory_document_quarantine WHERE wiki_document_id=42').fetchone()[0] == 'source_deleted'
    assert get_active_memory(path, 'shared', 'bc250_server', 'operating_system') is None


def test_claim_rejects_unsupported_slot_and_wrong_source(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','assistant','Fedora','t')")
    args = dict(record_id='r', operation_id='o', subject_key='bc250_server', predicate_key='operating_system',
                kind='fact', conversation_id='c', message_id='u', start=0, end=6, value='Fedora', request_hash='hash')
    with pytest.raises(ValueError):
        claim_memory(path, **args)
    with pytest.raises(ValueError):
        claim_memory(path, **{**args, 'predicate_key': 'unknown'})
    with connect(path) as db:
        db.execute("UPDATE messages SET role='user' WHERE id='u'")
    with pytest.raises(ValueError):
        claim_memory(path, **{**args, 'end': 99})
    with pytest.raises(ValueError):
        claim_memory(path, **{**args, 'start': True})


def test_unknown_write_after_source_deletion_stays_blocked(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app.main import delete_conversation

    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','Fedora','t')")
    claim_memory(path, record_id='r', operation_id='o', subject_key='bc250_server',
                 predicate_key='operating_system', kind='fact', conversation_id='c', message_id='u',
                 start=0, end=6, value='Fedora', request_hash='hash')
    transition_operation(path, 'o', 'claimed', 'prepared')
    transition_operation(path, 'o', 'prepared', 'sending')
    delete_conversation('c', SimpleNamespace(database_path=path))
    with pytest.raises(ValueError, match='source no longer available'):
        transition_operation(path, 'o', 'unknown', 'confirmed', document_id=42, revision='v1')
    with connect(path) as db:
        assert db.execute("SELECT state FROM memory_operations WHERE id='o'").fetchone()[0] == 'unknown'
        assert db.execute("SELECT state FROM memory_records WHERE id='r'").fetchone()[0] == 'redacted'
        assert db.execute('SELECT count(*) FROM memory_document_quarantine').fetchone()[0] == 0


def test_confirm_rejects_existing_quarantined_document_without_changing_job(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','Fedora','t')")
        db.execute("INSERT INTO memory_document_quarantine VALUES (42,'source_deleted','t','t')")
    claim_memory(path, record_id='r', operation_id='o', subject_key='bc250_server',
                 predicate_key='operating_system', kind='fact', conversation_id='c', message_id='u',
                 start=0, end=6, value='Fedora', request_hash='hash')
    transition_operation(path, 'o', 'claimed', 'prepared')
    transition_operation(path, 'o', 'prepared', 'sending')
    with pytest.raises(ValueError, match='already managed'):
        transition_operation(path, 'o', 'sending', 'confirmed', document_id=42, revision='v1')
    with connect(path) as db:
        assert db.execute("SELECT state FROM memory_operations WHERE id='o'").fetchone()[0] == 'sending'
        assert db.execute("SELECT wiki_document_id FROM memory_records WHERE id='r'").fetchone()[0] is None
        assert db.execute('SELECT reason FROM memory_document_quarantine WHERE wiki_document_id=42').fetchone()[0] == 'source_deleted'


def test_concurrent_claims_allow_only_one(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','Fedora','t')")
    def claim(index):
        try:
            claim_memory(path, record_id=f'r{index}', operation_id=f'o{index}', subject_key='bc250_server',
                         predicate_key='operating_system', kind='fact', conversation_id='c', message_id='u',
                         start=0, end=6, value='Fedora', request_hash='hash')
            return 'claimed'
        except sqlite3.IntegrityError:
            return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (1, 2))) == ['blocked', 'claimed']
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM memory_records').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM memory_operations').fetchone()[0] == 1


def test_correction_activation_is_atomic_and_requires_active_target(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','Fedora Ubuntu','t')")
    base = dict(subject_key='bc250_server', predicate_key='operating_system', kind='fact',
                conversation_id='c', message_id='u', request_hash='hash')
    claim_memory(path, **base, record_id='r1', operation_id='o1', start=0, end=6, value='Fedora')
    for before, after in [('claimed', 'prepared'), ('prepared', 'sending')]:
        transition_operation(path, 'o1', before, after)
    transition_operation(path, 'o1', 'sending', 'confirmed', document_id=1, revision='v1')
    apply_confirmed(path, 'o1')
    with pytest.raises(ValueError):
        claim_memory(path, **base, record_id='r2', operation_id='o2', start=7, end=13, value='Ubuntu')
    claim_memory(path, **base, record_id='r2', operation_id='o2', start=7, end=13, value='Ubuntu',
                 supersedes_id='r1', explicit_correction=True)
    for before, after in [('claimed', 'prepared'), ('prepared', 'sending')]:
        transition_operation(path, 'o2', before, after)
    transition_operation(path, 'o2', 'sending', 'confirmed', document_id=2, revision='v2')
    with connect(path) as db:
        db.execute("UPDATE memory_records SET state='quarantined' WHERE id='r1'")
    with pytest.raises(ValueError):
        apply_confirmed(path, 'o2')
    with connect(path) as db:
        assert db.execute("SELECT state FROM memory_records WHERE id='r2'").fetchone()[0] == 'candidate'
        assert db.execute("SELECT state FROM memory_operations WHERE id='o2'").fetchone()[0] == 'confirmed'
        db.execute("UPDATE memory_records SET state='active' WHERE id='r1'")
    apply_confirmed(path, 'o2')
    with connect(path) as db:
        assert db.execute("SELECT state FROM memory_records WHERE id='r1'").fetchone()[0] == 'superseded'
    assert get_active_memory(path, 'shared', 'bc250_server', 'operating_system')['id'] == 'r2'
