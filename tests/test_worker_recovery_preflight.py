import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app.model_control.preflight import RecoveryPreflight


@pytest.mark.parametrize('state', [{}, {'hold_id': 'switch-' + 'a' * 32, 'stage': 'switching'}])
def test_recovery_contract_binds_authenticated_studio_to_journal(tmp_path, monkeypatch, state):
    digest = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    calls = []
    def respond(request):
        calls.append((request.url.path, request.headers['Authorization']))
        if request.url.path == '/control/identity':
            return httpx.Response(200, json={'protocol': 3, 'role': 'switch'})
        return httpx.Response(200, json={'protocol': 1, 'enabled': True, 'gpu_blocked': True, 'studio_recovery_clear': True,
                                         'gate_verified': True, 'journal_sha256': digest})
    pre = RecoveryPreflight('studio', 'gate', tmp_path / 'lock', tmp_path / 'journal',
                            held_lock=None, journal=SimpleNamespace(read=lambda: state),
                            transport=httpx.MockTransport(respond))
    monkeypatch.setattr(pre, 'service_identity', lambda unit, account: (1 if account == 'ai-studio' else 2, 'start'))
    monkeypatch.setattr(pre, 'same_studio_files', lambda pid: calls.append(('files', pid)))
    pre()
    assert calls == [('files', 1), ('/control/identity', 'Bearer gate'),
                     ('/internal/model-switch/recovery', 'Bearer studio'), ('files', 1)]


@pytest.mark.parametrize('failure', ['unsupported', 'bad_digest', 'unblocked', 'studio_recovery', 'restart', 'changed_journal', 'gate_auth'])
def test_recovery_contract_fails_closed(tmp_path, monkeypatch, failure):
    state = {}
    digest = hashlib.sha256(b'{}').hexdigest()
    def respond(request):
        if request.url.path == '/control/identity':
            return httpx.Response(401 if failure == 'gate_auth' else 200, json={'protocol': 3, 'role': 'switch'})
        if failure == 'unsupported':
            return httpx.Response(404)
        return httpx.Response(200, json={'protocol': 1, 'enabled': True, 'gpu_blocked': failure != 'unblocked',
                                         'studio_recovery_clear': failure != 'studio_recovery',
                                         'gate_verified': True, 'journal_sha256': 'bad' if failure == 'bad_digest' else digest})
    reads = iter([state, {'changed': True}] if failure == 'changed_journal' else [state, state])
    pre = RecoveryPreflight('studio', 'gate', tmp_path / 'lock', tmp_path / 'journal',
                            held_lock=None, journal=SimpleNamespace(read=lambda: next(reads)),
                            transport=httpx.MockTransport(respond))
    counts = {'ai-studio': 0, 'ai-gate': 0}
    def identity(unit, account):
        counts[account] += 1
        return (1, str(counts[account]) if failure == 'restart' else 'start')
    monkeypatch.setattr(pre, 'service_identity', identity)
    monkeypatch.setattr(pre, 'same_studio_files', lambda pid: None)
    with pytest.raises((RuntimeError, httpx.HTTPError)):
        pre()


@pytest.mark.parametrize('field', ['protocol', 'enabled', 'gpu_blocked', 'studio_recovery_clear', 'gate_verified'])
def test_recovery_rejects_boolean_integer_type_confusion(tmp_path, monkeypatch, field):
    response = {'protocol': 1, 'enabled': True, 'gpu_blocked': True, 'studio_recovery_clear': True,
                'gate_verified': True, 'journal_sha256': hashlib.sha256(b'{}').hexdigest()}
    response[field] = True if field == 'protocol' else 1
    def reply(request):
        if request.url.path == '/control/identity':
            return httpx.Response(200, json={'protocol': 3, 'role': 'switch'})
        return httpx.Response(200, json=response)
    pre = RecoveryPreflight('studio', 'gate', tmp_path / 'lock', tmp_path / 'journal', held_lock=None,
                            journal=SimpleNamespace(read=lambda: {}), transport=httpx.MockTransport(reply))
    monkeypatch.setattr(pre, 'service_identity', lambda *args: (1, 'start'))
    monkeypatch.setattr(pre, 'same_studio_files', lambda pid: None)
    with pytest.raises(RuntimeError):
        pre()
