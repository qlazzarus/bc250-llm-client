"""Fake Wiki and mock LLM coverage for the client-visible memory history contract."""
import asyncio
import json
import os
from types import SimpleNamespace
import pytest
from fastapi import HTTPException

os.environ.setdefault('APP_API_KEY', 'test')
os.environ.setdefault('LLM_GATE_API_KEY', 'test')

from backend.app import main
from backend.app.db import connect, initialize


def _prepare_history_database(tmp_path, monkeypatch):
    path = str(tmp_path / 'memory-history.db')
    initialize(path)
    return path


@pytest.mark.parametrize('invalid_write', [False, True])
def test_fake_wiki_memory_write_is_visible_through_history_api(tmp_path, monkeypatch, invalid_write):
    path = _prepare_history_database(tmp_path, monkeypatch)
    timestamp = '2026-11-12T00:00:00+00:00'
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES ('c','대화',?,?, 'quick')", (timestamp, timestamp))
        db.execute("INSERT INTO messages VALUES ('u','c','user','나는 홍차를 좋아해',?)", (timestamp,))
        db.execute("""INSERT INTO generation_requests
            (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at)
            VALUES ('g','c','u','completed',?,?,?)""", (timestamp, timestamp, timestamp))
        db.execute("INSERT INTO wiki_memory_jobs (generation_id,conversation_id,status,created_at,updated_at) VALUES ('g','c','pending',?,?)", (timestamp, timestamp))

    writes = []

    async def wiki_request(method, path, **kwargs):
        if method == 'GET':
            return []
        assert (method, path) == ('POST', '/api/documents')
        writes.append(kwargs['json'])
        if invalid_write:
            raise HTTPException(502, {'code': 'wiki_invalid_response', 'message': 'invalid'})
        return {'id': 17}

    async def resolve_gate_model(_settings):
        return 'mock-model'

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': json.dumps({
                'action': 'create', 'kind': 'preference', 'reason': 'durable',
                'source_text': '홍차를 좋아해', 'source_message_id': 'u',
                'title': '차 선호', 'document_id': None, 'old_text': None,
            })}}]}

    class MockGateClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(main, 'wiki_request', wiki_request)
    monkeypatch.setattr(main, 'resolve_gate_model', resolve_gate_model)
    monkeypatch.setattr(main.httpx, 'AsyncClient', MockGateClient)
    main.app.state.wiki_memory_lock = asyncio.Lock()
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    settings = SimpleNamespace(model_control_enabled=False, database_path=path, llm_context_tokens=16384,
                               llm_context_safety_tokens=512, max_completion_tokens=1024,
                               llm_gate_url='http://gate', llm_gate_api_key='test', llm_timeout_seconds=1)
    asyncio.run(main.run_wiki_memory_job(settings, 'g'))

    row = asyncio.run(main.wiki_memory_history('c', SimpleNamespace(database_path=path)))['data'][0]
    assert writes == [{'title': '차 선호', 'category': 'general', 'content': '홍차를 좋아해'}]
    if invalid_write:
        assert (row['status'], row['reason_code'], row['needs_attention']) == ('unknown', 'write_unknown', 1)
    else:
        assert (row['status'], row['source_text'], row['memory_kind'], row['reason_code'], row['needs_attention']) == (
            'completed', '홍차를 좋아해', 'preference', 'stored', 0,
        )


def test_question_completion_has_no_history_entry_for_ui(tmp_path, monkeypatch):
    path = _prepare_history_database(tmp_path, monkeypatch)
    timestamp = '2026-11-12T00:00:00+00:00'
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES ('c','대화',?,?, 'quick')", (timestamp, timestamp))
        db.execute("INSERT INTO messages VALUES ('u','c','user','iPhone 12 Pro Max가 어떤 기종이야?',?)", (timestamp,))
        db.execute("""INSERT INTO generation_requests
            (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at)
            VALUES ('g','c','u','pending',?,?,?)""", (timestamp, timestamp, timestamp))
    main.complete_generation(SimpleNamespace(database_path=path), 'g', '설명', 'stop', None, True)

    assert asyncio.run(main.wiki_memory_history('c', SimpleNamespace(database_path=path))) == {'data': []}
