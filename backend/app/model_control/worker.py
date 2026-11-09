"""Singleton switch worker used by the opt-in Unix socket service.

An independent lifetime lock is required: the per-transition GPU lock alone does
not establish a singleton worker. Call close only after all requests have ended.
"""

import asyncio
import os
import re

from .coordinator import SwitchAdmissionBlocked, SwitchFailed, finish_before_cancelling
from .barrier import SwitchRecoveryRequired
from .operations import OperationConflict


class ReconciliationError(RuntimeError):
    pass


class UnknownModel(ValueError):
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
        self.reconciliation_error = None
        self.recovery_lock = asyncio.Lock()

    def startup(self):
        """Acquire the lifetime lock before examining any recovery records."""
        if self.ready:
            raise ReconciliationError("Worker is already running")
        self.lifetime_lock.acquire()
        try:
            operation = self.store.unresolved()
            diagnosis = self.inspect_journal()
            if diagnosis["journal_error"]:
                if operation and operation["state"] != "recovery_required":
                    operation = self.store.transition(operation["id"], "recovery_required",
                                                      error_code="interrupted")
                self.reconciliation_error = "Switch journal cannot be verified"
                self.ready = True
                return operation
            journal = diagnosis["journal"]
            if journal and (not operation or journal.get("operation_id") != operation["id"]
                            or journal["target"] != operation["model"]
                            or journal["hold_id"] != self.store.hold_for(operation["id"])):
                if operation and operation["state"] != "recovery_required":
                    operation = self.store.transition(operation["id"], "recovery_required",
                                                      error_code="interrupted")
                self.reconciliation_error = "Switch journal and operation do not match"
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

    def inspect_journal(self):
        """Return a sanitized diagnosis without modifying or exposing invalid data."""
        try:
            return {"journal": self.switch.journal.read(), "journal_error": None}
        except OSError:
            return {"journal": None, "journal_error": "unavailable"}
        except (SwitchRecoveryRequired, ValueError, TypeError, UnicodeError):
            return {"journal": None, "journal_error": "invalid_or_untrusted"}

    async def submit(self, principal, model, idempotency_key):
        self._require_ready()
        if self.reconciliation_error:
            raise ReconciliationError(self.reconciliation_error)
        self.inflight += 1
        try:
            existing = self.store.find_idempotent(principal, model, idempotency_key)
            if existing is not None:
                return existing, False
            if model not in await self.switch.service.installed():
                raise UnknownModel("Unknown model")
            return self.store.submit(principal, model, idempotency_key)
        finally:
            self.inflight -= 1

    async def run(self, principal, operation_id):
        self._require_ready()
        if self.reconciliation_error:
            raise ReconciliationError(self.reconciliation_error)
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
                                     verified=lambda **values: self.store.record_verified(operation_id, **values),
                                     hold_created=lambda hold: self.store.record_hold(operation_id, hold))
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
            async with self.recovery_lock:
                return await self._recover(principal, operation_id)
        finally:
            self.inflight -= 1

    async def resolve_verified(self, principal, operation_id):
        """Close an interrupted operation without a journal after verifying current state.

        A correlated hold can be released only by the gate's atomic conditional
        operation. Only the root socket caller can request this recovery.
        """
        self._require_ready()
        self.inflight += 1
        try:
            async with self.recovery_lock:
                return await finish_before_cancelling(self._resolve_guarded(principal, operation_id))
        finally:
            self.inflight -= 1

    async def _resolve_guarded(self, principal, operation_id):
        with self.switch.origin_lock():
            return await self._resolve_verified(principal, operation_id)

    async def _resolve_verified(self, principal, operation_id):
            if self.verify_studio is None or self.reconciliation_error:
                raise ReconciliationError("Recovery verification unavailable or records disagree")
            async with self.switch.lock:
                operation = self.store.get(principal, operation_id)
                if operation is None or operation["state"] != "recovery_required":
                    raise OperationConflict("Operation does not require recovery")
                if self.switch.journal.read():
                    raise ReconciliationError("A journal must use normal recovery")
                try:
                    await asyncio.to_thread(self.verify_studio)
                    if self.switch.journal.read():
                        raise ReconciliationError("Journal changed during recovery verification")
                    gate = self.switch.gate
                    status = await gate.llm.gate("GET")
                    holds = status.get("holds")
                    own_hold = self.store.hold_for(operation_id)
                    if (type(status.get("protocol")) is not int or status.get("protocol") != 3
                            or "recovery_exclusive_hold" not in status
                            or status["recovery_exclusive_hold"] not in (None, own_hold)
                            or (status["recovery_exclusive_hold"] is not None and own_hold not in holds)
                            or status.get("durable") is not True
                            or not isinstance(status.get("epoch"), str)
                            or not re.fullmatch(r"[0-9a-f]{32}", status["epoch"])
                            or status.get("paused") is not True or not isinstance(holds, list)
                            or not holds or not any(isinstance(h, str) and (
                                h in {"startup", "legacy"} or h.startswith("admin-")
                            ) for h in holds) or any(not isinstance(h, str) or not (
                                h == own_hold or h == "startup" or h == "legacy" or h.startswith("admin-")
                            ) for h in holds)):
                        raise ReconciliationError("An independent gate hold is required")
                    await self.switch.service.ensure_settled()
                    await gate.drain()
                    await self.switch.service.healthy(await self.switch.service.current())
                    # Recheck before requesting an atomic conditional release.
                    after = await gate.llm.gate("GET")
                    if (after.get("paused") is not True or after.get("holds") != holds
                            or type(after.get("protocol")) is not int or after.get("protocol") != 3
                            or "recovery_exclusive_hold" not in after
                            or after["recovery_exclusive_hold"] != status["recovery_exclusive_hold"]
                            or after.get("durable") is not True
                            or after.get("epoch") != status["epoch"]):
                        raise ReconciliationError("Gate state changed during verification")
                    if own_hold and own_hold in holds:
                        independent = next(h for h in holds if h != own_hold)
                        try:
                            if status["recovery_exclusive_hold"] == own_hold:
                                await gate.release_recovery_reservation(
                                    own_hold, status["epoch"], holds, required_independent_hold=independent)
                            else:
                                released = await gate.release_if_independent(own_hold, status["epoch"], independent)
                                if sorted(released["holds"]) != sorted(h for h in holds if h != own_hold):
                                    raise ReconciliationError("Other gate holds changed")
                        except Exception as error:
                            # A lost response may have removed the hold. Restore it
                            # if possible; either way preserve the unresolved record.
                            try:
                                await gate.acquire(own_hold)
                            except Exception:
                                pass
                            raise ReconciliationError("Conditional release outcome is uncertain") from error
                except ReconciliationError:
                    raise
                except Exception as error:
                    raise ReconciliationError("Current state could not be verified") from error
                return self.store.transition(operation_id, "failed", error_code="interrupted")

    async def _recover(self, principal, operation_id):
        if self.reconciliation_error or self.verify_studio is None:
            raise ReconciliationError("Recovery verification unavailable or records disagree")
        operation = self.store.get(principal, operation_id)
        if operation is None or operation["state"] != "recovery_required":
            raise OperationConflict("Operation does not require recovery")
        journal = self.switch.journal.read()
        if (not journal or journal.get("operation_id") != operation_id
                or journal["target"] != operation["model"]
                or journal["hold_id"] != self.store.hold_for(operation_id)):
            raise ReconciliationError("No matching switch journal; manual verification required")
        self.store.transition(operation_id, "rolling_back")
        try:
            await self.switch.recover(expected=journal, preflight=self.verify_studio)
        except Exception:
            return self.store.transition(operation_id, "recovery_required",
                                         error_code="recovery_required")
        return self.store.transition(operation_id, "failed_rolled_back",
                                     error_code="service_unavailable")
