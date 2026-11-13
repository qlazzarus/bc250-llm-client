"""Rehearse a cutover on temporary databases and a fake legacy write fence."""
import sqlite3

import pytest

from backend.app.db import connect, initialize
from backend.app.integrated_memory import create_initial_memory
from backend.app.wiki_repository import create_document, list_documents, search_documents


def backup(source, destination):
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


class FakeLegacyWiki:
    """Represents a write fence, not a test of systemd or the real Wiki service."""
    def __init__(self):
        self.documents = {1: '기존 Wiki 문서'}
        self.fenced = False

    def write(self, doc_id, value):
        if self.fenced:
            raise PermissionError('legacy writer fenced')
        self.documents[doc_id] = value


def test_isolated_cutover_preserves_both_databases_and_legacy_history(tmp_path, monkeypatch):
    path = str(tmp_path / 'client.db')
    before = str(tmp_path / 'before.db')
    after = str(tmp_path / 'after.db')
    legacy = FakeLegacyWiki()
    with monkeypatch.context() as patch:
        for name in ('_apply_memory_namespace_migration', '_apply_wiki_revision_migration', '_apply_wiki_alias_migration', '_apply_wiki_evidence_migration'):
            patch.setattr('backend.app.db.' + name, lambda _connection: None)
        initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at,mode) VALUES ('c','보존할 대화','t','t','quick')")
        db.execute("INSERT INTO messages VALUES ('legacy-u','c','user','기존 발언','t')")
        db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('legacy-g','c','legacy-u','completed','t','t','t')")
        db.execute("INSERT INTO wiki_memory_jobs(generation_id,conversation_id,status,document_id,created_at,updated_at) VALUES ('legacy-g','c','unknown',1,'t','t')")
    backup(path, before)
    legacy.fenced = True
    with pytest.raises(PermissionError):
        legacy.write(1, '변경 금지')
    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert list_documents(db, store_id='integrated') == []
        assert tuple(db.execute("SELECT store_id,status,document_id FROM wiki_memory_jobs WHERE generation_id='legacy-g'").fetchone()) == ('legacy_external', 'unknown', 1)
        db.execute('BEGIN IMMEDIATE')
        created = create_document(db, store_id='integrated', title='새 Wiki', content='통합 고유 문서')
        assert created['id'] == 1  # Same numeric ID, different store.
        db.execute("INSERT INTO messages VALUES ('new-u','c','user','BC250 서버 운영체제는 Fedora','new')")
        db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('new-g','c','new-u','completed','new','new','new')")
        db.execute("INSERT INTO wiki_memory_jobs(generation_id,conversation_id,status,store_id,created_at,updated_at) VALUES ('new-g','c','pending','integrated','new','new')")
    memory = create_initial_memory(path, 'new-g')
    initialize(path)  # Reopening the migrated DB models a new process startup.
    with connect(path) as db:
        assert search_documents(db, store_id='integrated', query='통합 고유')[0]['id'] == 1
        assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='new-g'").fetchone()[0] == 'completed'
        assert db.execute("SELECT store_id FROM memory_records WHERE wiki_document_id=?", (memory['document_id'],)).fetchone()[0] == 'integrated'
        assert db.execute("SELECT title FROM conversations WHERE id='c'").fetchone()[0] == '보존할 대화'
    backup(path, after)
    # Read-only recovery preserves both states; it never replaces either DB.
    for snapshot in (before, after):
        db = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
        try:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            with pytest.raises(sqlite3.OperationalError):
                db.execute("DELETE FROM conversations")
        finally:
            db.close()
    with sqlite3.connect(f'file:{before}?mode=ro', uri=True) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 0
        assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='legacy-g'").fetchone()[0] == 'unknown'
    with sqlite3.connect(f'file:{after}?mode=ro', uri=True) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 2
        assert db.execute("SELECT store_id FROM wiki_memory_jobs WHERE generation_id='legacy-g'").fetchone()[0] == 'legacy_external'
    assert legacy.documents == {1: '기존 Wiki 문서'}


def test_previous_v16_initializer_does_not_fence_a_v21_database(tmp_path):
    """A historical initializer is not a valid guard against version rollback."""
    import importlib.util
    from pathlib import Path
    fixture = Path(__file__).parent / 'fixtures' / 'client_schema_v16.py'
    spec = importlib.util.spec_from_file_location('historical_client_db', fixture)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = str(tmp_path / 'client.db')
    initialize(path)
    assert module.LATEST_SCHEMA_VERSION == 16
    module.initialize(path)
    with connect(path) as db:
        assert db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 21
        # Legacy INSERT omits store_id, demonstrating its interpretation remains
        # implicit even after the new namespace migration.
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c','t','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','source','t')")
        db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('g','c','u','completed','t','t','t')")
        db.execute("INSERT INTO wiki_memory_jobs(generation_id,conversation_id,status,document_id,created_at,updated_at) VALUES ('g','c','unknown',1,'t','t')")
        assert db.execute("SELECT store_id FROM wiki_memory_jobs WHERE generation_id='g'").fetchone()[0] == 'legacy_external'
