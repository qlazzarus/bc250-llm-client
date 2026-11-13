import asyncio
import os
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault('APP_API_KEY', 'test')
os.environ.setdefault('LLM_GATE_API_KEY', 'test')
from backend.app import main
from backend.app.config import get_settings
from backend.app.db import _apply_wiki_revision_migration, connect, initialize
from backend.app.wiki_repository import create_document, update_document, delete_document, list_revisions, diff_revisions, WikiNotFound, WikiDiffTooLarge


@pytest.fixture
def path(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    return path


def create(path, content='원래 본문'):
    db = connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            return create_document(db, store_id='integrated', title='서버', content=content)
    finally:
        db.close()


def update(path, doc, content, **kwargs):
    db = connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            return update_document(db, store_id='integrated', document_id=doc['id'], expected_revision=doc['revision'],
                                   title=kwargs.get('title', '새 서버'), content=content, category=kwargs.get('category', 'new'))
    finally:
        db.close()


def test_migration_snapshots_only_current_revision_and_is_repeatable(tmp_path, monkeypatch):
    path = str(tmp_path / 'client.db')
    with monkeypatch.context() as patch:
        patch.setattr('backend.app.db._apply_wiki_revision_migration', lambda db: None)
        initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO wiki_documents(title,content,revision,created_at,updated_at) VALUES ('old','text',5,'t','t')")
    initialize(path)
    initialize(path)
    with connect(path) as db:
        rows = db.execute('SELECT document_id,revision,title FROM wiki_document_revisions').fetchall()
        assert [tuple(row) for row in rows] == [(1, 5, 'old')]
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=19').fetchone()[0] == 1


def test_migration_failure_rolls_back_snapshot_and_schema():
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_documents(id INTEGER PRIMARY KEY,revision INTEGER,title TEXT,content TEXT,category TEXT,updated_at TEXT)')
        db.execute("INSERT INTO wiki_documents VALUES (1,1,'a','b','c','t')")
        db.execute('CREATE TRIGGER wiki_revision_au AFTER UPDATE ON wiki_documents BEGIN SELECT 1; END')
        with pytest.raises(sqlite3.OperationalError):
            _apply_wiki_revision_migration(db)
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='wiki_document_revisions'").fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=19').fetchone()[0] == 0
    finally:
        db.close()


def test_revision_diff_scope_delete_and_rollback(path):
    doc = create(path)
    current = update(path, doc, '새 본문\n두 번째 줄')
    db = connect(path)
    try:
        assert [row['revision'] for row in list_revisions(db, store_id='integrated', document_id=doc['id'])] == [2, 1]
        diff = diff_revisions(db, store_id='integrated', document_id=doc['id'], from_revision=1, to_revision=2)['diff']
        assert '-서버' in diff['title'] and '+새 서버' in diff['title']
        assert '-원래 본문' in diff['content'] and '+새 본문' in diff['content']
        assert '-general' in diff['category'] and '+new' in diff['category']
        with pytest.raises(WikiNotFound):
            diff_revisions(db, store_id='integrated', document_id=doc['id'], from_revision=1, to_revision=3)
        with pytest.raises(WikiNotFound):
            diff_revisions(db, store_id='integrated', document_id=999, from_revision=1, to_revision=2)
        with pytest.raises(RuntimeError):
            with db:
                db.execute('BEGIN IMMEDIATE')
                update_document(db, store_id='integrated', document_id=doc['id'], expected_revision=2, title='x', content='rollback')
                raise RuntimeError('rollback')
        assert [row['revision'] for row in list_revisions(db, store_id='integrated', document_id=doc['id'])] == [2, 1]
        assert db.execute("SELECT count(*) FROM wiki_documents_fts WHERE wiki_documents_fts MATCH 'rollback'").fetchone()[0] == 0
        with db:
            db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'source_deleted','t','t')", (doc['id'],))
        with pytest.raises(WikiNotFound):
            list_revisions(db, store_id='integrated', document_id=doc['id'])
        with db:
            db.execute("DELETE FROM memory_document_quarantine WHERE store_id='integrated' AND wiki_document_id=?", (doc['id'],))
            delete_document(db, store_id='integrated', document_id=doc['id'], expected_revision=current['revision'])
        assert db.execute('SELECT count(*) FROM wiki_document_revisions').fetchone()[0] == 0
        with pytest.raises(WikiNotFound):
            list_revisions(db, store_id='integrated', document_id=doc['id'])
    finally:
        db.close()


def test_revision_retention_and_large_diff(path):
    doc = create(path, '원본')
    for i in range(100):
        doc = update(path, doc, f'변경 {i}')
    db = connect(path)
    try:
        revisions = list_revisions(db, store_id='integrated', document_id=doc['id'])
        assert len(revisions) == 100 and revisions[0]['revision'] == 101 and revisions[-1]['revision'] == 2
        with pytest.raises(WikiNotFound):
            diff_revisions(db, store_id='integrated', document_id=doc['id'], from_revision=1, to_revision=2)
    finally:
        db.close()
    big = create(path, '\n'.join(['a'] * 2100))
    update(path, big, '\n'.join(['b'] * 2100))
    db = connect(path)
    try:
        with pytest.raises(WikiDiffTooLarge):
            diff_revisions(db, store_id='integrated', document_id=big['id'], from_revision=1, to_revision=2)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_http_revision_auth_and_quarantine(path):
    doc = create(path)
    update(path, doc, '변경')
    large = create(path, '\n'.join(['a'] * 2100))
    update(path, large, '\n'.join(['b'] * 2100))
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', app_auth_mode='key', app_api_key='test', max_request_bytes=65536)
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            url = f'/api/v1/wiki/documents/{doc["id"]}'
            assert (await client.get(url + '/revisions?store_id=integrated')).status_code == 401
            client.headers['Authorization'] = 'Bearer test'
            assert (await client.get(url + '/revisions?store_id=integrated')).status_code == 200
            response = await client.get(url + '/diff?store_id=integrated&from_revision=1&to_revision=2')
            assert response.status_code == 200 and '+변경' in response.json()['diff']['content']
            assert (await client.get(url + '/diff?store_id=integrated&from_revision=1&to_revision=99')).status_code == 404
            too_large = await client.get(f'/api/v1/wiki/documents/{large["id"]}/diff?store_id=integrated&from_revision=1&to_revision=2')
            assert too_large.status_code == 413 and too_large.json()['error']['code'] == 'wiki_diff_too_large'
            assert (await client.get(url + '/revisions?store_id=legacy_external')).status_code == 422
            with connect(path) as db:
                db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'source_deleted','t','t')", (doc['id'],))
            assert (await client.get(url + '/revisions?store_id=integrated')).status_code == 404
            assert (await client.get(url + '/diff?store_id=integrated&from_revision=1&to_revision=2')).status_code == 404
    finally:
        main.app.dependency_overrides.pop(get_settings, None)
