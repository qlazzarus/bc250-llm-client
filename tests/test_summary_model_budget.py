"""Only the measured chronological prefix advances the summary cursor."""
import asyncio
import os
os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')
import httpx
import pytest
from backend.app import main
from backend.app.config import Settings
from backend.app.db import connect, initialize


@pytest.fixture(autouse=True)
def supported_test_features(monkeypatch):
    monkeypatch.setattr(main, 'require_model_capabilities', lambda *_: None)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['none', 'too_large', 'stale', 'missing', 'summary_changed'])
async def test_summary_selects_whole_turns_and_preserves_cursor_on_failure(tmp_path, monkeypatch, failure):
    settings = Settings(_env_file=None, model_control_enabled=True, database_path=str(tmp_path / 'db'),
                        llm_gate_url='http://gate', llm_context_tokens=600,
                        llm_context_safety_tokens=100, roleplay_summary_max_tokens=100)
    initialize(settings.database_path)
    with connect(settings.database_path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,summary) VALUES ('c','title','t','t','old')")
        for i, (role, content) in enumerate([('user','one'), ('assistant','answer one'), ('user','two'), ('assistant','answer two')]):
            db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?)', (f'm{i}', 'c', role, content, str(i)))
        db.execute("INSERT INTO summary_jobs VALUES ('job','c',NULL,'m3','pending',NULL,'t','t')")
    async def resolve(_settings): return main.GateModel('model', 'a' * 32, 600)
    monkeypatch.setattr(main, 'resolve_gate_model', resolve)
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    requests = []
    def handle(request):
        import json
        body = json.loads(request.content)
        requests.append((request.url.path, body, request.headers.get('x-model-epoch')))
        if request.url.path.endswith('input_tokens'):
            if failure == 'stale': return httpx.Response(409, json={})
            if failure == 'missing': return httpx.Response(404, json={})
            count = 600 if failure == 'too_large' or 'answer two' in body['messages'][1]['content'] else 100
            return httpx.Response(200, json={'input_tokens': count})
        if failure == 'summary_changed':
            with connect(settings.database_path) as db:
                db.execute("UPDATE conversations SET summary='edited' WHERE id='c'")
        return httpx.Response(200, json={'choices': [{'message': {'content': 'new summary'}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    await main.run_summary_job(settings, 'job')
    with connect(settings.database_path) as db:
        conversation = db.execute("SELECT * FROM conversations WHERE id='c'").fetchone()
        job = db.execute("SELECT * FROM summary_jobs WHERE id='job'").fetchone()
    assert all(epoch == 'a' * 32 for _, _, epoch in requests)
    if failure == 'none':
        assert conversation['summary'] == 'new summary' and conversation['summary_up_to_message_id'] == 'm1'
        assert job['status'] == 'completed' and job['through_message_id'] == 'm1'
        sent = [body for path, body, _ in requests if path.endswith('/v1/chat/completions')]
        assert len(sent) == 1 and 'answer one' in sent[0]['messages'][1]['content'] and 'answer two' not in sent[0]['messages'][1]['content']
    else:
        assert conversation['summary'] == ('edited' if failure == 'summary_changed' else 'old')
        assert conversation['summary_up_to_message_id'] is None and job['status'] == 'failed'
        if failure != 'summary_changed': assert all(path.endswith('/input_tokens') for path, _, _ in requests)
