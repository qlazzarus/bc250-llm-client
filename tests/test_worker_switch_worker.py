import asyncio
import subprocess
import sys

import pytest
from backend.app.model_control.files import FileLock, LockBusyError
from backend.app.model_control.coordinator import ModelSwitch, SwitchJournal
from backend.app.model_control.adapters import AsyncGPULock
from backend.app.model_control.operations import OperationConflict, SwitchOperations
from backend.app.model_control.worker import ReconciliationError, SwitchWorker


@pytest.fixture(autouse=True)
def local_xattrs(monkeypatch):
    # macOS Python does not expose the Linux fd xattr API used in production.
    monkeypatch.setattr("backend.app.model_control.coordinator.protected_xattrs", lambda _fd: {})


class Crash(BaseException):
    pass


class Gate:
    def __init__(self):
        self.holds = {"legacy"}

    async def acquire(self, hold):
        self.holds.add(hold)

    async def reserve_recovery(self, hold):
        assert hold in self.holds
        return 'e' * 32

    async def verify_recovery_reservation(self, hold, epoch):
        assert hold in self.holds and epoch == 'e' * 32
        return list(self.holds)

    async def release_recovery_reservation(self, hold, epoch, holds):
        assert set(holds) == self.holds
        await self.release(hold)

    async def drain(self):
        pass

    async def release(self, hold):
        self.holds.discard(hold)


class Service:
    def __init__(self):
        self.model = "old"
        self.available = ["old", "new"]
        self.crash = False
        self.fail = False

    async def installed(self):
        return self.available

    async def current(self):
        return self.model

    async def ensure_settled(self):
        pass

    async def apply(self, model):
        self.model = model
        if self.crash and model == "new":
            raise Crash()
        if self.fail and model == "new":
            raise RuntimeError("restart failed")

    async def healthy(self, model):
        if model != self.model:
            raise RuntimeError("wrong model")


@pytest.fixture
def worker(tmp_path):
    store = SwitchOperations(tmp_path / "operations.sqlite3")
    path = tmp_path / "switch.json"
    path.write_text("{}")
    journal, gate, service = SwitchJournal(path), Gate(), Service()
    switch = ModelSwitch(journal, asyncio.Lock(), gate, service)
    return SwitchWorker(store, switch, FileLock(tmp_path / "worker.lock")), store, journal, gate, service


async def test_worker_runs_one_queued_request_and_reports_success(worker):
    instance, store, journal, gate, service = worker
    assert instance.startup() is None
    operation, created = await instance.submit("admin", "new", "request-1")
    assert created and operation["state"] == "queued"
    result = await instance.run("admin", operation["id"])
    assert result["state"] == "succeeded" and service.model == "new"
    assert journal.read() == {} and gate.holds == {"legacy"}
    assert store.get("admin", operation["id"]) == result
    with pytest.raises(OperationConflict):
        await instance.run("admin", operation["id"])


async def test_restart_reconciles_interrupted_operation_and_recovers(worker):
    instance, store, journal, gate, service = worker
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    service.crash = True
    with pytest.raises(Crash):
        await instance.run("admin", operation["id"])
    assert journal.read()["operation_id"] == operation["id"]
    assert store.get("admin", operation["id"])["state"] == "switching"
    instance.close()  # Simulate process exit releasing its independent singleton lock.
    restarted = SwitchWorker(store, instance.switch, FileLock(instance.lifetime_lock.path), verify_studio=lambda: None)
    assert restarted.startup()["state"] == "recovery_required"
    with pytest.raises(OperationConflict):
        await restarted.submit("admin", "old", "request-2")
    service.crash = False
    result = await restarted.recover("admin", operation["id"])
    assert result["state"] == "failed_rolled_back"
    assert journal.read() == {} and gate.holds == {"legacy"} and service.model == "old"


async def test_ambiguous_interruption_without_journal_requires_manual_verification(worker):
    instance, store, journal, _, _ = worker
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    store.transition(operation["id"], "waiting_for_gpu")
    instance.close()
    restarted = SwitchWorker(store, instance.switch, FileLock(instance.lifetime_lock.path))
    assert restarted.startup()["state"] == "recovery_required"
    assert journal.read() == {}
    with pytest.raises(ReconciliationError):
        await restarted.recover("admin", operation["id"])
    with pytest.raises(OperationConflict):
        await restarted.submit("admin", "old", "request-2")


async def test_orphan_journal_prevents_worker_start(worker):
    instance, store, journal, _, _ = worker
    journal.write({"hold_id": "switch-" + "a" * 32, "old": "old", "target": "new",
                   "stage": "switching", "mutation_started": True})
    instance.startup()
    assert instance.reconciliation_error
    with pytest.raises(ReconciliationError):
        await instance.submit("admin", "new", "request-1")
    assert store.unresolved() is None


async def test_idempotent_lookup_survives_model_removal(worker):
    instance, _, _, _, service = worker
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    service.available.remove("new")
    repeated, created = await instance.submit("admin", "new", "request-1")
    assert not created and repeated == operation


async def test_failure_is_not_reported_as_success(worker):
    instance, _, journal, gate, service = worker
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    service.fail = True
    result = await instance.run("admin", operation["id"])
    assert result["state"] == "failed_rolled_back"
    assert service.model == "old" and journal.read() == {} and gate.holds == {"legacy"}


async def test_cleanup_failure_requires_recovery_without_rollback(worker, monkeypatch):
    instance, store, journal, gate, service = worker
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")

    def fail_after_replacement():
        journal.path.write_text('{}')
        raise OSError('directory fsync failed after replacement')

    monkeypatch.setattr(journal, 'clear', fail_after_replacement)
    result = await instance.run('admin', operation['id'])
    assert result['state'] == 'recovery_required' and service.model == 'new'
    assert journal.read() == {} and len(gate.holds) == 2
    with pytest.raises(OperationConflict):
        await instance.submit('admin', 'old', 'request-2')
    assert store.get('admin', operation['id'])['current_model'] is None


async def test_mismatched_journal_blocks_startup_and_new_requests(worker):
    instance, store, journal, _, _ = worker
    operation, _ = store.submit("admin", "new", "request-1")
    journal.write({"hold_id": "switch-" + "a" * 32, "old": "old", "target": "new",
                   "stage": "switching", "mutation_started": True,
                   "operation_id": "b" * 32})
    instance.startup()
    assert instance.reconciliation_error
    assert store.get("admin", operation["id"])["state"] == "recovery_required"
    with pytest.raises(ReconciliationError):
        await instance.submit("admin", "old", "request-2")


async def test_two_workers_cannot_claim_the_same_operation(worker):
    instance, _, _, _, service = worker
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    entered, release = asyncio.Event(), asyncio.Event()
    original_apply = service.apply

    async def delayed_apply(model):
        entered.set()
        await release.wait()
        await original_apply(model)

    service.apply = delayed_apply
    task = asyncio.create_task(instance.run("admin", operation["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ReconciliationError):
            instance.close()
        with pytest.raises(OperationConflict):
            await instance.run("admin", operation["id"])
        release.set()
        assert (await asyncio.wait_for(task, 2))["state"] == "succeeded"
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_second_worker_cannot_start_while_first_owns_lifetime_lock(worker):
    instance, store, _, _, _ = worker
    instance.startup()
    second = SwitchWorker(store, instance.switch, FileLock(instance.lifetime_lock.path))
    with pytest.raises(LockBusyError):
        second.startup()
    instance.close()
    assert second.startup() is None
    second.close()


def test_lifetime_lock_excludes_another_process(worker):
    instance, _, _, _, _ = worker
    code = ("import fcntl,sys; f=open(sys.argv[1], 'a+'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('ready', flush=True); sys.stdin.buffer.read(1)")
    child = subprocess.Popen([sys.executable, "-c", code, str(instance.lifetime_lock.path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline() == b"ready\n"
        with pytest.raises(LockBusyError):
            instance.startup()
        child.stdin.write(b"x")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        assert instance.startup() is None
        instance.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdin.close()
        child.stdout.close()


def test_worker_lifetime_lock_must_not_be_gpu_lock(worker):
    instance, store, _, _, _ = worker
    path = instance.lifetime_lock.path
    instance.switch.lock = AsyncGPULock(path, file_lock=FileLock(path))
    with pytest.raises(ValueError, match="must differ"):
        SwitchWorker(store, instance.switch, FileLock(path))


async def test_resolve_without_journal_requires_verified_paused_current_model(worker):
    instance, store, journal, gate, service = worker
    checks = []
    class GateStatus:
        async def gate(self, method):
            checks.append("gate")
            return {"protocol": 3, "recovery_exclusive_hold": None, "durable": True, "paused": True, "epoch": "e" * 32, "holds": ["startup"]}

    gate.llm = GateStatus()
    instance.verify_studio = lambda: checks.append("studio")
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    store.transition(operation["id"], "waiting_for_gpu")
    store.transition(operation["id"], "recovery_required", error_code="interrupted")
    result = await instance.resolve_verified("admin", operation["id"])
    assert result["state"] == "failed" and result["error_code"] == "interrupted"
    assert checks == ["studio", "gate", "gate"]
    assert gate.holds == {"legacy"} and journal.read() == {} and service.model == "old"


@pytest.mark.parametrize("status", [
    {"protocol": 3, "recovery_exclusive_hold": None, "durable": True, "paused": False, "holds": []},
    {"protocol": 3, "recovery_exclusive_hold": None, "durable": True, "paused": True, "holds": ["switch-" + "a" * 32]},
    {"protocol": 1, "durable": True, "paused": True, "holds": ["startup"]},
])
async def test_resolve_without_journal_preserves_uncertain_state(worker, status):
    instance, store, _, gate, _ = worker
    class GateStatus:
        async def gate(self, method):
            return status

    gate.llm = GateStatus()
    instance.verify_studio = lambda: None
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    store.transition(operation["id"], "waiting_for_gpu")
    store.transition(operation["id"], "recovery_required", error_code="interrupted")
    with pytest.raises(ReconciliationError):
        await instance.resolve_verified("admin", operation["id"])
    assert store.get("admin", operation["id"])["state"] == "recovery_required"






@pytest.mark.parametrize("kind", ["drain_timeout", "restart_failed", "health_timeout"])
async def test_failed_rolled_back_reports_specific_sanitized_reason(worker, kind):
    from backend.app.model_control.llm import BusyError
    from backend.app.model_control.coordinator import ModelHealthTimeout, ModelRestartFailed

    instance, _, journal, gate, service = worker
    if kind == "drain_timeout":
        attempts = 0
        async def drain():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BusyError("private drain detail")
        gate.drain = drain
    elif kind == "restart_failed":
        original_apply = service.apply
        async def apply(model):
            if model == "new":
                raise ModelRestartFailed("private systemd detail")
            await original_apply(model)
        service.apply = apply
    else:
        original_healthy = service.healthy
        async def healthy(model):
            if model == "new":
                raise ModelHealthTimeout("private health detail")
            await original_healthy(model)
        service.healthy = healthy
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    result = await instance.run("admin", operation["id"])
    assert result["state"] == "failed_rolled_back" and result["error_code"] == kind
    assert "private" not in str(result) and journal.read() == {} and service.model == "old"


async def test_switch_rejects_uncoordinated_studio_before_creating_journal(worker):
    instance, _, journal, gate, service = worker
    def fail_preflight():
        raise RuntimeError("Studio uses the previous GPU lock")

    instance.switch.preflight = fail_preflight
    instance.startup()
    operation, _ = await instance.submit("admin", "new", "request-1")
    result = await instance.run("admin", operation["id"])
    assert result["state"] == "failed" and result["error_code"] == "admission_blocked"
    assert journal.read() == {} and gate.holds == {"legacy"} and service.model == "old"

async def test_hold_correlation_survives_restart_and_cannot_be_replaced(worker):
    instance, store, journal, gate, service = worker
    instance.startup()
    operation, _ = await instance.submit('admin', 'new', 'hold-test')
    service.crash = True
    with pytest.raises(Crash):
        await instance.run('admin', operation['id'])
    hold = journal.read()['hold_id']
    assert store.hold_for(operation['id']) == hold
    with pytest.raises(OperationConflict):
        store.record_hold(operation['id'], 'switch-' + 'f' * 32)
    instance.close()
    reopened = SwitchOperations(store.path)
    assert reopened.hold_for(operation['id']) == hold


async def test_root_recovery_actions_reject_client_uid(worker):
    from backend.app.model_control.bridge import ModelControlBridge
    instance, _, _, _, _ = worker
    bridge = ModelControlBridge(instance, 1000)
    bridge.start()
    for action in ('recover', 'resolve_verified'):
        assert await bridge.dispatch({'operation': action, 'id': 'a' * 32}, uid=1000) == {'error': 'forbidden'}
    assert await bridge.dispatch({'operation': 'inspect_recovery'}, uid=1000) == {'error': 'forbidden'}
    assert 'journal' in await bridge.dispatch({'operation': 'inspect_recovery'}, uid=0)
    await bridge.close()


@pytest.mark.parametrize('failure', [None, 'unsupported', 'lost_response', 'changed_hold'])
async def test_empty_journal_uses_conditional_release_and_preserves_independent_holds(worker, failure):
    instance, store, journal, gate, service = worker
    own = 'switch-' + 'a' * 32
    gate.holds = {'legacy', 'admin-maintenance', own}
    class GateStatus:
        async def gate(self, method):
            return {'protocol': 3, 'recovery_exclusive_hold': None, 'durable': True, 'paused': True,
                    'epoch': 'e' * 32, 'holds': sorted(gate.holds)}
    gate.llm = GateStatus()
    async def release(hold, epoch, independent):
        assert hold == own and epoch == 'e' * 32
        assert independent in {'legacy', 'admin-maintenance'}
        if failure == 'unsupported':
            raise RuntimeError('unsupported')
        gate.holds.remove(hold)
        if failure == 'lost_response':
            raise RuntimeError('response lost')
        holds = sorted(gate.holds)
        if failure == 'changed_hold':
            holds.remove('legacy')
        return {'holds': holds}
    gate.release_if_independent = release
    instance.verify_studio = lambda: None
    instance.startup()
    operation, _ = await instance.submit('admin', 'new', 'conditional-test')
    store.transition(operation['id'], 'waiting_for_gpu')
    store.record_hold(operation['id'], own)
    store.transition(operation['id'], 'recovery_required', error_code='interrupted')
    if failure:
        with pytest.raises(ReconciliationError):
            await instance.resolve_verified('admin', operation['id'])
        assert store.get('admin', operation['id'])['state'] == 'recovery_required'
        assert own in gate.holds
    else:
        assert (await instance.resolve_verified('admin', operation['id']))['state'] == 'failed'
        assert own not in gate.holds
    assert {'legacy', 'admin-maintenance'} <= gate.holds
    assert journal.read() == {} and service.model == 'old'


@pytest.mark.parametrize('failure', ['lost_response', 'terminal_db'])
async def test_empty_journal_failure_survives_worker_restart(worker, monkeypatch, failure):
    instance, store, journal, gate, service = worker
    own = 'switch-' + 'b' * 32
    gate.holds = {'legacy', own}
    class GateStatus:
        async def gate(self, method):
            return {'protocol': 3, 'recovery_exclusive_hold': None, 'durable': True, 'paused': True,
                    'epoch': 'e' * 32, 'holds': sorted(gate.holds)}
    gate.llm = GateStatus()
    async def release(hold, epoch, independent):
        gate.holds.remove(hold)
        if failure == 'lost_response':
            raise RuntimeError('lost response')
        return {'holds': sorted(gate.holds)}
    gate.release_if_independent = release
    instance.verify_studio = lambda: None
    instance.startup()
    operation, _ = await instance.submit('admin', 'new', 'restart-failure')
    store.transition(operation['id'], 'waiting_for_gpu')
    store.record_hold(operation['id'], own)
    store.transition(operation['id'], 'recovery_required', error_code='interrupted')
    original = store.transition
    def transition(*args, **kwargs):
        if failure == 'terminal_db' and args[1] == 'failed':
            raise RuntimeError('database unavailable')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'transition', transition)
    with pytest.raises((ReconciliationError, RuntimeError)):
        await instance.resolve_verified('admin', operation['id'])
    instance.close()
    reopened = SwitchOperations(store.path)
    restarted = SwitchWorker(reopened, instance.switch, FileLock(instance.lifetime_lock.path), verify_studio=lambda: None)
    restarted.startup()
    assert reopened.get('admin', operation['id'])['state'] == 'recovery_required'
    assert journal.read() == {} and 'legacy' in gate.holds
    if failure == 'lost_response':
        assert own in gate.holds
    restarted.close()


async def test_concurrent_recovery_requests_are_serialized(worker):
    instance, store, journal, gate, service = worker
    instance.verify_studio = lambda: None
    instance.startup()
    operation, _ = await instance.submit('admin', 'new', 'concurrent-recovery')
    store.transition(operation['id'], 'waiting_for_gpu')
    store.transition(operation['id'], 'recovery_required', error_code='interrupted')
    class GateStatus:
        async def gate(self, method):
            await asyncio.sleep(0)
            return {'protocol': 3, 'recovery_exclusive_hold': None, 'durable': True, 'paused': True,
                    'epoch': 'e' * 32, 'holds': ['legacy']}
    gate.llm = GateStatus()
    results = await asyncio.gather(instance.resolve_verified('admin', operation['id']),
                                   instance.resolve_verified('admin', operation['id']),
                                   instance.recover('admin', operation['id']),
                                   return_exceptions=True)
    assert sum(isinstance(result, dict) and result['state'] == 'failed' for result in results) == 1
    assert sum(isinstance(result, OperationConflict) for result in results) == 2
    assert journal.read() == {} and gate.holds == {'legacy'}


@pytest.mark.parametrize('failure', [None, 'lost_response', 'other_reservation'])
async def test_empty_journal_recovers_own_reservation_with_independent_hold(worker, failure):
    instance, store, journal, gate, service = worker
    own = 'switch-' + 'c' * 32
    gate.holds = {'legacy', own}
    reservation = own if failure != 'other_reservation' else 'switch-' + 'd' * 32
    class GateStatus:
        async def gate(self, method):
            return {'protocol': 3, 'durable': True, 'paused': True, 'epoch': 'e' * 32,
                    'holds': sorted(gate.holds), 'recovery_exclusive_hold': reservation}
    gate.llm = GateStatus()
    calls = []
    async def release(hold, epoch, holds, *, required_independent_hold):
        calls.append((hold, epoch, holds, required_independent_hold))
        assert required_independent_hold == 'legacy'
        gate.holds.remove(hold)
        if failure == 'lost_response':
            raise RuntimeError('lost response')
    gate.release_recovery_reservation = release
    instance.verify_studio = lambda: None
    instance.startup()
    operation, _ = await instance.submit('admin', 'new', 'reserved-empty')
    store.transition(operation['id'], 'waiting_for_gpu')
    store.record_hold(operation['id'], own)
    store.transition(operation['id'], 'recovery_required', error_code='interrupted')
    if failure:
        with pytest.raises(ReconciliationError):
            await instance.resolve_verified('admin', operation['id'])
        assert store.get('admin', operation['id'])['state'] == 'recovery_required'
        assert own in gate.holds
        assert bool(calls) == (failure == 'lost_response')
    else:
        assert (await instance.resolve_verified('admin', operation['id']))['state'] == 'failed'
        assert len(calls) == 1 and own not in gate.holds
    assert gate.holds >= {'legacy'} and journal.read() == {}
