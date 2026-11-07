"""Single worker protocol shared by CLI and Client API."""
import asyncio
import json
import socket
import struct

from .operations import IdempotencyConflict, OperationConflict
from .worker import ReconciliationError

MAX_MESSAGE = 4096
MAX_RESPONSE = 65536
PRINCIPAL = 'single-admin'


def peer_uid(peer):
    return struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


class ModelControlBridge:
    def __init__(self, worker, allowed_uid, *, switch_enabled=False, peer_identity=peer_uid):
        self.worker, self.allowed_uid = worker, allowed_uid
        self.switch_enabled = switch_enabled
        self.peer_identity = peer_identity
        self.healthy = False
        self.tasks = set()

    def start(self):
        pending = self.worker.startup()
        self.healthy = True
        if self.switch_enabled and pending and pending['state'] == 'queued':
            self.schedule(pending['id'])

    def schedule(self, operation_id):
        task = asyncio.create_task(self.worker.run(PRINCIPAL, operation_id))
        self.tasks.add(task)
        def done(result):
            self.tasks.discard(result)
            if result.cancelled() or result.exception() is not None:
                self.healthy = False
        task.add_done_callback(done)

    async def close(self):
        self.healthy = False
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.worker.close()

    async def dispatch(self, request):
        if not self.healthy or not isinstance(request, dict):
            return {'error': 'service_unavailable'}
        action = request.get('operation')
        if action == 'models' and set(request) == {'operation'}:
            service = self.worker.switch.service
            models, current = await service.installed(), await service.current()
            ready = await service.probe(current)
            pending = self.worker.store.unresolved()
            return {'models': [{'name': name, 'current': name == current} for name in models],
                    'configured_model': current, 'loaded_model': current if ready else None,
                    'ready': ready, 'switch_enabled': self.switch_enabled,
                    'active_operation_id': pending['id'] if pending else None}
        if action == 'current' and set(request) == {'operation'}:
            return {'operation': self.worker.store.unresolved()}
        if action == 'get' and set(request) == {'operation', 'id'}:
            return self.worker.store.get(PRINCIPAL, request['id']) or {'error': 'not_found'}
        if action == 'submit' and set(request) == {'operation', 'model', 'idempotency_key'}:
            if not self.switch_enabled:
                return {'error': 'switch_disabled'}
            result, created = await self.worker.submit(PRINCIPAL, request['model'], request['idempotency_key'])
            if created:
                self.schedule(result['id'])
            return result
        raise ValueError('Invalid request')

    async def handle(self, reader, writer):
        try:
            peer = writer.get_extra_info('socket')
            uid = self.peer_identity(peer)
            if uid not in {self.allowed_uid, 0}:
                result = {'error': 'forbidden'}
            else:
                raw = await asyncio.wait_for(reader.readline(), 5)
                if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                    raise ValueError('Invalid message')
                result = await self.dispatch(json.loads(raw))
        except IdempotencyConflict:
            result = {'error': 'idempotency_conflict'}
        except OperationConflict:
            result = {'error': 'switch_busy_or_recovery_required'}
        except (ValueError, UnicodeError, asyncio.LimitOverrunError):
            result = {'error': 'invalid_request'}
        except Exception:
            result = {'error': 'service_unavailable'}
        try:
            encoded = json.dumps(result, separators=(',', ':')).encode() + b'\n'
            writer.write(encoded if len(encoded) <= MAX_RESPONSE else b'{"error":"service_unavailable"}\n')
            await asyncio.wait_for(writer.drain(), 5)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 5)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
