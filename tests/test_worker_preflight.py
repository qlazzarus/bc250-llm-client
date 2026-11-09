import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.app.model_control.preflight import CoordinationPreflight
from backend.app.model_control.files import FileLock, LockBusyError


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux procfs contract')
def test_shared_files_match_live_process_and_locked_inode(tmp_path):
    lock_path, journal = tmp_path / 'gpu.lock', tmp_path / 'switch.json'
    lock_path.touch()
    journal.write_text('{}')
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    lock = FileLock(lock_path)
    try:
        lock.acquire()
        pre = CoordinationPreflight('studio', 'gate', lock_path, journal, held_lock=lock)
        pre.same_studio_files(child.pid)
        with pytest.raises(LockBusyError):
            FileLock(lock_path).acquire()
        lock_path.unlink()
        lock_path.touch()
        with pytest.raises(RuntimeError, match='inode changed'):
            pre.same_studio_files(child.pid)
        lock.release()
        lock.acquire()
        journal.unlink()
        with pytest.raises(OSError):
            pre.same_studio_files(child.pid)
    finally:
        lock.release()
        child.stdin.close()
        child.wait(timeout=5)


def test_preflight_checks_identities_and_authenticated_protocol(tmp_path, monkeypatch):
    lock, journal = tmp_path / 'lock', tmp_path / 'journal'
    lock.write_text(''); journal.write_text('{}')
    handle = lock.open('r')
    requests = []
    def response(request):
        requests.append(request)
        if request.url.path.endswith('/identity'):
            return httpx.Response(200, json={'protocol': 3, 'role': 'switch'})
        return httpx.Response(200, json={'protocol': 1, 'enabled': True, 'transition_clear': True})
    pre = CoordinationPreflight('studio-key', 'gate-key', lock, journal,
                                held_lock=SimpleNamespace(handle=handle), transport=httpx.MockTransport(response))
    identities = []
    def identity(unit, account):
        identities.append((unit, account))
        return os.getpid(), 'start'
    monkeypatch.setattr(pre, 'service_identity', identity)
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
        assert identities == [('bc250-ai-studio.service', 'ai-studio'),
                              ('bc250-llm-gate.service', 'ai-studio')] * 2
        assert [r.headers['authorization'] for r in requests] == ['Bearer gate-key', 'Bearer studio-key']
        lock.unlink(); lock.write_text('replaced')
        with pytest.raises(RuntimeError, match='inode changed'):
            pre()
    finally:
        handle.close()


@pytest.mark.parametrize('bad', ['gate', 'studio', 'pid', 'old_protocol'])
def test_preflight_rejects_untrusted_contract(tmp_path, monkeypatch, bad):
    lock, journal = tmp_path / 'lock', tmp_path / 'journal'
    lock.write_text(''); journal.write_text('{}')
    def response(request):
        if request.url.path.endswith('/identity'):
            return httpx.Response(200, json={'protocol': 2 if bad == 'old_protocol' else 3, 'role': 'admin' if bad == 'gate' else 'switch'})
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


@pytest.mark.parametrize('fault', [None, 'unit_uid', 'process_uid', 'process_gid', 'process_privileges'])
def test_service_identity_checks_effective_process_credentials(monkeypatch, fault):
    props = {'LoadState': 'loaded', 'ActiveState': 'active', 'Job': '', 'KillMode': 'control-group',
             'User': 'wrong' if fault == 'unit_uid' else 'ai-client', 'Group': 'ai-client',
             'MainPID': '777', 'NoNewPrivileges': 'yes'}
    monkeypatch.setattr('backend.app.model_control.preflight.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='\n'.join(f'{key}={value}' for key, value in props.items())))
    monkeypatch.setattr('backend.app.model_control.preflight.pwd.getpwnam', lambda _name: SimpleNamespace(pw_uid=1100))
    monkeypatch.setattr('backend.app.model_control.preflight.grp.getgrnam', lambda _name: SimpleNamespace(gr_gid=1100))
    real_read = Path.read_text
    def read_text(path, *args, **kwargs):
        if str(path) == '/proc/777/status':
            uid = '1000 1000 1000 1000' if fault == 'process_uid' else '1100 1100 1100 1100'
            gid = '1000 1000 1000 1000' if fault == 'process_gid' else '1100 1100 1100 1100'
            priv = '0' if fault == 'process_privileges' else '1'
            return f'Uid:\t{uid}\nGid:\t{gid}\nNoNewPrivs:\t{priv}\n'
        if str(path) == '/proc/777/stat': return '777 (test) ' + ' '.join(['0'] * 19 + ['123456'])
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    if fault is None:
        assert CoordinationPreflight.service_identity('unit', 'ai-client') == (777, '123456')
    else:
        with pytest.raises(RuntimeError): CoordinationPreflight.service_identity('unit', 'ai-client')
