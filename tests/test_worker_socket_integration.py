import asyncio
import json
import os
import tempfile
from argparse import Namespace

import httpx
import pytest

os.environ.setdefault('APP_API_KEY', 'client-key')
os.environ.setdefault('LLM_GATE_API_KEY', 'gate-key')
from backend.app import main
from backend.app.model_control.adapters import AsyncGPULock
from backend.app.model_control.bridge import ModelControlBridge
from backend.app.model_control.cli import run as cli_run
from backend.app.model_control.client import ModelControlClient
from backend.app.model_control.coordinator import ModelSwitch, SwitchJournal
from backend.app.model_control.files import FileLock
from backend.app.model_control.operations import SwitchOperations
from backend.app.model_control.worker import SwitchWorker


class Gate:
    def __init__(self): self.holds = {'admin-maintenance'}
    async def ensure_available(self): pass
    async def acquire(self, hold): self.holds.add(hold)
    async def reserve_recovery(self, hold): return 'e' * 32
    async def verify_recovery_reservation(self, hold, epoch): return list(self.holds)
    async def release_recovery_reservation(self, hold, epoch, holds): await self.release(hold)
    async def release(self, hold): self.holds.remove(hold)
    async def drain(self): pass


class Service:
    def __init__(self):
        self.model = 'old'
        self.entered, self.release = asyncio.Event(), asyncio.Event()
    async def installed(self): return ['old', 'new']
    async def current(self): return self.model
    async def probe(self, model): return model == self.model
    async def ensure_settled(self): pass
    async def healthy(self, model): assert self.model == model
    async def apply(self, model):
        if model == 'new':
            self.entered.set()
            await self.release.wait()
        self.model = model


@pytest.fixture
async def running_worker(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.app.model_control.coordinator.protected_xattrs', lambda _fd: {})
    journal_path = tmp_path / 'switch.json'; journal_path.write_text('{}')
    service, gate = Service(), Gate()
    store = SwitchOperations(tmp_path / 'operations.db')
    switch = ModelSwitch(SwitchJournal(journal_path), AsyncGPULock(tmp_path / 'gpu.lock', file_lock=FileLock(tmp_path / 'gpu.lock')), gate, service)
    worker = SwitchWorker(store, switch, FileLock(tmp_path / 'lifetime.lock'))
    # macOS lacks SO_PEERCRED; Linux production uses bridge.peer_uid by default.
    bridge = ModelControlBridge(worker, 1000, switch_enabled=True, peer_identity=lambda _peer: 1000)
    bridge.start()
    socket_dir = tempfile.TemporaryDirectory(prefix='bc250-', dir='/tmp')
    path = __import__('pathlib').Path(socket_dir.name) / 's'
    server = await asyncio.start_unix_server(bridge.handle, path=str(path), limit=4097)
    yield bridge, service, gate, path, journal_path
    server.close(); await server.wait_closed(); await bridge.close(); socket_dir.cleanup()


async def test_api_cli_and_socket_share_one_durable_worker(running_worker, monkeypatch, capsys):
    bridge, service, gate, path, journal = running_worker
    monkeypatch.setattr(main.get_settings(), 'app_auth_mode', 'key')
    monkeypatch.setattr(main.get_settings(), 'app_api_key', 'client-key')
    monkeypatch.setattr(main.get_settings(), 'operator_key', 'operator-key')
    monkeypatch.setattr(main.get_settings(), 'model_control_enabled', True)
    monkeypatch.setattr(main.get_settings(), 'model_control_socket', str(path))
    headers = {'Authorization': 'Bearer client-key', 'X-BC250-Operator-Key': 'operator-key'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as api:
        assert await cli_run(Namespace(command='list'), ModelControlClient(str(path))) == 0
        # The existing benchmark compares each list line directly with ACTIVE_MODEL.
        assert capsys.readouterr().out.splitlines() == ['old', 'new']
        assert (await api.get('/api/v1/llm/models', headers=headers)).json()['loaded_model'] == 'old'
        invalid = await api.post('/api/v1/llm/switches', headers={**headers, 'Idempotency-Key': 'invalid'}, json={'model': 'missing'})
        assert invalid.status_code == 422
        assert invalid.json()['error']['code'] == 'invalid_model'
        assert (await api.get('/api/v1/llm/switches/current', headers=headers)).json()['operation'] is None
        request_headers = {**headers, 'Idempotency-Key': 'one:shared.key'}
        result = await api.post('/api/v1/llm/switches', headers=request_headers, json={'model': 'new'})
        assert result.status_code == 202
        operation = result.json(); await asyncio.wait_for(service.entered.wait(), 2)
        # Disabling new submissions must not make a lost accepted response
        # undiscoverable by the same idempotency key.
        bridge.switch_enabled = False
        recovered = await api.post('/api/v1/llm/switches', headers=request_headers, json={'model': 'new'})
        assert recovered.status_code == 202 and recovered.json()['id'] == operation['id']
        blocked = await api.post('/api/v1/llm/switches', headers={**headers, 'Idempotency-Key': 'disabled'}, json={'model': 'old'})
        assert blocked.status_code == 503
        bridge.switch_enabled = True
        same = await ModelControlClient(str(path)).request('submit', model='new', idempotency_key='one:shared.key')
        assert same['id'] == operation['id']
        assert (await api.post('/api/v1/llm/switches', headers=request_headers, json={'model': 'new'})).json()['id'] == operation['id']
        conflict = await api.post('/api/v1/llm/switches', headers=request_headers, json={'model': 'old'})
        assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'idempotency_conflict'
        assert (await api.post('/api/v1/llm/switches', headers={**headers, 'Idempotency-Key': 'two'}, json={'model': 'old'})).status_code == 409
        assert await cli_run(Namespace(command='status', operation=operation['id']), ModelControlClient(str(path))) == 0
        assert operation['id'] in capsys.readouterr().out
        service.release.set()
        for _ in range(100):
            final = (await api.get('/api/v1/llm/switches/' + operation['id'], headers=headers)).json()
            if final['state'] == 'succeeded': break
            await asyncio.sleep(.01)
        assert final['state'] == 'succeeded'
        assert final['previous_model'] == 'old' and final['current_model'] == 'new'
        assert gate.holds == {'admin-maintenance'} and json.loads(journal.read_text()) == {}


async def test_socket_peer_and_bad_frames_fail_closed(running_worker):
    bridge, service, _gate, path, _journal = running_worker
    bridge.peer_identity = lambda _peer: 1234
    assert (await ModelControlClient(str(path)).request('models'))['error'] == 'forbidden'
    bridge.peer_identity = lambda _peer: 1000
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b'x' * 4097 + b'\n'); await writer.drain()
    assert json.loads(await reader.readline())['error'] == 'invalid_request'
    writer.close(); await writer.wait_closed()
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b'{"operation":'); await writer.drain(); writer.write_eof()
    assert json.loads(await reader.readline())['error'] == 'invalid_request'
    writer.close(); await writer.wait_closed()
    assert not service.entered.is_set()


async def test_cli_switch_continues_after_submitter_disconnects(running_worker, capsys):
    _bridge, service, _gate, path, _journal = running_worker
    client = ModelControlClient(str(path))
    cli = asyncio.create_task(cli_run(Namespace(command='switch', model='new', key='cli-key'), client))
    await asyncio.wait_for(service.entered.wait(), 2)
    submitted = await client.request('current')
    assert submitted['operation']['model'] == 'new'
    cli.cancel()
    with pytest.raises(asyncio.CancelledError): await cli
    service.release.set()
    for _ in range(100):
        final = await client.request('get', id=submitted['operation']['id'])
        if final['state'] == 'succeeded': break
        await asyncio.sleep(.01)
    assert final['state'] == 'succeeded'
    assert 'operation_id=' in capsys.readouterr().out


async def test_profile_rpc_is_read_only_and_blocked_during_switch(running_worker):
    bridge, service, _gate, path, _journal = running_worker
    class Registry:
        def current_model(self): return service.model
        def model_profile(self, name):
            return {'name': name, 'model_id': f'/models/{name}.gguf', 'configured_context': 4096,
                    'parallel': 1, 'config_sha256': 'a' * 64,
                    'capabilities': {'reasoning_control': 'unknown', 'json_schema': 'unknown'},
                    'runtime_identity': None}
    service.registry = Registry()
    client = ModelControlClient(str(path))
    assert (await client.request('profile'))['name'] == 'old'
    operation = await client.request('submit', model='new', idempotency_key='profile-race')
    await asyncio.wait_for(service.entered.wait(), 2)
    assert (await client.request('profile'))['error'] == 'switch_busy_or_recovery_required'
    service.release.set()
    for _ in range(100):
        result = await client.request('get', id=operation['id'])
        if result['state'] == 'succeeded': break
        await asyncio.sleep(.01)
    assert result['state'] == 'succeeded'
    assert (await client.request('profile'))['name'] == 'new'
