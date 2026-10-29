import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.app.db import connect, initialize
from backend.app.wiki_memory import parse_decision


def test_decision_requires_exact_user_source_and_searched_document():
    source = '나는 녹차를 좋아해.'
    decision = {'action': 'append', 'source_text': '녹차를 좋아해', 'title': '', 'document_id': 42}
    assert parse_decision(json.dumps(decision), source, [{'id': 42}]).document_id == 42
    with pytest.raises(ValueError):
        parse_decision(json.dumps({**decision, 'source_text': '커피를 좋아해'}), source, [{'id': 42}])
    with pytest.raises(ValueError):
        parse_decision(json.dumps(decision), source, [{'id': 43}])


def test_jobs_created_only_for_enabled_completed_quick_chat(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app.main import complete_generation
    path = str(tmp_path / 'test.db')
    initialize(path)
    timestamp = '2026-01-01T00:00:00+00:00'
    with connect(path) as db:
        for mode in ('quick', 'roleplay', 'image_prompt'):
            db.execute('INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES (?,?,?,?,?)', (mode, mode, timestamp, timestamp, mode))
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', (mode + '-user', mode, 'user', '내용', timestamp))
            db.execute('INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)', (mode + '-gen', mode, mode + '-user', 'pending', timestamp, timestamp, timestamp))
    settings = SimpleNamespace(database_path=path)
    for mode in ('quick', 'roleplay', 'image_prompt'):
        complete_generation(settings, mode + '-gen', '답변', 'stop', None, True)
    with connect(path) as db:
        rows = db.execute('SELECT generation_id,status FROM wiki_memory_jobs').fetchall()
    assert [(row['generation_id'], row['status']) for row in rows] == [('quick-gen', 'pending')]
    assert complete_generation(settings, 'quick-gen', '다시', 'stop', None, True) is None


def test_unknown_write_result_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app import main
    path = str(tmp_path / 'test.db')
    initialize(path)
    timestamp = '2026-01-01T00:00:00+00:00'
    with connect(path) as db:
        db.execute('INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES (?,?,?,?,?)', ('c', 'c', timestamp, timestamp, 'quick'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', ('u', 'c', 'user', '나는 녹차를 좋아해', timestamp))
        db.execute('INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)', ('g', 'c', 'u', 'completed', timestamp, timestamp, timestamp))
        db.execute('INSERT INTO wiki_memory_jobs (generation_id,conversation_id,status,created_at,updated_at) VALUES (?,?,?,?,?)', ('g', 'c', 'pending', timestamp, timestamp))
    calls = []
    async def wiki_request(method, path, **kwargs):
        calls.append(method)
        if method == 'GET': return []
        raise main.fail('wiki_unavailable', 'timeout', 503)
    async def model(_settings): return 'model'
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'message': {'content': json.dumps({'action': 'create', 'source_text': '녹차를 좋아해', 'title': '차 선호', 'document_id': None})}}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(main, 'wiki_request', wiki_request)
    monkeypatch.setattr(main, 'resolve_gate_model', model)
    monkeypatch.setattr(main.httpx, 'AsyncClient', Client)
    main.app.state.wiki_memory_lock = asyncio.Lock()
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    settings = SimpleNamespace(database_path=path, llm_context_tokens=16384, llm_context_safety_tokens=512, max_completion_tokens=1024, llm_gate_url='http://localhost', llm_gate_api_key='x', llm_timeout_seconds=1)
    asyncio.run(main.run_wiki_memory_job(settings, 'g'))
    asyncio.run(main.run_wiki_memory_job(settings, 'g'))
    with connect(path) as db:
        row = db.execute('SELECT status,source_text FROM wiki_memory_jobs').fetchone()
    assert row['status'] == 'unknown'
    assert row['source_text'] == '녹차를 좋아해'
    assert calls == ['GET', 'POST']
