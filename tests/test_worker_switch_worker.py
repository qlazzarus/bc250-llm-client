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
    restarted = SwitchWorker(store, instance.switch, FileLock(instance.lifetime_lock.path))
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
    with pytest.raises(ReconciliationError):
        instance.startup()
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


async def test_mismatched_journal_blocks_startup_and_new_requests(worker):
    instance, store, journal, _, _ = worker
    operation, _ = store.submit("admin", "new", "request-1")
    journal.write({"hold_id": "switch-" + "a" * 32, "old": "old", "target": "new",
                   "stage": "switching", "mutation_started": True,
                   "operation_id": "b" * 32})
    with pytest.raises(ReconciliationError):
        instance.startup()
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
            return {"protocol": 2, "durable": True, "paused": True, "holds": ["startup"]}

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
    {"protocol": 2, "durable": True, "paused": False, "holds": []},
    {"protocol": 2, "durable": True, "paused": True, "holds": ["switch-" + "a" * 32]},
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
