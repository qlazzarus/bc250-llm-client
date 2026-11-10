"""Kill an isolated worker at durable cleanup boundaries against the real gate candidate."""
import asyncio
import os
from pathlib import Path
import signal
import socket
import sys

import httpx
import pytest
import uvicorn

from backend.app.model_control.adapters import AsyncGPULock, SwitchGate
from backend.app.model_control.coordinator import ModelSwitch, SwitchJournal
from backend.app.model_control.files import FileLock
from backend.app.model_control.operations import SwitchOperations
from backend.app.model_control.worker import SwitchWorker
from gate_candidate.gate import create_app, provision


class Service:
    def __init__(self, path): self.path = path
    async def installed(self): return ['a', 'b']
    async def current(self): return self.path.read_text()
    async def ensure_settled(self): pass
    async def healthy(self, model): assert await self.current() == model
    async def apply(self, model): self.path.write_text(model)


class LLM:
    def __init__(self, url, transport=None): self.url, self.transport = url, transport
    async def gate(self, method):
        async with httpx.AsyncClient(transport=self.transport, trust_env=False) as client:
            result = await client.request(method, self.url, headers={'Authorization': 'Bearer switch'})
            result.raise_for_status()
            return result.json()
    async def drain(self): pass
    async def service_state(self): return {'ActiveState': 'active'}


def make_worker(path, url, transport=None):
    store = SwitchOperations(path / 'operations.sqlite3')
    gate = SwitchGate(url, 'switch', LLM(url, transport), transport=transport)
    lock = AsyncGPULock(path / 'gpu.lock', file_lock=FileLock(path / 'gpu.lock'))
    switch = ModelSwitch(SwitchJournal(path / 'journal.json'), lock, gate, Service(path / 'model'))
    return SwitchWorker(store, switch, FileLock(path / 'worker.lock'), verify_studio=lambda: None)


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


async def crash_child(path, url, point):
    # Only the test's macOS xattr substitute is used; no production checks change.
    import backend.app.model_control.coordinator as coordinator
    coordinator.protected_xattrs = lambda _: {}
    worker = make_worker(path, url)
    worker.startup()
    operation, _ = await worker.submit('admin', 'b', 'crash-test')
    if point in {'before_clear', 'after_clear'}:
        clear = worker.switch.journal.clear
        def interrupted_clear():
            if point == 'before_clear': kill()
            clear()
            kill()
        worker.switch.journal.clear = interrupted_clear
    elif point in {'before_release', 'after_release'}:
        release = worker.switch.gate.release_recovery_reservation
        async def interrupted_release(*args):
            if point == 'before_release': kill()
            await release(*args)
            kill()
        worker.switch.gate.release_recovery_reservation = interrupted_release
    else:
        transition = worker.store.transition
        def interrupted_transition(operation_id, state, **kwargs):
            if state == 'succeeded': kill()
            return transition(operation_id, state, **kwargs)
        worker.store.transition = interrupted_transition
    await worker.run('admin', operation['id'])
    raise AssertionError('worker did not reach crash point')


@pytest.mark.asyncio
@pytest.mark.parametrize('point', ['before_clear', 'after_clear', 'before_release', 'after_release', 'before_terminal'])
async def test_sigkill_cleanup_restarts_and_preserves_independent_hold(tmp_path, monkeypatch, point):
    monkeypatch.setattr('backend.app.model_control.coordinator.protected_xattrs', lambda _: {})
    (tmp_path / 'journal.json').write_text('{}')
    (tmp_path / 'model').write_text('a')
    provision(tmp_path / 'gate.json')
    app = create_app(tmp_path / 'gate.json', 'http://upstream', admin_key='admin', client_key='client',
                     studio_key='studio', switch_key='switch')
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as admin:
            headers = {'Authorization': 'Bearer admin'}
            assert (await admin.delete('/control/holds/startup', headers=headers)).status_code == 200
            assert (await admin.put('/control', headers=headers, json={'paused': True})).status_code == 200
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0)); sock.listen(20); sock.setblocking(False)
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='warning'))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                for _ in range(200):
                    if server.started: break
                    await asyncio.sleep(.01)
                assert server.started
                url = f'http://127.0.0.1:{port}/control'
                child = await asyncio.create_subprocess_exec(sys.executable, __file__, str(tmp_path), url, point,
                                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
                assert child.returncode == -signal.SIGKILL, (child.returncode, stdout, stderr)
                worker = make_worker(tmp_path, url, transport)
                try:
                    pending = worker.startup()
                    assert pending['state'] == 'recovery_required'
                    assert 'legacy' in (await app.state.gate.status())['holds']
                    if point == 'before_clear':
                        result = await worker.recover('admin', pending['id'])
                        assert result['state'] == 'failed_rolled_back'
                        assert (tmp_path / 'model').read_text() == 'a'
                    else:
                        result = await worker.resolve_verified('admin', pending['id'])
                        assert result['state'] == 'failed'
                        assert (tmp_path / 'model').read_text() == 'b'
                    assert worker.switch.journal.read() == {}
                    assert (await app.state.gate.status())['holds'] == ['legacy']
                finally:
                    worker.close()
            finally:
                server.should_exit = True
                await task
                sock.close()


if __name__ == '__main__':
    asyncio.run(crash_child(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
