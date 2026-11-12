import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from backend.app.db import connect, initialize
from backend.app.wiki_memory import MEMORY_POLICY_VERSION, MemoryDecision, automatic_memory_skip_reason, candidate_text_is_safe, parse_decision


def test_decision_requires_exact_user_source_and_searched_document():
    source = '나는 녹차를 좋아해.'
    decision = {'action': 'append', 'kind': 'preference', 'reason': 'durable', 'source_text': '녹차를 좋아해',
                'source_message_id': 'u', 'title': '', 'document_id': 42, 'old_text': None}
    assert parse_decision(json.dumps(decision), source, [{'id': 42}], user_message_id='u').document_id == 42
    with pytest.raises(ValueError):
        parse_decision(json.dumps({**decision, 'source_text': '커피를 좋아해'}), source, [{'id': 42}], user_message_id='u')
    with pytest.raises(ValueError):
        parse_decision(json.dumps(decision), source, [{'id': 43}], user_message_id='u')
    with pytest.raises(ValueError):
        parse_decision(json.dumps({**decision, 'source_message_id': 'assistant'}), source, [{'id': 42}], user_message_id='u')


def test_decision_requires_a_durable_kind_for_writes_and_no_source_for_none():
    base = {'action': 'create', 'kind': 'fact', 'reason': 'durable', 'source_text': '나는 녹차를 좋아해',
            'source_message_id': 'u', 'title': '차 선호', 'document_id': None, 'old_text': None}
    assert MemoryDecision.model_validate(base).kind == 'fact'
    with pytest.raises(ValueError):
        MemoryDecision.model_validate({**base, 'kind': 'question'})
    with pytest.raises(ValueError):
        MemoryDecision.model_validate({**base, 'reason': 'ambiguous'})
    assert MemoryDecision.model_validate({**base, 'action': 'none', 'kind': 'uncertain',
                                          'reason': 'insufficient_context', 'source_text': '',
                                          'source_message_id': None}).action == 'none'
    with pytest.raises(ValueError):
        MemoryDecision.model_validate({**base, 'action': 'none', 'kind': 'uncertain',
                                       'reason': 'insufficient_context'})


@pytest.mark.parametrize(('kind', 'source_text', 'safe'), [
    ('fact', '나는 그 기기를 안 써', True),
    ('plan', '11월 20일에 교체할 계획이야', True),
    ('plan', '언젠가 교체할 계획이야', False),
    ('fact', '지금 쓰는 기기에서 galaxy를 추천해줘', False),
])
def test_candidate_text_safety_preserves_negation_and_requires_timed_plans(kind, source_text, safe):
    decision = MemoryDecision.model_validate({'action': 'create', 'kind': kind, 'reason': 'durable',
                                              'source_text': source_text, 'source_message_id': 'u',
                                              'title': '기억', 'document_id': None, 'old_text': None})
    assert candidate_text_is_safe(decision) is safe


def test_memory_metadata_migration_preserves_legacy_job_without_backfill(tmp_path):
    path = str(tmp_path / 'legacy-memory.db')
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE generation_requests (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, user_message_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE wiki_memory_jobs (
            generation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, status TEXT NOT NULL, action TEXT, document_id INTEGER, title TEXT,
            source_text TEXT, previous_content TEXT, proposed_content TEXT, error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            explicit INTEGER NOT NULL DEFAULT 0, source_message_id TEXT
        );
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    """)
    connection.execute("INSERT INTO conversations VALUES ('c','legacy','t','t')")
    connection.execute("INSERT INTO messages VALUES ('u','c','user','기존 내용','t')")
    connection.execute("INSERT INTO generation_requests VALUES ('g','c','u','completed','t','t')")
    connection.execute("INSERT INTO wiki_memory_jobs VALUES ('g','c','skipped',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'t','t',0,NULL)")
    connection.commit()
    connection.close()

    initialize(path)
    initialize(path)

    with connect(path) as db:
        row = db.execute('SELECT status,memory_kind,reason_code,needs_attention,policy_version FROM wiki_memory_jobs WHERE generation_id=?', ('g',)).fetchone()
        versions = [item['version'] for item in db.execute('SELECT version FROM schema_migrations ORDER BY version')]
    assert dict(row) == {'status': 'skipped', 'memory_kind': None, 'reason_code': None, 'needs_attention': None, 'policy_version': None}
    assert versions.count(15) == 1


def test_memory_history_returns_optional_metadata_without_changing_legacy_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app import main

    path = str(tmp_path / 'history.db')
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('c','title','t','t')")
        db.execute("INSERT INTO messages VALUES ('u','c','user','내용','t')")
        db.execute("INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES ('g','c','u','completed','t','t','t')")
        db.execute("""INSERT INTO wiki_memory_jobs
            (generation_id,conversation_id,status,explicit,memory_kind,reason_code,needs_attention,policy_version,created_at,updated_at)
            VALUES ('g','c','skipped',0,'question','question',0,?, 't','t')""", (MEMORY_POLICY_VERSION,))

    result = asyncio.run(main.wiki_memory_history('c', SimpleNamespace(database_path=path)))
    row = result['data'][0]
    assert row['status'] == 'skipped'
    assert row['memory_kind'] == 'question'
    assert row['reason_code'] == 'question'
    assert row['needs_attention'] == 0
    assert row['policy_version'] == MEMORY_POLICY_VERSION
    assert 'source_text' in row and 'error_message' in row


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
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'message': {'content': json.dumps({'action': 'create', 'kind': 'preference', 'reason': 'durable', 'source_text': '녹차를 좋아해', 'source_message_id': 'u', 'title': '차 선호', 'document_id': None, 'old_text': None})}}]}
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
    settings = SimpleNamespace(model_control_enabled=False, database_path=path, llm_context_tokens=16384, llm_context_safety_tokens=512, max_completion_tokens=1024, llm_gate_url='http://localhost', llm_gate_api_key='x', llm_timeout_seconds=1)
    asyncio.run(main.run_wiki_memory_job(settings, 'g'))
    asyncio.run(main.run_wiki_memory_job(settings, 'g'))
    with connect(path) as db:
        row = db.execute('SELECT status,source_text,memory_kind,reason_code,needs_attention,policy_version FROM wiki_memory_jobs').fetchone()
    assert row['status'] == 'unknown'
    assert row['source_text'] == '녹차를 좋아해'
    assert (row['memory_kind'], row['reason_code'], row['needs_attention'], row['policy_version']) == ('preference', 'write_unknown', 1, MEMORY_POLICY_VERSION)
    assert calls == ['GET', 'POST']


@pytest.mark.parametrize(('source', 'expected_reason'), [
    ('iphone 12 pro max 가 어떤 기종이야?', 'obvious_question'),
    ('iPhone 12 Pro Max 어떤 기종이야', 'obvious_question'),
    ('What is an iPhone 12 Pro Max?', 'obvious_question'),
    ('추천해줘', 'obvious_question'),
    ('안녕하세요', 'greeting_or_simple_request'),
    ('기억하지 마. 비밀번호는 저장하지 마.', 'memory_opt_out'),
    ('기억해 줘라는 말이 뭐야?', 'obvious_question'),
    ('나는 iPhone 12 Pro Max를 쓰는데 Galaxy를 추천해 줘', None),
    ('나는 녹차를 좋아해. 기억해 줘.', None),
])
def test_automatic_memory_skip_reason_is_narrow(source, expected_reason):
    assert automatic_memory_skip_reason(source) == expected_reason


@pytest.mark.parametrize('source', [
    'iphone 12 pro max 가 어떤 기종이야?',
    '기억하지 마. 비밀번호는 저장하지 마.',
    '기억해 줘라는 말이 뭐야?',
])
def test_non_memory_candidate_does_not_queue_a_memory_job(tmp_path, monkeypatch, source):
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app.main import complete_generation

    path = str(tmp_path / 'test.db')
    initialize(path)
    timestamp = '2026-01-01T00:00:00+00:00'
    with connect(path) as db:
        db.execute('INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES (?,?,?,?,?)',
                   ('c', 'c', timestamp, timestamp, 'quick'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', ('u', 'c', 'user', source, timestamp))
        db.execute('INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                   ('g', 'c', 'u', 'pending', timestamp, timestamp, timestamp))

    complete_generation(SimpleNamespace(database_path=path), 'g', '답변', 'stop', None, True)

    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM wiki_memory_jobs').fetchone()[0] == 0


@pytest.mark.parametrize('source', [
    'iphone 12 pro max 가 어떤 기종이야?',
    '기억하지 마. 비밀번호는 저장하지 마.',
])
def test_non_memory_candidate_skips_legacy_pending_job_without_wiki_or_llm(tmp_path, monkeypatch, source):
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app import main

    path = str(tmp_path / 'test.db')
    initialize(path)
    timestamp = '2026-01-01T00:00:00+00:00'
    with connect(path) as db:
        db.execute('INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES (?,?,?,?,?)',
                   ('c', 'c', timestamp, timestamp, 'quick'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', ('u', 'c', 'user', source, timestamp))
        db.execute('INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                   ('g', 'c', 'u', 'completed', timestamp, timestamp, timestamp))
        db.execute('INSERT INTO wiki_memory_jobs (generation_id,conversation_id,status,created_at,updated_at) VALUES (?,?,?,?,?)',
                   ('g', 'c', 'pending', timestamp, timestamp))

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError('non-memory candidate must not call Wiki or LLM')

    monkeypatch.setattr(main, 'wiki_request', unexpected_call)
    monkeypatch.setattr(main, 'resolve_gate_model', unexpected_call)
    main.app.state.wiki_memory_lock = asyncio.Lock()
    main.app.state.generation_limiter = asyncio.Semaphore(1)

    asyncio.run(main.run_wiki_memory_job(SimpleNamespace(database_path=path), 'g'))

    with connect(path) as db:
        assert db.execute('SELECT status FROM wiki_memory_jobs WHERE generation_id=?', ('g',)).fetchone()['status'] == 'skipped'


def test_mixed_fact_and_recommendation_request_stays_a_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app.main import complete_generation

    path = str(tmp_path / 'test.db')
    initialize(path)
    timestamp = '2026-01-01T00:00:00+00:00'
    source = '나는 iPhone 12 Pro Max를 쓰는데 Galaxy를 추천해 줘'
    with connect(path) as db:
        db.execute('INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES (?,?,?,?,?)',
                   ('c', 'c', timestamp, timestamp, 'quick'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', ('u', 'c', 'user', source, timestamp))
        db.execute('INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                   ('g', 'c', 'u', 'pending', timestamp, timestamp, timestamp))

    complete_generation(SimpleNamespace(database_path=path), 'g', '답변', 'stop', None, True)

    with connect(path) as db:
        row = db.execute('SELECT status FROM wiki_memory_jobs WHERE generation_id=?', ('g',)).fetchone()
    assert row['status'] == 'pending'


@pytest.mark.parametrize(('selected_text', 'expected_status', 'expected_writes'), [
    ('지금 쓰는 iphone 12 pro max 에서 galaxy 로 바꾸려고 하는데 추천해줘', 'skipped', []),
    ('지금 쓰는 iphone 12 pro max', 'completed', [{'title': '기기 정보', 'category': 'general', 'content': '지금 쓰는 iphone 12 pro max'}]),
])
def test_mixed_request_rejects_whole_request_but_accepts_minimal_fact(tmp_path, monkeypatch, selected_text, expected_status, expected_writes):
    """W03 keeps the durable quoted fact but rejects a request-shaped quote."""
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app import main

    source = '지금 쓰는 iphone 12 pro max 에서 galaxy 로 바꾸려고 하는데 추천해줘'
    path = str(tmp_path / 'test.db')
    initialize(path)
    timestamp = '2026-01-01T00:00:00+00:00'
    with connect(path) as db:
        db.execute('INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES (?,?,?,?,?)',
                   ('c', 'c', timestamp, timestamp, 'quick'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', ('u', 'c', 'user', source, timestamp))
        db.execute('INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                   ('g', 'c', 'u', 'completed', timestamp, timestamp, timestamp))
        db.execute('INSERT INTO wiki_memory_jobs (generation_id,conversation_id,status,created_at,updated_at) VALUES (?,?,?,?,?)',
                   ('g', 'c', 'pending', timestamp, timestamp))

    writes = []

    async def wiki_request(method, _path, **kwargs):
        if method == 'GET':
            return []
        writes.append(kwargs['json'])
        return {'id': 2}

    async def model(_settings):
        return 'model'

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            decision = {'action': 'create', 'kind': 'fact', 'reason': 'durable', 'source_text': selected_text,
                        'source_message_id': 'u', 'title': '기기 정보', 'document_id': None, 'old_text': None}
            return {'choices': [{'message': {'content': json.dumps(decision)}}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(main, 'wiki_request', wiki_request)
    monkeypatch.setattr(main, 'resolve_gate_model', model)
    monkeypatch.setattr(main.httpx, 'AsyncClient', Client)
    main.app.state.wiki_memory_lock = asyncio.Lock()
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    settings = SimpleNamespace(model_control_enabled=False, database_path=path, llm_context_tokens=16384,
                               llm_context_safety_tokens=512, max_completion_tokens=1024,
                               llm_gate_url='http://localhost', llm_gate_api_key='x', llm_timeout_seconds=1)

    asyncio.run(main.run_wiki_memory_job(settings, 'g'))

    with connect(path) as db:
        row = db.execute('SELECT status,source_text FROM wiki_memory_jobs WHERE generation_id=?', ('g',)).fetchone()
    assert (row['status'], row['source_text']) == (expected_status, selected_text if expected_status == 'completed' else None)
    assert writes == expected_writes


def test_explicit_memory_requests_and_safe_replacement():
    from backend.app.wiki_memory import explicit_memory_request, replace_once
    assert explicit_memory_request('홍차를 좋아한다고 기억해 줘')
    assert explicit_memory_request('위키에 저장해 줘')
    assert not explicit_memory_request('기억력이 좋아지는 방법은?')
    assert replace_once('차: 녹차\n색: 녹색', '차: 녹차', '차: 홍차') == '차: 홍차\n색: 녹색'
    with pytest.raises(ValueError):
        replace_once('녹차, 녹차', '녹차', '홍차')
    with pytest.raises(ValueError):
        replace_once('차: 녹차', '커피', '홍차')


def test_explicit_request_can_quote_previous_user_message_only():
    decision = {'action': 'create', 'kind': 'preference', 'reason': 'durable', 'source_text': '홍차를 좋아해',
                'source_message_id': 'previous', 'title': '차 선호', 'document_id': None, 'old_text': None}
    assert parse_decision(json.dumps(decision), '이걸 기억해 줘', [], user_message_id='current',
                          previous_user_messages=[{'id': 'previous', 'content': '홍차를 좋아해'}]).source_text == '홍차를 좋아해'
    with pytest.raises(ValueError):
        parse_decision(json.dumps(decision), '이걸 기억해 줘', [], user_message_id='current')


def test_short_follow_up_search_includes_previous_user_question():
    from backend.app.wiki_memory import search_query
    history = [{'role': 'user', 'content': '서버 백업은 어떻게 해?'}, {'role': 'assistant', 'content': '백업 안내'}]
    assert search_query('그건 어디에 있어?', history) == '그건 어디에 있어? 서버 백업은 어떻게 해?'
    assert search_query('긴 질문' * 30, history) == '긴 질문' * 30


def test_explicit_request_queues_even_when_automatic_memory_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_API_KEY', 'test')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'test')
    from backend.app.main import complete_generation
    path = str(tmp_path / 'test.db')
    initialize(path)
    timestamp = '2026-01-01T00:00:00+00:00'
    with connect(path) as db:
        db.execute('INSERT INTO conversations (id,title,created_at,updated_at,mode) VALUES (?,?,?,?,?)', ('c', 'c', timestamp, timestamp, 'quick'))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', ('u', 'c', 'user', '홍차를 좋아한다고 기억해 줘', timestamp))
        db.execute('INSERT INTO generation_requests (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)', ('g', 'c', 'u', 'pending', timestamp, timestamp, timestamp))
    complete_generation(SimpleNamespace(database_path=path), 'g', '알겠어요', 'stop', None, False)
    with connect(path) as db:
        row = db.execute('SELECT status,explicit FROM wiki_memory_jobs WHERE generation_id=?', ('g',)).fetchone()
    assert (row['status'], row['explicit']) == ('pending', 1)
