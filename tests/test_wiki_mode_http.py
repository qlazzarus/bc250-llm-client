"""HTTP-level tests use the real worker threads and isolated SQLite."""
import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault('APP_API_KEY', 'test')
os.environ.setdefault('LLM_GATE_API_KEY', 'test')
from backend.app import main
from backend.app.config import get_settings
from backend.app.db import initialize


@pytest.mark.asyncio
async def test_integrated_http_auth_and_concurrent_revision(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', app_auth_mode='key', app_api_key='test', max_request_bytes=65536)
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            unauthorized = await client.get('/api/v1/wiki/documents')
            assert unauthorized.status_code == 401
            assert (await client.get('/api/v1/wiki/capabilities')).status_code == 401
            client.headers['Authorization'] = 'Bearer test'
            assert (await client.get('/api/v1/wiki/capabilities')).json() == {'store_id': 'integrated', 'revision_history': True}
            empty = await client.get('/api/v1/wiki/search', params={'q': 'Fedora'})
            assert empty.status_code == 200 and empty.json() == []
            payload = {'title': '서버', 'content': 'Fedora'}
            created = await client.post('/api/v1/wiki/documents', json=payload)
            assert created.status_code == 201
            assert created.json() == {'id': 1, 'status': 'created', 'revision': 1}
            assert (await client.get('/api/v1/wiki/documents/1')).status_code == 422
            assert (await client.get('/api/v1/wiki/documents/1?store_id=legacy_external')).status_code == 422
            assert (await client.get('/api/v1/wiki/documents/999?store_id=integrated')).status_code == 404
            doc = await client.get('/api/v1/wiki/documents/1?store_id=integrated')
            assert doc.status_code == 200 and doc.json()['revision'] == 1
            async def update(content):
                return await client.put('/api/v1/wiki/documents/1?store_id=integrated',
                                        json={'title': '서버', 'content': content, 'expected_revision': 1})
            responses = await asyncio.gather(update('one'), update('two'))
            assert sorted(r.status_code for r in responses) == [200, 409]
            latest = (await client.get('/api/v1/wiki/documents/1?store_id=integrated')).json()
            assert latest['content'] in ('one', 'two') and latest['revision'] == 2
            assert (await client.delete('/api/v1/wiki/documents/1?store_id=integrated')).status_code == 422
            stale = await client.delete('/api/v1/wiki/documents/1?store_id=integrated&expected_revision=1')
            assert stale.status_code == 409
            deleted = await client.delete('/api/v1/wiki/documents/1?store_id=integrated&expected_revision=2')
            assert deleted.status_code == 200 and deleted.json() == {'status': 'deleted'}
    finally:
        main.app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_alias_http_auth_revision_and_search(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', app_auth_mode='key', app_api_key='test', max_request_bytes=65536)
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            url = '/api/v1/wiki/documents/1/aliases?store_id=integrated'
            assert (await client.get(url)).status_code == 401
            client.headers['Authorization'] = 'Bearer test'
            created = await client.post('/api/v1/wiki/documents', json={'title': 'bc250 ai 서버', 'content': 'Fedora'})
            assert created.status_code == 201
            assert (await client.get(url)).json() == []
            saved = await client.put(url, json={'expected_revision': 1, 'aliases': ['bc250']})
            assert saved.status_code == 200 and saved.json()['revision'] == 2
            assert (await client.put(url, json={'expected_revision': 1, 'aliases': []})).status_code == 409
            assert (await client.get(url)).json() == ['bc250']
            for query in ('bc250', 'bc-250', 'BC 250', 'bc250 ai 서버'):
                response = await client.get('/api/v1/wiki/search', params={'q': query})
                assert response.status_code == 200 and response.json()[0]['id'] == 1
    finally:
        main.app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_memory_summary_omits_bodies_and_detail_is_conversation_scoped(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    from backend.app.db import connect
    with connect(path) as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','private text','t')")
        db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('g','c','u','completed','t','t','t')")
        db.execute("""INSERT INTO wiki_memory_jobs(generation_id,conversation_id,status,source_text,previous_content,proposed_content,created_at,updated_at)
                      VALUES ('g','c','completed','private text','old body','new body','t','t')""")
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', app_auth_mode='key', app_api_key='test', max_request_bytes=65536)
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            endpoint = '/api/v1/conversations/c/wiki-memory'
            assert (await client.get(endpoint + '/g')).status_code == 401
            client.headers['Authorization'] = 'Bearer test'
            summary = (await client.get(endpoint, params={'view': 'summary'})).json()['data'][0]
            assert all(key not in summary for key in ('source_text', 'previous_content', 'proposed_content', 'confirmation_token'))
            assert (await client.get(endpoint)).json()['data'][0]['source_text'] == 'private text'
            assert (await client.get(endpoint + '/g')).json()['data']['previous_content'] == 'old body'
            assert (await client.get('/api/v1/conversations/other/wiki-memory/g')).status_code == 404
            assert (await client.get(endpoint, params={'view': 'bad'})).status_code == 422
    finally:
        main.app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_summary_reduces_history_payload_for_thirty_large_records(tmp_path):
    import json
    from backend.app.db import connect
    path = str(tmp_path / 'client.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c','t','t','t')")
        for index in range(30):
            user, generation = f'u{index}', f'g{index}'
            db.execute("INSERT INTO messages VALUES (?,'c','user','source','t')", (user,))
            db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,'c',?,'completed','t','t','t')", (generation, user))
            db.execute("INSERT INTO wiki_memory_jobs(generation_id,conversation_id,status,previous_content,proposed_content,created_at,updated_at) VALUES (?,'c','completed',?,?,'t','t')", (generation, '가' * 2000, '나' * 2000))
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', app_auth_mode='key', app_api_key='test', max_request_bytes=65536)
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test', headers={'Authorization': 'Bearer test'}) as client:
            path = '/api/v1/conversations/c/wiki-memory'
            full = await client.get(path)
            summary = await client.get(path, params={'view': 'summary'})
            assert full.status_code == summary.status_code == 200
            assert len(summary.json()['data']) == 30
            assert len(summary.content) < len(full.content) // 10
            print(f'30 records: full={len(full.content)} bytes summary={len(summary.content)} bytes')
    finally:
        main.app.dependency_overrides.pop(get_settings, None)
