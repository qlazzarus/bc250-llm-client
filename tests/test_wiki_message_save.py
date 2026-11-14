import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from backend.app import main
from backend.app.db import connect, initialize


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / 'db.sqlite')
    initialize(path)
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c','c','t','t')")
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('other','c','t','t')")
        db.execute("INSERT INTO messages VALUES ('m','c','user','original','t')")
        db.execute("INSERT INTO messages VALUES ('a','c','assistant','answer','t')")
    return path, SimpleNamespace(database_path=path, wiki_store='integrated')


def body(**kwargs):
    return main.WikiMessageSave(**({'request_id': uuid4(), 'conversation_id': 'c', 'message_id': 'm', 'title': 'BC-250', 'content': 'content', 'source': 'user source', 'action': 'create'} | kwargs))


def save(settings, data):
    return asyncio.run(main.wiki_message_save(data, settings))


def test_concurrent_replay_conflict_and_source_deletion(setup):
    path, settings = setup
    data = body()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: save(settings, data), range(4)))
    assert results == [results[0]] * 4
    with pytest.raises(HTTPException) as exc:
        save(settings, data.model_copy(update={'content': 'different'}))
    assert (exc.value.status_code, exc.value.detail['code']) == (409, 'wiki_request_conflict')
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1
        assert db.execute('SELECT source_text FROM wiki_document_sources').fetchone()[0] == 'user source'
        db.execute("DELETE FROM conversations WHERE id='c'")
    assert save(settings, data) == results[0]
    with connect(path) as db:
        assert db.execute('SELECT source_message_id FROM wiki_document_sources').fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 1


def test_source_duplicate_revision_and_quarantine(setup):
    path, settings = setup
    for data in (body(message_id='a'), body(conversation_id='other'), body(message_id='missing')):
        with pytest.raises(HTTPException) as exc:
            save(settings, data)
        assert exc.value.status_code == 404
    first = save(settings, body())
    candidates = asyncio.run(main.wiki_message_candidates(title='bc 250', settings=settings))
    assert [item['id'] for item in candidates['matches']] == [first['id']]
    with pytest.raises(HTTPException) as exc:
        save(settings, body(title='bc 250'))
    assert exc.value.detail['code'] == 'wiki_duplicate_title'
    second = save(settings, body(title='bc 250', acknowledged_duplicate_ids=[first['id']]))
    assert second['id'] != first['id']
    update = body(action='update', document_id=first['id'], expected_revision=1)
    assert save(settings, update)['revision'] == 2
    with pytest.raises(HTTPException) as exc:
        save(settings, body(action='update', document_id=first['id'], expected_revision=1))
    assert exc.value.detail['code'] == 'wiki_conflict'
    with connect(path) as db:
        db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'owned','t','t')", (second['id'],))
    assert [item['id'] for item in asyncio.run(main.wiki_message_candidates(title='bc 250', settings=settings))['matches']] == [first['id']]
    with pytest.raises(HTTPException):
        save(settings, body(action='update', document_id=second['id'], expected_revision=1))


def test_atomic_rollback_and_legacy_rejected(setup, monkeypatch):
    path, settings = setup
    def fail(*args, **kwargs):
        raise RuntimeError('injected')
    monkeypatch.setattr(main, 'now', fail)
    with pytest.raises(RuntimeError):
        save(settings, body())
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM wiki_documents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM wiki_message_save_receipts').fetchone()[0] == 0
    settings.wiki_store = 'legacy_external'
    with pytest.raises(HTTPException) as exc:
        save(settings, body())
    assert exc.value.status_code == 422
