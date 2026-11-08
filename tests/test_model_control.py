import os
os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')

from fastapi.testclient import TestClient
from backend.app import main


class FakeWorker:
    def __init__(self):
        self.operation = None
        self.key = None

    async def request(self, action, **kwargs):
        if action == 'models':
            return {'models': [{'name': 'a', 'current': True}, {'name': 'b', 'current': False}],
                    'configured_model': 'a', 'loaded_model': 'a', 'ready': True, 'switch_enabled': True, 'active_operation_id': None}
        if action == 'current':
            return {'operation': self.operation}
        if action == 'get':
            return self.operation if self.operation and kwargs['id'] == self.operation['id'] else {'error': 'not_found'}
        if action == 'submit':
            if self.operation and (self.key != kwargs['idempotency_key'] or self.operation['model'] != kwargs['model']):
                return {'error': 'switch_busy_or_recovery_required'}
            self.key = kwargs['idempotency_key']
            self.operation = {'id': 'a3414f5329e246c590731273dba97380', 'model': kwargs['model'], 'state': 'queued'}
            return self.operation
        raise AssertionError(action)


def test_operator_and_disabled(monkeypatch):
    monkeypatch.setattr(main.get_settings(), 'operator_key', 'operator-secret')
    monkeypatch.setattr(main.get_settings(), 'model_control_enabled', False)
    with TestClient(main.app) as client:
        auth = {'Authorization': 'Bearer client-key'}
        assert client.get('/api/v1/llm/models', headers=auth).status_code == 403
        assert client.put('/api/v1/admin/gate-control', headers=auth, json={'paused': True}).status_code == 403
        assert client.get('/api/v1/llm/models', headers={**auth, 'X-BC250-Operator-Key': 'operator-secret'}).status_code == 503


def test_fake_worker_flow(monkeypatch):
    monkeypatch.setattr(main.get_settings(), 'operator_key', 'operator-secret')
    fake = FakeWorker()
    main.app.dependency_overrides[main.get_model_control] = lambda: fake
    headers = {'Authorization': 'Bearer client-key', 'X-BC250-Operator-Key': 'operator-secret'}
    try:
        with TestClient(main.app) as client:
            assert client.get('/api/v1/llm/models', headers=headers).json()['loaded_model'] == 'a'
            assert client.post('/api/v1/llm/switches', headers=headers, json={'model': 'b'}).status_code == 422
            h = {**headers, 'Idempotency-Key': 'request1'}
            first = client.post('/api/v1/llm/switches', headers=h, json={'model': 'b'})
            assert first.status_code == 202
            assert client.post('/api/v1/llm/switches', headers=h, json={'model': 'b'}).json() == first.json()
            assert client.post('/api/v1/llm/switches', headers={**headers, 'Idempotency-Key': 'other'}, json={'model': 'a'}).status_code == 409
            assert client.post('/api/v1/llm/switches', headers=h, json={'model': 'a'}).status_code == 409
            assert client.get('/api/v1/llm/switches/current', headers=headers).json()['operation'] == first.json()
            assert client.get('/api/v1/llm/switches/' + first.json()['id'], headers=headers).json() == first.json()
    finally:
        main.app.dependency_overrides.clear()


def test_local_mode_is_not_operator_and_missing_socket(monkeypatch, tmp_path):
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'app_auth_mode', 'local')
    monkeypatch.setattr(settings, 'app_allowed_hosts', 'testserver')
    monkeypatch.setattr(settings, 'operator_key', 'operator-secret')
    monkeypatch.setattr(settings, 'model_control_enabled', True)
    monkeypatch.setattr(settings, 'model_control_socket', str(tmp_path / 'missing.sock'))
    with TestClient(main.app) as client:
        local = {'X-BC250-Client': '1'}
        assert client.put('/api/v1/admin/gate-control', headers=local, json={'paused': True}).status_code == 403
        assert client.get('/api/v1/llm/models', headers={**local, 'X-BC250-Operator-Key': 'operator-secret'}).status_code == 503


def test_operator_key_never_bypasses_local_request_checks(monkeypatch):
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'app_auth_mode', 'local')
    monkeypatch.setattr(settings, 'app_allowed_hosts', 'testserver')
    monkeypatch.setattr(settings, 'operator_key', 'operator-secret')
    main.app.dependency_overrides[main.get_model_control] = lambda: FakeWorker()
    try:
        with TestClient(main.app) as client:
            operator = {'X-BC250-Operator-Key': 'operator-secret'}
            assert client.get('/api/v1/llm/models', headers=operator).status_code == 403
            assert client.get('/api/v1/llm/models', headers={**operator, 'X-BC250-Client': '1', 'Origin': 'https://untrusted.example'}).status_code == 403
            assert client.get('/api/v1/llm/models', headers={**operator, 'X-BC250-Client': '1'}).status_code == 200
            monkeypatch.setattr(settings, 'operator_key', '')
            assert client.get('/api/v1/llm/models', headers={'X-BC250-Client': '1'}).status_code == 403
    finally:
        main.app.dependency_overrides.clear()


def test_invalid_worker_response_is_not_disclosed(monkeypatch):
    class InvalidWorker:
        async def request(self, action, **kwargs):
            return {'id': 'a' * 32, 'model': 'a', 'state': 'failed', 'error_code': 'internal-server-details'}

    monkeypatch.setattr(main.get_settings(), 'operator_key', 'operator-secret')
    main.app.dependency_overrides[main.get_model_control] = lambda: InvalidWorker()
    headers = {'Authorization': 'Bearer client-key', 'X-BC250-Operator-Key': 'operator-secret'}
    try:
        with TestClient(main.app) as client:
            response = client.get('/api/v1/llm/switches/' + 'a' * 32, headers=headers)
            assert response.status_code == 503
            assert 'internal-server-details' not in response.text
    finally:
        main.app.dependency_overrides.clear()
