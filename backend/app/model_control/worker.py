"""Singleton switch worker used by the opt-in Unix socket service.

An independent lifetime lock is required: the per-transition GPU lock alone does
not establish a singleton worker. Call close only after all requests have ended.
"""

import asyncio
import os

from .coordinator import SwitchAdmissionBlocked, SwitchFailed
from .operations import OperationConflict


class ReconciliationError(RuntimeError):
    pass


class SwitchWorker:
    def __init__(self, store, switch, lifetime_lock, *, verify_studio=None):
        gpu_lock = getattr(switch.lock, "file_lock", None)
        if (gpu_lock is not None and hasattr(gpu_lock, "path") and hasattr(lifetime_lock, "path")
                and os.path.abspath(gpu_lock.path) == os.path.abspath(lifetime_lock.path)):
            raise ValueError("Worker lifetime lock must differ from GPU lock")
        self.store, self.switch = store, switch
        self.lifetime_lock = lifetime_lock
        self.verify_studio = verify_studio
        self.ready = False
        self.inflight = 0

    def startup(self):
        """Acquire the lifetime lock before examining any recovery records."""
        if self.ready:
            raise ReconciliationError("Worker is already running")
        self.lifetime_lock.acquire()
        try:
            journal = self.switch.journal.read()
            operation = self.store.unresolved()
            if journal and (not operation or journal.get("operation_id") != operation["id"]
                            or journal["target"] != operation["model"]):
                if operation and operation["state"] != "recovery_required":
                    self.store.transition(operation["id"], "recovery_required",
                                          error_code="interrupted")
                raise ReconciliationError("Switch journal and operation do not match")
            if operation and (journal or operation["state"] not in {"queued", "recovery_required"}):
                if operation["state"] != "recovery_required":
                    operation = self.store.transition(operation["id"], "recovery_required",
                                                      error_code="interrupted")
            self.ready = True
            return operation
        except BaseException:
            self.lifetime_lock.release()
            raise

    def close(self):
        if self.inflight:
            raise ReconciliationError("Cannot release worker lock while requests are active")
        self.ready = False
        self.lifetime_lock.release()

    def _require_ready(self):
        if not self.ready:
            raise ReconciliationError("Worker startup reconciliation required")

    async def submit(self, principal, model, idempotency_key):
        self._require_ready()
        self.inflight += 1
        try:
            existing = self.store.find_idempotent(principal, model, idempotency_key)
            if existing is not None:
                return existing, False
            if model not in await self.switch.service.installed():
                raise ValueError("Unknown model")
            return self.store.submit(principal, model, idempotency_key)
        finally:
            self.inflight -= 1

    async def run(self, principal, operation_id):
        self._require_ready()
        self.inflight += 1
        try:
            return await self._run(principal, operation_id)
        finally:
            self.inflight -= 1

    async def _run(self, principal, operation_id):
        operation = self.store.get(principal, operation_id)
        if operation is None:
            raise ValueError("Unknown operation")
        # Atomic transition claims the queued operation exactly once.
        self.store.transition(operation_id, "waiting_for_gpu")

        def progress(stage):
            self.store.transition(operation_id, stage)

        try:
            await self.switch.switch(operation["model"], operation_id=operation_id, progress=progress,
                                     verified=lambda **values: self.store.record_verified(operation_id, **values))
        except SwitchAdmissionBlocked:
            return self.store.transition(operation_id, "failed", error_code="admission_blocked")
        except SwitchFailed as error:
            return self.store.transition(operation_id, "failed_rolled_back",
                                         error_code=error.error_code)
        except Exception:
            return self.store.transition(operation_id, "recovery_required",
                                         error_code="recovery_required")
        return self.store.transition(operation_id, "succeeded")

    async def recover(self, principal, operation_id):
        self._require_ready()
        self.inflight += 1
        try:
            return await self._recover(principal, operation_id)
        finally:
            self.inflight -= 1

    async def resolve_verified(self, principal, operation_id):
        """Close an interrupted operation without a journal after verifying current state.

        This never changes the model or gate holds. Only the root socket caller
        can request it, and an existing independent gate hold must remain set.
        """
        self._require_ready()
        self.inflight += 1
        try:
            if self.verify_studio is None:
                raise ReconciliationError("Studio verification is unavailable")
            async with self.switch.lock:
                operation = self.store.get(principal, operation_id)
                if operation is None or operation["state"] != "recovery_required":
                    raise OperationConflict("Operation does not require recovery")
                if self.switch.journal.read():
                    raise ReconciliationError("A journal must use normal recovery")
                try:
                    await asyncio.to_thread(self.verify_studio)
                    gate = self.switch.gate
                    status = await gate.llm.gate("GET")
                    holds = status.get("holds")
                    if (status.get("protocol") != 2 or status.get("durable") is not True
                            or status.get("paused") is not True or not isinstance(holds, list)
                            or not holds or any(not isinstance(h, str) or not (
                                h == "startup" or h == "legacy" or h.startswith("admin-")
                            ) for h in holds)):
                        raise ReconciliationError("An independent gate hold is required")
                    await self.switch.service.ensure_settled()
                    await gate.drain()
                    await self.switch.service.healthy(await self.switch.service.current())
                    # Recheck before closing the barrier; do not release any hold.
                    after = await gate.llm.gate("GET")
                    if (after.get("paused") is not True or after.get("holds") != holds
                            or after.get("protocol") != 2 or after.get("durable") is not True):
                        raise ReconciliationError("Gate state changed during verification")
                except ReconciliationError:
                    raise
                except Exception as error:
                    raise ReconciliationError("Current state could not be verified") from error
                return self.store.transition(operation_id, "failed", error_code="interrupted")
        finally:
            self.inflight -= 1

    async def _recover(self, principal, operation_id):
        operation = self.store.get(principal, operation_id)
        if operation is None or operation["state"] != "recovery_required":
            raise OperationConflict("Operation does not require recovery")
        journal = self.switch.journal.read()
        if (not journal or journal.get("operation_id") != operation_id
                or journal["target"] != operation["model"]):
            raise ReconciliationError("No matching switch journal; manual verification required")
        self.store.transition(operation_id, "rolling_back")
        try:
            await self.switch.recover()
        except Exception:
            return self.store.transition(operation_id, "recovery_required",
                                         error_code="recovery_required")
        return self.store.transition(operation_id, "failed_rolled_back",
                                     error_code="service_unavailable")
