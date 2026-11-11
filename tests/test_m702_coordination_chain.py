"""Exercise rollout checks and startup release against the actual gate app."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from gate_candidate.gate import create_app, provision
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import coordination_status
import release_startup_hold
import rollback_readiness


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_response', [False, True])
async def test_coordination_release_and_rollback_keep_legacy_hold(tmp_path, monkeypatch, lost_response):
    # The gate implementation is real. Process identities, Studio status and
    # the worker's response are explicit doubles; no operating service is used.
    state = tmp_path / 'gate.json'
    provision(state, legacy_paused=True)
    names = {'gate-admin-key': 'admin', 'gate-client-key': 'client',
             'gate-studio-key': 'studio', 'switch-gate-key': 'switch',
             'studio-coordination-key': 'coordination'}
    for name, value in names.items():
        (tmp_path / name).write_text(value)
    monkeypatch.setattr(coordination_status, 'KEYS', tmp_path)
    monkeypatch.setattr(release_startup_hold, 'KEYS', tmp_path)
    monkeypatch.setattr(rollback_readiness, 'KEYS', tmp_path)
    identity = lambda: (999999, 'start')
    monkeypatch.setattr(coordination_status, 'gate_identity', identity)
    monkeypatch.setattr(coordination_status, 'studio_identity', identity)
    monkeypatch.setattr(coordination_status, 'client_identity', identity)
    monkeypatch.setattr(coordination_status, 'studio_status', lambda: {'quiescent': True})
    read_bytes = Path.read_bytes
    def client_environment(path):
        if str(path) == '/proc/999999/environ':
            return (b'LLM_GATE_API_KEY=client\0LLM_GATE_HOLD_ID=client-' + b'a' * 32
                    + b'\0OPERATOR_KEY=test-operator\0MODEL_CONTROL_ENABLED=false\0')
        return read_bytes(path)
    monkeypatch.setattr(Path, 'read_bytes', client_environment)
    app = create_app(state, 'http://upstream', admin_key='admin', client_key='client',
                     studio_key='studio', switch_key='switch')
    with TestClient(app) as gate:
        def get(url, key):
            parsed = urlsplit(url)
            if parsed.port == 8090:
                assert key == 'coordination'
                return {'protocol': 1, 'enabled': True, 'transition_clear': True}
            response = gate.get(parsed.path, headers={'Authorization': 'Bearer ' + key})
            response.raise_for_status()
            return response.json()
        monkeypatch.setattr(coordination_status, 'get', get)
        monkeypatch.setattr(release_startup_hold, 'get', get)
        monkeypatch.setattr(rollback_readiness, 'get', get)
        monkeypatch.setattr(release_startup_hold, 'read_status', coordination_status.read_status)
        monkeypatch.setattr(rollback_readiness, 'read_status', coordination_status.read_status)
        before = coordination_status.read_status()
        assert before['holds'] == ['legacy', 'startup']

        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def read(self, *_args): return json.dumps(self.value).encode()
        calls = []
        def delete(request, **_kwargs):
            calls.append(request.full_url)
            response = gate.delete(urlsplit(request.full_url).path,
                                   headers={'Authorization': request.get_header('Authorization')})
            response.raise_for_status()
            if lost_response:
                raise OSError('simulated lost release response')
            return Response(response.json())
        monkeypatch.setattr(release_startup_hold.urllib.request, 'urlopen', delete)
        if lost_response:
            with pytest.raises(OSError): release_startup_hold.release()
        else:
            assert release_startup_hold.release() == ['legacy']
        assert len(calls) == 1
        assert coordination_status.read_status()['holds'] == ['legacy']

        class Worker:
            def __init__(self, _path): pass
            async def request(self, operation):
                assert operation == 'inspect_recovery'
                return {'operation': None, 'hold_id': None, 'journal': {}, 'journal_error': None,
                        'reconciliation_error': None}
        monkeypatch.setattr(rollback_readiness, 'ModelControlClient', Worker)
        monkeypatch.setattr(rollback_readiness, 'require_settled_llama', lambda: '123')
        assert (await rollback_readiness.assess())['legacy_pause_to_restore'] is True
        assert coordination_status.read_status()['holds'] == ['legacy']
