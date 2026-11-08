"""Candidate GPU lock and gate adapters; no model switch is exposed here."""

import asyncio
import re
import time
from urllib.parse import urlsplit

import httpx

from .files import FileLock, LockBusyError
from .coordinator import SwitchAdmissionBlocked


class AsyncGPULock:
    """Wait for the same pre-created flock used by Studio."""

    def __init__(self, path, *, poll_interval=0.25, file_lock=None):
        if poll_interval <= 0:
            raise ValueError("Poll interval must be positive")
        self.file_lock = file_lock or FileLock(path, secure_precreated=True)
        self.poll_interval = poll_interval

    async def __aenter__(self):
        while True:
            try:
                self.file_lock.acquire()
                return self
            except LockBusyError:
                await asyncio.sleep(self.poll_interval)

    async def __aexit__(self, exc_type, exc, traceback):
        self.file_lock.release()


class SwitchGate:
    """Use a switch-specific gate hold and the existing gate/slots drain check."""

    def __init__(self, admission_url, admission_key, llm, *, transport=None):
        url = urlsplit(admission_url)
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/control"
                or url.username is not None or url.password is not None
                or url.query or url.fragment or not url.port):
            raise ValueError("Admission control must be a loopback control URL")
        if not admission_key:
            raise ValueError("Admission control credential required")
        self.url, self.key, self.llm, self.transport = admission_url, admission_key, llm, transport

    async def ensure_available(self):
        try:
            async with httpx.AsyncClient(timeout=10, transport=self.transport, trust_env=False) as client:
                response = await client.get(self.url, headers={"Authorization": f"Bearer {self.key}"})
                response.raise_for_status()
                state = response.json()
        except (httpx.HTTPError, ValueError) as error:
            # This check runs before journal creation or service mutation.
            raise SwitchAdmissionBlocked("Cannot verify admission state") from error
        if (not isinstance(state, dict) or state.get("protocol") != 3
                or state.get("durable") is not True
                or not isinstance(state.get("epoch"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", state["epoch"])
                or not isinstance(state.get("holds"), list)
                or any(not isinstance(hold, str) for hold in state["holds"])
                or type(state.get("paused")) is not bool
                or state["paused"] != bool(state["holds"])):
            raise SwitchAdmissionBlocked("Owned durable gate protocol required")
        if any(hold == "startup" or hold.startswith(("studio-", "switch-"))
               for hold in state["holds"]):
            raise SwitchAdmissionBlocked("Independent GPU recovery or gate startup requires attention")

    async def _hold(self, hold_id, acquire):
        if not isinstance(hold_id, str) or not re.fullmatch(r"switch-[0-9a-f]{32}", hold_id):
            raise ValueError("Invalid switch hold ID")
        async with httpx.AsyncClient(timeout=10, transport=self.transport, trust_env=False) as client:
            response = await client.request(
                "PUT" if acquire else "DELETE",
                self.url + "/holds/" + hold_id,
                headers={"Authorization": f"Bearer {self.key}"},
            )
            response.raise_for_status()
            value = response.json()
        if (not isinstance(value, dict) or type(value.get("held")) is not bool
                or type(value.get("paused")) is not bool or value["held"] != acquire
                or (acquire and not value["paused"])):
            raise RuntimeError("Admission gate returned an invalid switch hold state")

    async def acquire(self, hold_id):
        await self._hold(hold_id, True)

    async def release(self, hold_id):
        await self._hold(hold_id, False)

    async def drain(self):
        await self.llm.drain()

    async def drain_recovery(self):
        state = await self.llm.service_state()
        if state.get("ActiveState") not in {"inactive", "failed"}:
            return await self.drain()
        # A failed restart has no /slots endpoint. Verify the entire cgroup and
        # wait for the gate to finish its outstanding upstream failures instead.
        await self.llm.verify_stopped(state, allow_failed=True)
        end = time.monotonic() + self.llm.s.drain_timeout
        while True:
            status = await self.llm.gate("GET")
            if not status["paused"]:
                raise RuntimeError("Gate opened during recovery")
            if status["active_requests"] == 0:
                return
            if time.monotonic() >= end:
                raise RuntimeError("Gate requests did not finish during recovery")
            await asyncio.sleep(0.25)
