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
        if (not isinstance(state, dict) or type(state.get("protocol")) is not int or state.get("protocol") != 3
                or state.get("durable") is not True
                or not isinstance(state.get("epoch"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", state["epoch"])
                or not isinstance(state.get("holds"), list)
                or any(not isinstance(hold, str) or not re.fullmatch(
                    r"(?:startup|legacy|admin-[A-Za-z0-9_.:-]{1,128}|(?:client|studio|switch)-[0-9a-f]{32})", hold)
                       for hold in state["holds"])
                or len(set(state["holds"])) != len(state["holds"])
                or "recovery_exclusive_hold" not in state
                or state["recovery_exclusive_hold"] is not None
                or type(state.get("paused")) is not bool
                or state["paused"] != bool(state["holds"])):
            raise SwitchAdmissionBlocked("Owned durable gate protocol required")
        if any(hold == "startup" or hold.startswith(("client-", "studio-", "switch-"))
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

    @staticmethod
    def _recovery_reservation_state(state, hold_id, epoch=None):
        if (not isinstance(state, dict) or type(state.get('protocol')) is not int
                or state['protocol'] != 3 or state.get('durable') is not True
                or state.get('paused') is not True
                or state.get('recovery_exclusive_hold') != hold_id
                or not isinstance(state.get('epoch'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', state['epoch'])
                or (epoch is not None and state['epoch'] != epoch)
                or not isinstance(state.get('holds'), list)):
            raise RuntimeError('Exclusive recovery reservation unavailable')
        holds = state['holds']
        if (holds.count(hold_id) != 1 or len(holds) != len(set(holds))
                or any(not isinstance(h, str) or not (
                    h == hold_id or h in {'startup', 'legacy'}
                    or re.fullmatch(r'admin-[A-Za-z0-9_.:-]{1,128}', h)
                ) for h in holds)):
            raise RuntimeError('Conflicting gate hold during recovery')
        return state['epoch']

    async def reserve_recovery(self, hold_id):
        """Require a durable gate-side exclusion; never emulate it with a GET."""
        if not isinstance(hold_id, str) or not re.fullmatch(r'switch-[0-9a-f]{32}', hold_id):
            raise ValueError('Invalid switch hold ID')
        async with httpx.AsyncClient(timeout=10, transport=self.transport, trust_env=False) as client:
            response = await client.put(self.url + '/holds/' + hold_id + '/recovery-reservation',
                                        headers={'Authorization': f'Bearer {self.key}'})
            response.raise_for_status()
            return self._recovery_reservation_state(response.json(), hold_id)

    async def verify_recovery_reservation(self, hold_id, epoch):
        state = await self.llm.gate('GET')
        self._recovery_reservation_state(state, hold_id, epoch)
        return list(state['holds'])

    @staticmethod
    def _released_recovery_state(state, hold_id, epoch, remaining):
        if (not isinstance(state, dict) or type(state.get('protocol')) is not int
                or state['protocol'] != 3 or state.get('durable') is not True
                or state.get('epoch') != epoch or 'recovery_exclusive_hold' not in state
                or state['recovery_exclusive_hold'] is not None
                or type(state.get('paused')) is not bool
                or state['paused'] != bool(remaining)
                or not isinstance(state.get('holds'), list)
                or any(not isinstance(h, str) for h in state['holds'])
                or sorted(state['holds']) != sorted(remaining) or hold_id in state['holds']):
            raise RuntimeError('Recovery reservation release could not be verified')

    async def release_recovery_reservation(self, hold_id, epoch, holds, *, required_independent_hold=None):
        """Atomically release only the reservation and owner with unchanged state."""
        self._recovery_reservation_state({'protocol': 3, 'durable': True, 'paused': True,
                                          'epoch': epoch, 'recovery_exclusive_hold': hold_id,
                                          'holds': holds}, hold_id, epoch)
        remaining = [h for h in holds if h != hold_id]
        if required_independent_hold is not None and (required_independent_hold not in remaining
                or not (required_independent_hold in {'startup', 'legacy'} or re.fullmatch(
                    r'admin-[A-Za-z0-9_.:-]{1,128}', required_independent_hold))):
            raise ValueError('Independent hold required')
        payload = {'expected_epoch': epoch, 'expected_holds': holds}
        if required_independent_hold is not None:
            payload['required_independent_hold'] = required_independent_hold
        async with httpx.AsyncClient(timeout=10, transport=self.transport, trust_env=False) as client:
            response = await client.post(
                self.url + '/holds/' + hold_id + '/release-recovery-reservation',
                headers={'Authorization': f'Bearer {self.key}'},
                json=payload)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get('held') is not False:
                raise RuntimeError('Recovery hold release could not be verified')
            self._released_recovery_state(value, hold_id, epoch, remaining)
        self._released_recovery_state(await self.llm.gate('GET'), hold_id, epoch, remaining)

    async def release_if_independent(self, hold_id, expected_epoch, required_independent_hold):
        """Require gate-side atomic epoch and independent-hold checks; no fallback."""
        if (not isinstance(hold_id, str) or not re.fullmatch(r"switch-[0-9a-f]{32}", hold_id)
                or not isinstance(expected_epoch, str) or not re.fullmatch(r"[0-9a-f]{32}", expected_epoch)
                or not isinstance(required_independent_hold, str) or not (
                    required_independent_hold in {"startup", "legacy"}
                    or re.fullmatch(r"admin-[A-Za-z0-9_.:-]{1,128}", required_independent_hold))):
            raise ValueError("Invalid conditional release")
        async with httpx.AsyncClient(timeout=10, transport=self.transport, trust_env=False) as client:
            response = await client.post(
                self.url + "/holds/" + hold_id + "/release-if-independent",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"expected_epoch": expected_epoch, "required_independent_hold": required_independent_hold},
            )
            response.raise_for_status()
            value = response.json()
        if (not isinstance(value, dict) or value.get("protocol") != 3
                or value.get("durable") is not True or value.get("epoch") != expected_epoch
                or value.get("held") is not False or value.get("paused") is not True
                or not isinstance(value.get("holds"), list)
                or required_independent_hold not in value["holds"] or hold_id in value["holds"]
                or any(not isinstance(h, str) for h in value["holds"])):
            raise RuntimeError("Conditional hold release could not be verified")
        return value

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
