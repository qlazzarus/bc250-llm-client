import os
from types import SimpleNamespace

import httpx
import pytest

from backend.app.model_control.preflight import CoordinationPreflight


def test_preflight_checks_identities_and_authenticated_protocol(tmp_path, monkeypatch):
    lock, journal = tmp_path / 'lock', tmp_path / 'journal'
    lock.write_text(''); journal.write_text('{}')
    handle = lock.open('r')
    requests = []
    def response(request):
        requests.append(request)
        if request.url.path.endswith('/identity'):
            return httpx.Response(200, json={'protocol': 2, 'role': 'switch'})
        return httpx.Response(200, json={'protocol': 1, 'enabled': True, 'transition_clear': True})
    pre = CoordinationPreflight('studio-key', 'gate-key', lock, journal,
                                held_lock=SimpleNamespace(handle=handle), transport=httpx.MockTransport(response))
    monkeypatch.setattr(pre, 'service_identity', lambda unit, account: (os.getpid(), 'start'))
    # /proc is Linux-only; compare against this process's paths through a test proxy.
    original_stat = os.stat
    def stat(path, **kwargs):
        prefix = f'/proc/{os.getpid()}/root'
        if str(path).startswith(prefix):
            path = str(path)[len(prefix):]
        return original_stat(path, **kwargs)
    monkeypatch.setattr('backend.app.model_control.preflight.os.stat', stat)
    try:
        pre()
        assert [r.headers['authorization'] for r in requests] == ['Bearer gate-key', 'Bearer studio-key']
        lock.unlink(); lock.write_text('replaced')
        with pytest.raises(RuntimeError, match='inode changed'):
            pre()
    finally:
        handle.close()


@pytest.mark.parametrize('bad', ['gate', 'studio', 'pid'])
def test_preflight_rejects_untrusted_contract(tmp_path, monkeypatch, bad):
    lock, journal = tmp_path / 'lock', tmp_path / 'journal'
    lock.write_text(''); journal.write_text('{}')
    def response(request):
        if request.url.path.endswith('/identity'):
            return httpx.Response(200, json={'protocol': 2, 'role': 'admin' if bad == 'gate' else 'switch'})
        return httpx.Response(200, json={'protocol': 1, 'enabled': bad != 'studio', 'transition_clear': True})
    with lock.open() as handle:
        pre = CoordinationPreflight('studio', 'gate', lock, journal, held_lock=SimpleNamespace(handle=handle), transport=httpx.MockTransport(response))
        calls = []
        def identity(unit, account):
            calls.append(unit)
            return (1, str(len(calls)) if bad == 'pid' else 'start')
        monkeypatch.setattr(pre, 'service_identity', identity)
        monkeypatch.setattr(pre, 'same_studio_files', lambda _pid: None)
        with pytest.raises(RuntimeError): pre()


def test_preflight_requires_both_credentials(tmp_path):
    with pytest.raises(ValueError):
        CoordinationPreflight('', 'key', tmp_path / 'lock', tmp_path / 'journal', held_lock=None)
