"""Exercise the bridge's peer identity boundary without a live Unix listener."""
import asyncio
import json
import socket
import struct
from types import SimpleNamespace

import pytest

from backend.app.model_control.bridge import ModelControlBridge, peer_uid


class Peer:
    def __init__(self, uid):
        self.uid = uid

    def getsockopt(self, level, option, size):
        assert (level, option, size) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return struct.pack('3i', 123, self.uid, 456)


class Writer:
    def __init__(self, uid):
        self.peer = Peer(uid)
        self.data = b''
        self.closed = False

    def get_extra_info(self, name):
        assert name == 'socket'
        return self.peer

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class Worker:
    def __init__(self):
        self.calls = []
        self.reconciliation_error = None
        self.store = SimpleNamespace(unresolved=lambda: None)
        self.switch = SimpleNamespace(journal=SimpleNamespace(read=lambda: {}))
        self.closed = False

    def startup(self):
        return None

    def close(self):
        self.closed = True

    def inspect_journal(self):
        return {'journal': {}, 'journal_error': None}

    async def recover(self, principal, operation_id):
        self.calls.append(('recover', principal, operation_id))
        return {'id': operation_id, 'state': 'failed_rolled_back'}

    async def resolve_verified(self, principal, operation_id):
        self.calls.append(('resolve_verified', principal, operation_id))
        return {'id': operation_id, 'state': 'failed'}


def reader(data):
    result = asyncio.StreamReader()
    result.feed_data(data)
    result.feed_eof()
    return result


@pytest.fixture(autouse=True)
def linux_peer_option(monkeypatch):
    # macOS lacks SO_PEERCRED; retain the actual parser and socket call contract.
    monkeypatch.setattr(socket, 'SO_PEERCRED', getattr(socket, 'SO_PEERCRED', 17), raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['recover', 'resolve_verified', 'inspect_recovery'])
@pytest.mark.parametrize('uid', [0, 1000, 1001])
async def test_handle_uses_peer_uid_for_recovery(action, uid):
    worker = Worker()
    bridge = ModelControlBridge(worker, 1000, peer_identity=peer_uid)
    bridge.start()
    payload = {'operation': action}
    if action != 'inspect_recovery':
        payload['id'] = 'a' * 32
    writer = Writer(uid)
    await bridge.handle(reader(json.dumps(payload).encode() + b'\n'), writer)
    result = json.loads(writer.data)
    if uid != 0:
        assert result == {'error': 'forbidden'}
        assert not worker.calls
    elif action == 'inspect_recovery':
        assert result['journal'] == {} and result['operation'] is None
    else:
        assert worker.calls == [(action, 'single-admin', 'a' * 32)]
        assert result['id'] == 'a' * 32
    assert writer.closed
    await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [b'{bad}\n', b'{"operation":"recover"}\n',
                                     b'{"operation":"inspect_recovery","id":"x"}\n',
                                     b'{}'])
async def test_handle_rejects_invalid_root_requests(payload):
    worker = Worker()
    bridge = ModelControlBridge(worker, 1000, peer_identity=peer_uid)
    bridge.start()
    writer = Writer(0)
    await bridge.handle(reader(payload), writer)
    assert json.loads(writer.data) == {'error': 'invalid_request'}
    assert not worker.calls
    await bridge.close()


@pytest.mark.asyncio
async def test_disconnected_recovery_survives_and_close_waits():
    worker = Worker()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def recover(principal, operation_id):
        entered.set()
        await finish.wait()
        worker.calls.append((principal, operation_id))
        return {'id': operation_id, 'state': 'failed_rolled_back'}

    worker.recover = recover
    bridge = ModelControlBridge(worker, 1000, peer_identity=peer_uid)
    bridge.start()
    request = json.dumps({'operation': 'recover', 'id': 'a' * 32}).encode() + b'\n'
    writer = Writer(0)
    handling = asyncio.create_task(bridge.handle(reader(request), writer))
    await entered.wait()
    handling.cancel()  # Simulate the caller disappearing while recovery runs.
    with pytest.raises(asyncio.CancelledError):
        await handling
    assert writer.closed
    closing = asyncio.create_task(bridge.close())
    await asyncio.sleep(0)
    assert not closing.done() and not worker.closed
    finish.set()
    await closing
    assert worker.calls == [('single-admin', 'a' * 32)]
    assert worker.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('uid', [0, 1000, 1001])
async def test_record_diagnosis_is_root_only_and_investigative(uid):
    worker = Worker()
    operation = {'id': 'a' * 32, 'model': 'new', 'state': 'recovery_required'}
    hold = 'switch-' + 'b' * 32
    journal = {'operation_id': 'c' * 32, 'target': 'new', 'old': 'old',
               'hold_id': hold, 'stage': 'switching', 'mutation_started': True}
    worker.store = SimpleNamespace(unresolved=lambda: operation, hold_for=lambda _: hold)
    worker.inspect_journal = lambda: {'journal': journal, 'journal_error': None}
    bridge = ModelControlBridge(worker, 1000, peer_identity=peer_uid)
    bridge.start()
    writer = Writer(uid)
    await bridge.handle(reader(b'{"operation":"inspect_recovery"}\n'), writer)
    result = json.loads(writer.data)
    if uid == 0:
        assert result['record_diagnosis'] == {
            'category': 'records_mismatch', 'mismatches': ['operation_id']}
        assert result['diagnosis_scope'] == 'investigation_only_non_atomic_no_gate'
        assert worker.store.unresolved() == operation and worker.inspect_journal()['journal'] == journal
    else:
        assert result == {'error': 'forbidden'}
    assert not worker.calls
    await bridge.close()
