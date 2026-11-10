"""Single worker protocol shared by CLI and Client API."""
import asyncio
import json
import socket
import struct

from .operations import IdempotencyConflict, OperationConflict
from .recovery_diagnosis import classify_recovery_records
from .worker import ReconciliationError, UnknownModel

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
        if self.switch_enabled and not self.worker.reconciliation_error and pending and pending['state'] == 'queued':
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

    async def dispatch(self, request, *, uid=None):
        if not self.healthy or not isinstance(request, dict):
            return {'error': 'service_unavailable'}
        action = request.get('operation')
        if action == 'inspect_recovery' and set(request) == {'operation'}:
            if uid != 0:
                return {'error': 'forbidden'}
            pending = self.worker.store.unresolved()
            hold_id = self.worker.store.hold_for(pending['id']) if pending else None
            journal = self.worker.inspect_journal()
            return {'operation': pending,
                    'hold_id': hold_id,
                    **journal,
                    # These separate reads are only an investigative hint: they
                    # do not inspect gate state or form an atomic snapshot and
                    # must never authorize recovery or unblock switching.
                    'record_diagnosis': classify_recovery_records(
                        pending, hold_id, journal['journal'], journal['journal_error']),
                    'diagnosis_scope': 'investigation_only_non_atomic_no_gate',
                    'reconciliation_error': self.worker.reconciliation_error}
        if action in {'recover', 'resolve_verified'} and set(request) == {'operation', 'id'}:
            if uid != 0:
                return {'error': 'forbidden'}
            method = getattr(self.worker, action)
            return await method(PRINCIPAL, request['id'])
        if action == 'profile' and set(request) == {'operation'}:
            # Fail closed while a queued, running or unresolved operation exists.
            if self.worker.reconciliation_error or self.worker.store.unresolved():
                return {'error': 'switch_busy_or_recovery_required'}
            registry = self.worker.switch.service.registry
            name = registry.current_model()
            profile = registry.model_profile(name)
            # Detect configuration replacement while reading. Gate epoch checks
            # around this request detect a coordinated model switch.
            if (registry.current_model() != name or registry.model_profile(name) != profile
                    or self.worker.store.unresolved()):
                return {'error': 'switch_busy_or_recovery_required'}
            return profile
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
                # A lost submit response must remain discoverable while the
                # operator has disabled new switches. This is read-only: a
                # queued operation is not scheduled by this lookup.
                existing = self.worker.store.find_idempotent(PRINCIPAL, request['model'], request['idempotency_key'])
                if existing is not None:
                    return existing
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
                request = json.loads(raw)
                if isinstance(request, dict) and request.get("operation") in {"recover", "resolve_verified"} and uid == 0:
                    task = asyncio.create_task(self.dispatch(request, uid=uid))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    result = await asyncio.shield(task)
                else:
                    result = await self.dispatch(request, uid=uid)
        except asyncio.CancelledError:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
            raise
        except UnknownModel:
            result = {'error': 'invalid_model'}
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
