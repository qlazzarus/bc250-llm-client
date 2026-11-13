import sqlite3

import pytest

from backend.app.db import _apply_integrated_wiki_migration, connect, initialize


def prepare_previous_database(path, version, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr('backend.app.db._apply_memory_namespace_migration', lambda db: None)
        patch.setattr('backend.app.db._apply_wiki_revision_migration', lambda db: None)
        initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','대화','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','기존 원문','t')")
        db.execute("""INSERT INTO generation_requests
            (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at)
            VALUES ('g','c','u','completed','t','t','t')""")
        db.execute("""INSERT INTO wiki_memory_jobs
            (generation_id,conversation_id,status,document_id,title,created_at,updated_at)
            VALUES ('g','c','completed',1,'기존 Wiki','t','t')""")
        db.execute("DELETE FROM schema_migrations WHERE version=17")
        db.execute('DROP TRIGGER wiki_documents_ai')
        db.execute('DROP TRIGGER wiki_documents_au')
        db.execute('DROP TRIGGER wiki_documents_ad')
        db.execute('DROP TABLE wiki_documents_fts')
        db.execute('DROP TABLE wiki_documents')
        if version == 15:
            db.execute("DELETE FROM schema_migrations WHERE version=16")
            db.execute('DROP TABLE memory_operations')
            db.execute('DROP TABLE memory_records')
            db.execute('DROP TABLE memory_document_quarantine')
        else:
            db.execute("INSERT INTO memory_document_quarantine VALUES (1,'source_deleted','t','t')")


@pytest.mark.parametrize('old_version', [15, 16])
def test_upgrade_is_repeatable_and_preserves_existing_data(tmp_path, old_version, monkeypatch):
    path = str(tmp_path / 'client.db')
    prepare_previous_database(path, old_version, monkeypatch)
    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert db.execute("SELECT content FROM messages WHERE id='u'").fetchone()[0] == '기존 원문'
        job = db.execute("SELECT status,document_id,title FROM wiki_memory_jobs WHERE generation_id='g'").fetchone()
        assert tuple(job) == ('completed', 1, '기존 Wiki')
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM wiki_documents_fts').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=17').fetchone()[0] == 1
        if old_version == 16:
            assert db.execute('SELECT reason FROM memory_document_quarantine WHERE wiki_document_id=1').fetchone()[0] == 'source_deleted'
    assert {file.name for file in tmp_path.iterdir()} == {'client.db'}


def test_fts_insert_update_delete_and_transaction_rollback(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO wiki_documents (title,content,created_at,updated_at) VALUES ('서버','Fedora backup','t','t')")
        assert db.execute("SELECT revision FROM wiki_documents WHERE id=1").fetchone()[0] == 1
        assert db.execute("SELECT rowid FROM wiki_documents_fts WHERE wiki_documents_fts MATCH 'Fedora'").fetchone()[0] == 1
        db.execute("UPDATE wiki_documents SET content='Ubuntu restore',revision=2 WHERE id=1")
        assert db.execute("SELECT count(*) FROM wiki_documents_fts WHERE wiki_documents_fts MATCH 'Fedora'").fetchone()[0] == 0
        assert db.execute("SELECT rowid FROM wiki_documents_fts WHERE wiki_documents_fts MATCH 'Ubuntu'").fetchone()[0] == 1
    with pytest.raises(RuntimeError):
        with connect(path) as db:
            db.execute("UPDATE wiki_documents SET content='temporary',revision=3 WHERE id=1")
            raise RuntimeError('rollback')
    with connect(path) as db:
        assert db.execute('SELECT content,revision FROM wiki_documents WHERE id=1').fetchone()[:] == ('Ubuntu restore', 2)
        assert db.execute("SELECT count(*) FROM wiki_documents_fts WHERE wiki_documents_fts MATCH 'temporary'").fetchone()[0] == 0
        db.execute('DELETE FROM wiki_documents WHERE id=1')
        assert db.execute('SELECT count(*) FROM wiki_documents_fts').fetchone()[0] == 0


def test_schema_migration_failure_rolls_back_all_new_objects():
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_documents_fts (id INTEGER)')
        with pytest.raises(sqlite3.OperationalError):
            _apply_integrated_wiki_migration(db)
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='wiki_documents'").fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=17').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='wiki_documents_fts'").fetchone()[0] == 1
    finally:
        db.close()


def test_fts_failure_rolls_back_document_write(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute('DROP TABLE wiki_documents_fts')
    with pytest.raises(sqlite3.OperationalError):
        with connect(path) as db:
            db.execute("INSERT INTO wiki_documents (title,content,created_at,updated_at) VALUES ('서버','내용','t','t')")
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 0


def test_revision_and_document_constraints(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        for revision in (0, -1, 1.5):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO wiki_documents (title,content,revision,created_at,updated_at) VALUES ('title','body',?,'t','t')", (revision,))
