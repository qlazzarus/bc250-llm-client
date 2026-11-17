"""Strict context preservation: never infer after silently dropping history."""
import asyncio
import os
os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')
import httpx
import pytest
from backend.app import main
from backend.app.config import Settings
from backend.app.db import connect, initialize


def setup_chat(tmp_path, **overrides):
    settings = Settings(_env_file=None, database_path=str(tmp_path / 'db'), **overrides)
    initialize(settings.database_path)
    with connect(settings.database_path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','chat','t','t')")
        for i in range(22):
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', (f'm{i:03}', 'c', 'user' if i % 2 == 0 else 'assistant', f'fact {i}', f'{i:03}'))
    return settings


def test_begin_generation_retains_all_unsummarized_messages(tmp_path):
    settings = setup_chat(tmp_path, max_history_messages=20)
    _, _, history, _ = main.begin_generation('c', main.MessageCreate(content='next'), settings)
    assert len(history) == 22
    assert history[0]['content'] == 'fact 0'


@pytest.mark.asyncio
async def test_fit_never_drops_turns(tmp_path):
    settings = setup_chat(tmp_path, model_control_enabled=True, llm_context_tokens=200, llm_context_safety_tokens=50)
    payload = {'max_tokens': 50, 'messages': [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'fact'}, {'role': 'user', 'content': 'next'}]}
    original = list(payload['messages'])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'input_tokens': 150}))) as client:
        with pytest.raises(main.HTTPException):
            await main.fit_chat_payload(settings, payload, client, main.GateModel('m', 'a' * 32, 200))
    assert payload['messages'] == original


def mock_gate(monkeypatch, settings, *, response=None, count=None):
    import json
    calls = []
    async def resolve(_):
        return main.GateModel('model', 'a' * 32, settings.llm_context_tokens) if settings.model_control_enabled else 'model'
    monkeypatch.setattr(main, 'resolve_gate_model', resolve)
    monkeypatch.setattr(main, 'require_model_capabilities', lambda *_: None)
    main.app.state.generation_limiter = asyncio.Semaphore(2)
    main.app.state.summary_tasks = set()
    def handle(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path.endswith('input_tokens'):
            return httpx.Response(200, json={'input_tokens': count(payload) if count else 100})
        if response:
            return response(payload)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'saved facts'}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    return calls


def state(settings):
    with connect(settings.database_path) as db:
        return dict(db.execute("SELECT * FROM conversations WHERE id='c'").fetchone())


@pytest.mark.asyncio
async def test_message_cap_compacts_before_request_without_deleting_or_duplicating(tmp_path, monkeypatch):
    settings = setup_chat(tmp_path, max_history_messages=20)
    calls = mock_gate(monkeypatch, settings)
    conversation, plan, payload = await main.prepare_chat_context(settings, 'c', 'current question', 'model', .7, False)
    assert conversation['summary_up_to_message_id'] == 'm009'
    contents = [m['content'] for m in payload['messages']]
    assert not any(c == 'fact 0' for c in contents)
    assert contents[-13:-1] == [f'fact {i}' for i in range(10, 22)]
    assert 'saved facts' in contents[1]
    assert contents[-1] == 'current question'
    assert all('current question' not in str(p) for path, p in calls if path.endswith('/completions'))
    with connect(settings.database_path) as db:
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 22


@pytest.mark.asyncio
async def test_small_number_of_long_turns_compacts_with_wiki_and_output_reservation(tmp_path, monkeypatch):
    settings = setup_chat(tmp_path, model_control_enabled=True, llm_context_tokens=3000, llm_context_safety_tokens=100)
    with connect(settings.database_path) as db:
        db.execute("DELETE FROM messages WHERE id>'m003'")
        db.execute("UPDATE conversations SET summary='prior facts',response_length='long'")
    # The first turn does not fit in chat, but can be summarized. Wiki is retained.
    def count(payload):
        is_chat = payload['messages'][-1]['content'] == 'question + Wiki evidence'
        return 1000 if is_chat and any(m['content'] == 'fact 0' for m in payload['messages']) else 100
    mock_gate(monkeypatch, settings, count=count)
    model = main.GateModel('model', 'a' * 32, 3000)
    conversation, plan, payload = await main.prepare_chat_context(settings, 'c', 'question + Wiki evidence', model, .7, False)
    assert conversation['summary_up_to_message_id'] == 'm001'
    assert payload['max_tokens'] == 2048
    assert [m['content'] for m in payload['messages']][-3:] == ['fact 2', 'fact 3', 'question + Wiki evidence']
    assert count(payload) + payload['max_tokens'] + 100 <= 3000


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['length', 'missing_reason', 'empty', 'wrong_type', 'malformed', 'timeout'])
async def test_invalid_summary_keeps_cursor_and_bounded_retries(tmp_path, monkeypatch, bad):
    settings = setup_chat(tmp_path, summary_job_max_attempts=2)
    def response(payload):
        if bad == 'timeout':
            raise httpx.ReadTimeout('timeout')
        choice = {'finish_reason': 'length' if bad == 'length' else 'stop', 'message': {'content': 'partial'}}
        if bad == 'missing_reason': choice.pop('finish_reason')
        if bad == 'empty': choice['message']['content'] = ' '
        if bad == 'wrong_type': choice['message']['content'] = ['invalid']
        return httpx.Response(200, json={'choices': {'invalid': 1}} if bad == 'malformed' else {'choices': [choice]})
    calls = mock_gate(monkeypatch, settings, response=response)
    job_id = main.queue_roleplay_summary(settings, 'c', force=True)
    await main.run_summary_job(settings, job_id)
    assert state(settings)['summary'] == ''
    assert state(settings)['summary_up_to_message_id'] is None
    assert len(calls) == 2
    with connect(settings.database_path) as db:
        assert db.execute('SELECT attempt_count FROM summary_job_state').fetchone()[0] == 2
        assert db.execute('SELECT status FROM summary_jobs').fetchone()[0] == 'failed'
    assert main.queue_roleplay_summary(settings, 'c', force=True) is not None


@pytest.mark.asyncio
async def test_truncated_summary_retries_smaller_prefix(tmp_path, monkeypatch):
    settings = setup_chat(tmp_path)
    attempts = []
    def response(payload):
        attempts.append(payload['messages'][1]['content'])
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length' if len(attempts) == 1 else 'stop', 'message': {'content': 'complete summary'}}]})
    mock_gate(monkeypatch, settings, response=response)
    job_id = main.queue_roleplay_summary(settings, 'c', force=True)
    await main.run_summary_job(settings, job_id)
    assert len(attempts) == 2 and len(attempts[1]) < len(attempts[0])
    assert state(settings)['summary_up_to_message_id'] == 'm003'


@pytest.mark.asyncio
async def test_manual_edit_and_optimistic_conflict(tmp_path, monkeypatch):
    settings = setup_chat(tmp_path)
    def response(payload):
        main.update_conversation('c', main.ConversationUpdate(summary='manual', expected_summary_revision=0), settings)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': 'obsolete'}}]})
    mock_gate(monkeypatch, settings, response=response)
    job_id = main.queue_roleplay_summary(settings, 'c', force=True)
    await main.run_summary_job(settings, job_id)
    assert state(settings)['summary'] == 'manual'
    assert state(settings)['summary_up_to_message_id'] is None
    with pytest.raises(main.HTTPException) as error:
        main.update_conversation('c', main.ConversationUpdate(summary='stale', expected_summary_revision=0), settings)
    assert error.value.status_code == 409
    assert state(settings)['summary_revision'] == 1


@pytest.mark.asyncio
async def test_failed_compaction_may_use_full_history_but_never_partial(tmp_path, monkeypatch):
    settings = setup_chat(tmp_path, max_history_messages=20, model_control_enabled=True, llm_context_tokens=2000, summary_job_max_attempts=1)
    count_value = [100]
    def response(payload): return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]})
    calls = mock_gate(monkeypatch, settings, response=response, count=lambda _: count_value[0])
    model = main.GateModel('model', 'a' * 32, 2000)
    _, _, payload = await main.prepare_chat_context(settings, 'c', 'next', model, .7, False)
    assert len(payload['messages']) == 24
    count_value[0] = 2000
    with pytest.raises(main.HTTPException) as error:
        await main.prepare_chat_context(settings, 'c', 'next', model, .7, False)
    assert error.value.detail['code'] == 'context_preservation_failed'
    assert state(settings)['summary_up_to_message_id'] is None
    # Overlarge first turn does not skip forward or issue inference.
    assert len([p for path, p in calls if path.endswith('/completions')]) == 1


@pytest.mark.asyncio
async def test_disabled_model_control_accounts_for_summary_and_instruction(tmp_path, monkeypatch):
    settings = setup_chat(tmp_path, llm_context_tokens=1200, llm_context_safety_tokens=100, roleplay_summary_max_tokens=100, summary_job_max_attempts=1)
    with connect(settings.database_path) as db:
        db.execute("UPDATE conversations SET summary=?", ('old ' * 1000,))
    calls = mock_gate(monkeypatch, settings)
    await main.run_summary_job(settings, main.queue_roleplay_summary(settings, 'c', force=True))
    assert not calls
    assert state(settings)['summary_up_to_message_id'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'running'])
async def test_startup_recovers_interrupted_jobs(tmp_path, monkeypatch, status):
    settings = setup_chat(tmp_path)
    mock_gate(monkeypatch, settings)
    job_id = main.queue_roleplay_summary(settings, 'c', force=True)
    with connect(settings.database_path) as db:
        db.execute('UPDATE summary_jobs SET status=?', (status,))
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    async with main.lifespan(main.app):
        await asyncio.gather(*list(main.app.state.summary_tasks))
    assert state(settings)['summary_up_to_message_id'] == 'm009'


@pytest.mark.asyncio
async def test_worker_does_not_reclaim_live_owner_and_crash_releases_lock(tmp_path, monkeypatch):
    import hashlib
    import subprocess
    import sys
    settings = setup_chat(tmp_path)
    calls = mock_gate(monkeypatch, settings)
    job_id = main.queue_roleplay_summary(settings, 'c', force=True)
    lock_dir = tmp_path / 'db.summary-locks'; lock_dir.mkdir()
    lock_path = lock_dir / hashlib.sha256(job_id.encode()).hexdigest()
    script = "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, '-c', script, str(lock_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        await main.run_summary_job(settings, job_id)
        assert calls == []
    finally:
        process.kill(); process.wait()
    await main.run_summary_job(settings, job_id)
    assert state(settings)['summary_up_to_message_id'] == 'm009'


def test_concurrent_generation_and_mixed_history(tmp_path):
    settings = setup_chat(tmp_path)
    with connect(settings.database_path) as db:
        for index, status in enumerate(['completed', 'failed', 'cancelled']):
            db.execute("INSERT INTO generation_requests(id,conversation_id,user_message_id,assistant_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,'t','t','t')", (str(index), 'c', f'm{index*2:03}', f'm{index*2+1:03}', status))
    _, _, history, _ = main.begin_generation('c', main.MessageCreate(content='one'), settings)
    assert [m['content'] for m in history[:2]] == ['fact 0', 'fact 1']
    assert not any(m['content'] in {'fact 2', 'fact 3', 'fact 4', 'fact 5'} for m in history)
    with pytest.raises(main.HTTPException) as error:
        main.begin_generation('c', main.MessageCreate(content='two'), settings)
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_http_strict_failure_never_calls_chat_inference(tmp_path, monkeypatch, stream):
    settings = setup_chat(tmp_path, model_control_enabled=True, llm_context_tokens=2000, summary_job_max_attempts=1)
    real_client = httpx.AsyncClient
    calls = mock_gate(monkeypatch, settings, count=lambda _: 2000)
    main.app.state.cancelled_generations = set()
    dependency = main.get_settings
    main.app.dependency_overrides[dependency] = lambda: settings
    try:
        async with real_client(transport=httpx.ASGITransport(app=main.app), base_url='http://client') as client:
            response = await client.post('/api/v1/conversations/c/messages', headers={'Authorization': 'Bearer client-key'}, json={'content': 'next', 'stream': stream})
        assert response.status_code == 422
        assert response.json()['error']['code'] == 'context_preservation_failed'
        assert all(path.endswith('/input_tokens') for path, _ in calls)
        with connect(settings.database_path) as db:
            assert db.execute('SELECT status FROM generation_requests').fetchone()[0] == 'failed'
            assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 23
    finally:
        main.app.dependency_overrides.pop(dependency, None)


@pytest.mark.asyncio
async def test_preflight_deadline_preserves_state(tmp_path, monkeypatch):
    import time
    settings = setup_chat(tmp_path, model_control_enabled=True, llm_context_tokens=2000, context_compaction_timeout_seconds=.2)
    mock_gate(monkeypatch, settings, count=lambda _: 2000)
    async def stalled_resolve(_):
        await asyncio.sleep(10)
    monkeypatch.setattr(main, 'resolve_gate_model', stalled_resolve)
    start = time.monotonic()
    with pytest.raises(main.HTTPException) as error:
        await main.prepare_chat_context(settings, 'c', 'next', main.GateModel('m', 'a'*32, 2000), .7, False)
    assert time.monotonic() - start < 1
    assert error.value.detail['code'] == 'context_preservation_failed'
    assert state(settings)['summary_up_to_message_id'] is None
    with connect(settings.database_path) as db:
        assert db.execute('SELECT status FROM summary_jobs').fetchone()[0] == 'failed'


@pytest.mark.asyncio
async def test_recovery_does_not_reset_persisted_retry_limit(tmp_path, monkeypatch):
    settings = setup_chat(tmp_path, summary_job_max_attempts=2)
    calls = mock_gate(monkeypatch, settings)
    job_id = main.queue_roleplay_summary(settings, 'c', force=True)
    with connect(settings.database_path) as db:
        db.execute("UPDATE summary_jobs SET status='running'")
        db.execute('UPDATE summary_job_state SET attempt_count=2')
    await main.run_summary_job(settings, job_id)
    assert calls == []
    assert state(settings)['summary_up_to_message_id'] is None
    assert state(settings)['summary_status'] == 'failed'


def test_legacy_none_policy_does_not_discard_chat_history(tmp_path):
    from backend.app.prompting import build_prompt
    settings = setup_chat(tmp_path)
    conversation = main.conversation_dict(state(settings))
    conversation['context_policy'] = 'none'
    plan = build_prompt(conversation, [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'fact'}], 'next', settings)
    assert [m['content'] for m in plan.messages][-3:] == ['old', 'fact', 'next']
