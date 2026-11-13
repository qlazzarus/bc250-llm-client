import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault('APP_API_KEY', 'test')
os.environ.setdefault('LLM_GATE_API_KEY', 'test')
from backend.app import main
from backend.app.db import connect, initialize


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    return path, SimpleNamespace(database_path=path, wiki_store='integrated')


def run(coro):
    return asyncio.run(coro)


def test_capabilities_reports_selected_store(setup):
    _, settings = setup
    assert main.wiki_capabilities(settings) == {'store_id': 'integrated', 'revision_history': True}
    settings.wiki_store = 'legacy_external'
    assert main.wiki_capabilities(settings) == {'store_id': 'legacy_external', 'revision_history': False}


def test_integrated_crud_search_revision_and_namespace(setup, monkeypatch):
    _, settings = setup
    async def no_external(*args, **kwargs):
        raise AssertionError('integrated mode must not contact old Wiki')
    monkeypatch.setattr(main, 'wiki_request', no_external)
    body = main.WikiDocument(title='서버', content='Fedora')
    assert run(main.wiki_search(q='Fedora', limit=5, settings=settings)) == []
    assert run(main.wiki_create(body, settings)) == {'id': 1, 'status': 'created', 'revision': 1}
    for store in (None, 'legacy_external'):
        with pytest.raises(HTTPException) as exc:
            run(main.wiki_get(1, store, settings))
        assert exc.value.status_code == 422
    assert run(main.wiki_get(1, 'integrated', settings))['content'] == 'Fedora'
    updated = main.WikiDocument(title='서버', content='Ubuntu', expected_revision=1)
    assert run(main.wiki_update(1, updated, 'integrated', settings))['revision'] == 2
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_update(1, updated, 'integrated', settings))
    assert exc.value.status_code == 409
    assert run(main.wiki_search(q='Ubuntu', limit=5, settings=settings))[0]['id'] == 1
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_delete(1, 'integrated', 1, settings))
    assert exc.value.status_code == 409
    assert run(main.wiki_delete(1, 'integrated', 2, settings)) == {'status': 'deleted'}
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_get(1, 'integrated', settings))
    assert exc.value.status_code == 404


def test_legacy_mode_preserves_api_and_rejects_integrated_id(setup, monkeypatch):
    _, settings = setup
    settings.wiki_store = 'legacy_external'
    calls = []
    async def fake(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {'id': 1, 'updated_at': 'v1'} if method == 'GET' else {'id': 1, 'status': 'updated'}
    monkeypatch.setattr(main, 'wiki_request', fake)
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_get(1, 'integrated', settings))
    assert exc.value.status_code == 422
    body = main.WikiDocument(title='title', content='text', expected_updated_at='v1')
    assert run(main.wiki_update(1, body, None, settings)) == {'id': 1, 'status': 'updated'}
    assert [call[:2] for call in calls] == [('GET', '/api/documents/1'), ('PUT', '/api/documents/1')]
    assert 'expected_revision' not in calls[1][2]['json']


def test_legacy_timestamp_precondition_is_atomic_in_integrated_mode(setup):
    _, settings = setup
    created = run(main.wiki_create(main.WikiDocument(title='title', content='old'), settings))
    original = run(main.wiki_get(created['id'], 'integrated', settings))
    body = main.WikiDocument(title='title', content='new', expected_updated_at=original['updated_at'])
    assert run(main.wiki_update(created['id'], body, 'integrated', settings))['revision'] == 2
    assert run(main.wiki_get(created['id'], 'integrated', settings))['updated_at'] != original['updated_at']
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_update(created['id'], body, 'integrated', settings))
    assert exc.value.status_code == 409


def test_integrated_database_error_is_not_empty_result(setup):
    path, settings = setup
    with connect(path) as db:
        db.execute('DROP TABLE wiki_documents_fts')
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_search(q='Fedora', limit=5, settings=settings))
    assert (exc.value.status_code, exc.value.detail['code']) == (503, 'wiki_unavailable')


def test_integrated_mode_does_not_run_legacy_pending_job(setup, monkeypatch):
    path, settings = setup
    with connect(path) as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c','x','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','test','t')")
        db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('g','c','u','completed','t','t','t')")
        db.execute("INSERT INTO wiki_memory_jobs(generation_id,conversation_id,status,created_at,updated_at) VALUES ('g','c','pending','t','t')")
    async def no_external(*args, **kwargs):
        raise AssertionError('old writer must not run')
    monkeypatch.setattr(main, 'wiki_request', no_external)
    run(main.run_wiki_memory_job(settings, 'g'))
    with connect(path) as db:
        row = db.execute("SELECT store_id,status,reason_code FROM wiki_memory_jobs WHERE generation_id='g'").fetchone()
        assert tuple(row) == ('legacy_external', 'pending', None)


def test_managed_documents_hidden_and_protected(setup):
    path, settings = setup
    with connect(path) as db:
        db.execute("INSERT INTO wiki_documents(id,title,content,created_at,updated_at) VALUES (1,'private','secret','t','t')")
        db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',1,'source_deleted','t','t')")
    assert run(main.wiki_list(settings)) == []
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_get(1, 'integrated', settings))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_update(1, main.WikiDocument(title='x', content='x', expected_revision=1), 'integrated', settings))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        run(main.wiki_delete(1, 'integrated', 1, settings))
    assert exc.value.status_code == 409


def test_rag_search_keeps_legacy_and_integrated_ids_separate(setup, monkeypatch):
    path, settings = setup
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        from backend.app.wiki_repository import create_document, replace_aliases
        doc = create_document(db, store_id='integrated', title='통합 서버', content='integrated')
        replace_aliases(db, store_id='integrated', document_id=doc['id'], expected_revision=1, aliases=['bc250'])
        db.execute("INSERT INTO memory_document_quarantine VALUES ('legacy_external',?,'owned','t','t')", (doc['id'],))
    async def legacy(method, url, **kwargs):
        assert (method, url) == ('GET', '/api/search')
        return [{'id': doc['id'], 'title': '구 서버', 'content': 'legacy'}]
    monkeypatch.setattr(main, 'wiki_request', legacy)
    assert run(main._wiki_search(settings, 'bc250', 3))[0]['content'] == 'integrated'
    settings.wiki_store = 'legacy_external'
    assert run(main._wiki_search(settings, 'bc250', 3))[0]['content'] == 'legacy'
    settings.wiki_store = 'integrated'
    with connect(path) as db:
        db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'source_deleted','t','t')", (doc['id'],))
    assert run(main._wiki_search(settings, 'bc250', 3)) == []
