import sqlite3

import pytest

from backend.app.db import _apply_memory_namespace_migration, connect, initialize
from backend.app.memory_repository import claim_memory, get_active_memory, redact_conversation_memory


def create_v17(path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr('backend.app.db._apply_memory_namespace_migration', lambda db: None)
        patch.setattr('backend.app.db._apply_wiki_revision_migration', lambda db: None)
        initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','old','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','Fedora','t')")
        db.execute("""INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at)
                      VALUES ('g','c','u','completed','t','t','t')""")
        db.execute("""INSERT INTO wiki_memory_jobs (generation_id,conversation_id,status,document_id,created_at,updated_at)
                      VALUES ('g','c','unknown',1,'t','t')""")
        db.execute("""INSERT INTO memory_records
          (id,scope_key,subject_key,predicate_key,kind,value,normalized_value,fingerprint,source_conversation_id,
           source_message_id,source_start,source_end,wiki_document_id,wiki_revision,state,created_at,updated_at)
          VALUES ('r','shared','bc250_server','operating_system','fact','Fedora','Fedora','hash','c','u',0,6,1,'v1','active','t','t')""")
        db.execute("""INSERT INTO memory_operations
          (id,record_id,scope_key,subject_key,predicate_key,operation_type,request_hash,wiki_document_id,state,created_at,updated_at)
          VALUES ('o','r','shared','bc250_server','operating_system','create','hash',1,'unknown','t','t')""")
        db.execute("INSERT INTO memory_document_quarantine VALUES (1,'owned','t','t')")


def test_v17_upgrade_preserves_history_and_isolates_same_id(tmp_path, monkeypatch):
    path = str(tmp_path / 'client.db')
    create_v17(path, monkeypatch)
    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert tuple(db.execute("SELECT store_id,status,document_id FROM wiki_memory_jobs WHERE generation_id='g'").fetchone()) == ('legacy_external', 'unknown', 1)
        assert tuple(db.execute("SELECT store_id,state,wiki_document_id FROM memory_operations WHERE id='o'").fetchone()) == ('legacy_external', 'unknown', 1)
        assert tuple(db.execute("SELECT store_id,state,wiki_document_id FROM memory_records WHERE id='r'").fetchone()) == ('legacy_external', 'active', 1)
        db.execute("INSERT INTO wiki_documents(id,title,content,created_at,updated_at) VALUES (1,'new','new content','t','t')")
        db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',1,'owned','t','t')")
        assert db.execute('SELECT count(*) FROM memory_document_quarantine WHERE wiki_document_id=1').fetchone()[0] == 2
    assert get_active_memory(path, 'shared', 'bc250_server', 'operating_system', store_id='integrated') is None
    claim_memory(path, record_id='r2', operation_id='o2', subject_key='bc250_server', predicate_key='operating_system',
                 kind='fact', conversation_id='c', message_id='u', start=0, end=6, value='Fedora', request_hash='hash', store_id='integrated')
    with connect(path) as db:
        assert tuple(db.execute("SELECT store_id,state FROM memory_operations WHERE id='o2'").fetchone()) == ('integrated', 'claimed')
        redact_conversation_memory(db, 'c', 'later')
        assert tuple(db.execute("SELECT reason FROM memory_document_quarantine WHERE store_id='legacy_external' AND wiki_document_id=1").fetchone()) == ('source_deleted',)
        assert tuple(db.execute("SELECT reason FROM memory_document_quarantine WHERE store_id='integrated' AND wiki_document_id=1").fetchone()) == ('owned',)
        assert db.execute("SELECT content FROM wiki_documents WHERE id=1").fetchone()[0] == 'new content'


def test_cross_store_operation_and_correction_are_rejected(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','Fedora','t')")
    claim_memory(path, record_id='r', operation_id='o', subject_key='bc250_server', predicate_key='operating_system',
                 kind='fact', conversation_id='c', message_id='u', start=0, end=6, value='Fedora', request_hash='hash')
    with connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match='operation store mismatch'):
            db.execute("UPDATE memory_operations SET store_id='integrated' WHERE id='o'")
        with pytest.raises(sqlite3.IntegrityError, match='record store immutable'):
            db.execute("UPDATE memory_records SET store_id='integrated' WHERE id='r'")
        with pytest.raises(sqlite3.IntegrityError, match='supersedes store mismatch'):
            db.execute("""INSERT INTO memory_records (id,scope_key,subject_key,predicate_key,kind,value,normalized_value,
              fingerprint,source_conversation_id,source_message_id,source_start,source_end,state,supersedes_id,created_at,updated_at,store_id)
              VALUES ('r2','shared','bc250_server','operating_system','fact','Fedora','Fedora','h','c','u',0,6,'candidate','r','t','t','integrated')""")


def test_namespace_migration_failure_rolls_back_schema(tmp_path, monkeypatch):
    path = str(tmp_path / 'client.db')
    create_v17(path, monkeypatch)
    with connect(path) as db:
        db.execute("CREATE TRIGGER memory_operation_store_insert AFTER INSERT ON messages BEGIN SELECT 1; END")
        with pytest.raises(sqlite3.OperationalError):
            _apply_memory_namespace_migration(db)
        assert 'store_id' not in [row['name'] for row in db.execute('PRAGMA table_info(memory_records)')]
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=18').fetchone()[0] == 0
        assert db.execute('SELECT reason FROM memory_document_quarantine WHERE wiki_document_id=1').fetchone()[0] == 'owned'
