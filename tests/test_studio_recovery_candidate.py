"""Exercise the Studio recovery patch against its archived source candidate."""
import ast
import asyncio
import hashlib
import hmac
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'docs/handoff/imagegen-20261107/source.tar.gz'
PATCH = ROOT / 'deploy/ai-studio-model-switch-recovery.patch'


@pytest.fixture
def endpoint(tmp_path):
    with tarfile.open(ARCHIVE) as archive:
        for name in ('backend/app/main.py', 'backend/app/services/llm.py', 'backend/app/services/gpu.py'):
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(name).read())
    subprocess.run(['patch', '-p1', '-i', str(PATCH)], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse((tmp_path / 'backend/app/main.py').read_text())
    create = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'create_app')
    fn = next(n for n in create.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'switch_recovery')
    fn.decorator_list = []
    fn.args.defaults = [ast.Constant(None)]
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    class Response:
        def __init__(self, content, status_code): self.content, self.status_code = content, status_code
    class Barrier:
        def __init__(self): self.value = {'hold_id': 'switch-' + 'a' * 32, 'old': 'old', 'target': 'new', 'stage': 'switching', 'mutation_started': True}
        def read(self):
            if isinstance(self.value, Exception): raise self.value
            return self.value
    class LLM:
        async def owned_control_identity(self): return True
    barrier = Barrier()
    gpu = SimpleNamespace(model_switch_coordination_enabled=lambda: True, llm=LLM(), switch_barrier=barrier, blocked=False, leased=False, shared_lock_held_by_root=lambda: True)
    db = SimpleNamespace(get_state=lambda key, default: default)
    namespace = {'settings': SimpleNamespace(coordination_key='secret'), 'app': SimpleNamespace(state=SimpleNamespace(gpu=gpu, db=db)), 'JSONResponse': Response, 'hmac': hmac, 'json': json, 'hashlib': hashlib}
    exec(compile(module, '<studio candidate>', 'exec'), namespace)
    return namespace['switch_recovery'], gpu, db, barrier


def call(fn, auth='Bearer secret'):
    return asyncio.run(fn(auth))


def test_recovery_reports_bound_journal_without_acquiring_lock(endpoint):
    fn, gpu, _, barrier = endpoint
    result = call(fn)
    digest = hashlib.sha256(json.dumps(barrier.value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    assert result == {'protocol': 1, 'enabled': True, 'gpu_blocked': True, 'studio_recovery_clear': True, 'gate_verified': True, 'journal_sha256': digest}
    assert call(fn, 'Bearer wrong').status_code == 401
    assert call(fn, None).status_code == 401


def test_recovery_fails_closed_for_unsafe_states(endpoint):
    fn, gpu, db, barrier = endpoint
    barrier.value = {}
    empty = call(fn)
    assert empty['gpu_blocked'] and empty['journal_sha256'] == hashlib.sha256(b'{}').hexdigest()
    gpu.shared_lock_held_by_root = lambda: False
    assert not call(fn)['gpu_blocked']
    gpu.shared_lock_held_by_root = lambda: True
    barrier.value = ValueError('corrupt journal')
    assert not call(fn)['gpu_blocked']
    barrier.value = {'unresolved': True}
    gpu.blocked = True
    assert not call(fn)['studio_recovery_clear']
    gpu.blocked, gpu.leased = False, True
    assert not call(fn)['studio_recovery_clear']
    gpu.leased = False
    db.get_state = lambda key, default: {'pending': True} if key == 'recovery' else default
    assert not call(fn)['studio_recovery_clear']
    async def failed_gate(): raise RuntimeError('unavailable')
    gpu.llm.owned_control_identity = failed_gate
    assert not call(fn)['gate_verified']
    gpu.model_switch_coordination_enabled = lambda: False
    assert not call(fn)['enabled'] and not call(fn)['gpu_blocked']


def test_studio_identity_requires_exact_protocol_three(tmp_path):
    with tarfile.open(ARCHIVE) as archive:
        for name in ('backend/app/main.py', 'backend/app/services/llm.py', 'backend/app/services/gpu.py'):
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(name).read())
    subprocess.run(['patch', '-p1', '-i', str(PATCH)], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse((tmp_path / 'backend/app/services/llm.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SystemdLLM')
    fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'owned_control_identity')
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    class Client:
        value = None
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs): return self
        def raise_for_status(self): pass
        def json(self): return self.value
    namespace = {'httpx': SimpleNamespace(AsyncClient=Client)}
    exec(compile(module, '<studio identity candidate>', 'exec'), namespace)
    instance = SimpleNamespace(s=SimpleNamespace(admission_key='key', admission_url='http://127.0.0.1'))
    for value, expected in [({'role': 'studio', 'protocol': 3}, True), ({'role': 'studio', 'protocol': 2}, False), ({'role': 'studio', 'protocol': 3, 'extra': 1}, False), ({'role': 'studio', 'protocol': True}, False)]:
        Client.value = value
        assert asyncio.run(namespace['owned_control_identity'](instance)) is expected


def test_studio_admission_blocks_conflicting_holds_and_reservations(tmp_path):
    with tarfile.open(ARCHIVE) as archive:
        for name in ('backend/app/main.py', 'backend/app/services/llm.py', 'backend/app/services/gpu.py'):
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(name).read())
    subprocess.run(['patch', '-p1', '-i', str(PATCH)], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse((tmp_path / 'backend/app/services/llm.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SystemdLLM')
    fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'ensure_image_admission')
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    class LockBusyError(RuntimeError): pass
    namespace = {'LockBusyError': LockBusyError}
    exec(compile(module, '<studio admission candidate>', 'exec'), namespace)
    state = {'protocol': 3, 'durable': True, 'holds': [], 'recovery_exclusive_hold': None}
    async def gate(_method): return state
    instance = SimpleNamespace(gate=gate)
    def check(): return asyncio.run(namespace['ensure_image_admission'](instance))
    check()
    for hold in ('startup', 'studio-' + 'a' * 32, 'switch-' + 'b' * 32):
        state['holds'] = [hold]
        with pytest.raises(LockBusyError): check()
    state['holds'] = ['admin-operator']
    check()  # Independent administrative holds are preserved by Studio's owned hold.
    state['recovery_exclusive_hold'] = 'switch-' + 'b' * 32
    with pytest.raises(RuntimeError): check()
    state['recovery_exclusive_hold'] = None
    state['protocol'] = 2
    with pytest.raises(RuntimeError): check()


def test_kernel_lock_observation_is_read_only_and_fails_closed(tmp_path):
    import os
    with tarfile.open(ARCHIVE) as archive:
        for name in ('backend/app/main.py', 'backend/app/services/llm.py', 'backend/app/services/gpu.py'):
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(name).read())
    subprocess.run(['patch', '-p1', '-i', str(PATCH)], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse((tmp_path / 'backend/app/services/gpu.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'GPUManager')
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'shared_lock_held_by_root')
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    lock = tmp_path / 'gpu.lock'
    lock.touch()
    info = lock.stat()
    line = f'1: FLOCK ADVISORY WRITE 123 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n'
    contents = {'/proc/locks': line, '/proc/123/status': 'Name:\tworker\nUid:\t0\t0\t0\t0\n'}
    class ProcPath:
        def __init__(self, path): self.path = path
        def read_text(self):
            if self.path not in contents: raise OSError('unavailable')
            return contents[self.path]
    namespace = {'Path': ProcPath, 'os': os}
    exec(compile(module, '<studio lock observation candidate>', 'exec'), namespace)
    file_lock = SimpleNamespace(_open_secure=lambda: lock.open('r'))
    instance = SimpleNamespace(file_lock=file_lock)
    observe = lambda: namespace['shared_lock_held_by_root'](instance)
    assert observe()
    contents['/proc/123/status'] = 'Uid:\t1000\t1000\t1000\t1000\n'
    assert not observe()
    contents['/proc/123/status'] = 'Uid:\t0\t0\t0\t0\n'
    contents['/proc/locks'] = line.replace('FLOCK', 'POSIX')
    assert not observe()
    contents['/proc/locks'] = line.replace(str(info.st_ino), str(info.st_ino + 1))
    assert not observe()
    contents.pop('/proc/locks')
    assert not observe()
