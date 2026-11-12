"""Background preparation waits for the new epoch without replaying inference."""
import asyncio
import json
import os

os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')
import httpx
import pytest
from backend.app import main
from backend.app.config import Settings
from backend.app.db import connect, initialize


@pytest.mark.asyncio
@pytest.mark.parametrize('job', ['title', 'wiki'])
async def test_background_preflight_waits_for_switch_and_uses_new_epoch(tmp_path, monkeypatch, job):
    settings = Settings(_env_file=None, model_control_enabled=True, database_path=str(tmp_path / 'db'),
                        llm_gate_url='http://gate')
    initialize(settings.database_path)
    with connect(settings.database_path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,title_pending) VALUES ('c','새 대화','t','t',1)")
        if job == 'wiki':
            db.execute("INSERT INTO messages VALUES ('u','c','user','I like tea','t')")
            db.execute("INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('g','c','u','completed','t','t','t')")
            db.execute("INSERT INTO wiki_memory_jobs (generation_id,conversation_id,status,created_at,updated_at) VALUES ('g','c','pending','t','t')")
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    main.app.state.wiki_memory_lock = asyncio.Lock()
    monkeypatch.setattr(main, 'BACKGROUND_MODEL_RECHECK_SECONDS', .01)
    hold_seen, release = asyncio.Event(), asyncio.Event()
    async def resolve(_settings):
        if not release.is_set():
            hold_seen.set()
            raise main.epoch_mismatch()
        model = main.GateModel('model-b', 'b' * 32, 4096)
        model.capabilities = {'reasoning_control': 'supported', 'json_schema': 'supported'}
        return model
    wiki_calls = []
    async def wiki_request(method, path, **kwargs):
        wiki_calls.append(method)
        assert method == 'GET'
        return []
    calls = []
    def gate(request):
        calls.append((request.url.path, request.headers.get('x-model-epoch')))
        assert request.headers.get('x-model-epoch') == 'b' * 32
        if request.url.path.endswith('/input_tokens'): return httpx.Response(200, json={'input_tokens': 20})
        if job == 'title': result = 'new title'
        else: result = json.dumps({'action': 'none', 'kind': 'uncertain', 'reason': 'insufficient_context', 'source_text': '', 'source_message_id': None, 'title': '', 'document_id': None, 'old_text': None})
        return httpx.Response(200, json={'choices': [{'message': {'content': result}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(main, 'resolve_gate_model', resolve)
    monkeypatch.setattr(main, 'wiki_request', wiki_request)
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(gate), **kw))
    task = asyncio.create_task(main.generate_conversation_title(settings, 'c', 'hello') if job == 'title'
                               else main.run_wiki_memory_job(settings, 'g'))
    await asyncio.wait_for(hold_seen.wait(), 1)
    await asyncio.sleep(.03)
    assert not calls and not task.done()
    with connect(settings.database_path) as db:
        if job == 'title': assert db.execute("SELECT title_pending FROM conversations WHERE id='c'").fetchone()[0] == 1
        else: assert db.execute("SELECT status FROM wiki_memory_jobs WHERE generation_id='g'").fetchone()[0] == 'running'
    release.set()
    await asyncio.wait_for(task, 1)
    assert [path for path, _ in calls].count('/v1/chat/completions') == 1
    if job == 'wiki':
        assert len(wiki_calls) >= 2  # Re-read search results after waiting.
        with connect(settings.database_path) as db:
            row = db.execute("SELECT status,memory_kind,reason_code,needs_attention,policy_version FROM wiki_memory_jobs WHERE generation_id='g'").fetchone()
        assert tuple(row) == ('skipped', 'uncertain', 'insufficient_context', 0, 1)
    else:
        with connect(settings.database_path) as db:
            assert db.execute("SELECT title FROM conversations WHERE id='c'").fetchone()[0] == 'new title'


@pytest.mark.asyncio
async def test_background_preparation_does_not_retry_unknown_failures(monkeypatch):
    attempts = 0
    async def prepare():
        nonlocal attempts
        attempts += 1
        raise main.fail('gate_unavailable', 'unknown', 503)
    with pytest.raises(main.HTTPException): await main.wait_for_background_preparation(prepare)
    assert attempts == 1


@pytest.mark.asyncio
async def test_summary_edited_during_hold_never_sends_stale_inference(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, model_control_enabled=True, database_path=str(tmp_path / 'db'),
                        llm_gate_url='http://gate', llm_context_tokens=1024,
                        llm_context_safety_tokens=100, roleplay_summary_max_tokens=100)
    initialize(settings.database_path)
    with connect(settings.database_path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,summary) VALUES ('c','title','t','t','old')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','question','0')")
        db.execute("INSERT INTO messages VALUES ('a','c','assistant','answer','1')")
        db.execute("INSERT INTO summary_jobs VALUES ('job','c',NULL,'a','pending',NULL,'t','t')")
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    monkeypatch.setattr(main, 'BACKGROUND_MODEL_RECHECK_SECONDS', .01)
    hold_seen, release = asyncio.Event(), asyncio.Event()
    async def resolve(_settings):
        if not release.is_set():
            hold_seen.set(); raise main.epoch_mismatch()
        model = main.GateModel('new', 'b' * 32, 1024)
        model.capabilities = {'reasoning_control': 'supported', 'json_schema': 'supported'}
        return model
    calls = []
    def gate(request):
        calls.append(request.url.path)
        assert request.url.path.endswith('/input_tokens')
        return httpx.Response(200, json={'input_tokens': 20})
    original = httpx.AsyncClient
    monkeypatch.setattr(main, 'resolve_gate_model', resolve)
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(gate), **kw))
    task = asyncio.create_task(main.run_summary_job(settings, 'job'))
    await asyncio.wait_for(hold_seen.wait(), 1)
    with connect(settings.database_path) as db:
        db.execute("UPDATE conversations SET summary='manual edit' WHERE id='c'")
    release.set()
    await asyncio.wait_for(task, 1)
    with connect(settings.database_path) as db:
        conversation = db.execute("SELECT summary,summary_up_to_message_id FROM conversations WHERE id='c'").fetchone()
        job = db.execute("SELECT status FROM summary_jobs WHERE id='job'").fetchone()
    assert conversation['summary'] == 'manual edit' and conversation['summary_up_to_message_id'] is None
    assert job['status'] == 'failed' and calls == ['/v1/chat/completions/input_tokens']


def test_legacy_gate_cannot_trigger_admission_retry():
    response = httpx.Response(409, request=httpx.Request('POST', 'http://gate'),
                              headers={'X-BC250-Admission': 'rejected'})
    with pytest.raises(httpx.HTTPStatusError):
        main.raise_if_not_admitted(response, enabled=False)
