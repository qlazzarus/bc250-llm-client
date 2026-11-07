"""Restricted systemd adapter used by the opt-in privileged worker."""

import asyncio
import time

import httpx

from .registry import MODEL_NAME
from .coordinator import ModelHealthTimeout, ModelRestartFailed, SwitchOutcomeUnknown


class SystemctlClient:
    """Invoke only fixed systemctl operations on the configured LLM unit."""

    async def _run(self, operation, *, timeout):
        if operation == "restart":
            args = ("restart", "llama-server.service")
        elif operation == "show":
            args = ("show", "--all", "--property=LoadState,ActiveState,MainPID,Job,KillMode",
                    "llama-server.service")
        else:
            raise ValueError("Unsupported systemctl operation")
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/systemctl", *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException as error:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            if isinstance(error, TimeoutError) and operation == "restart":
                raise SwitchOutcomeUnknown("systemctl restart timed out") from error
            raise
        if process.returncode:
            if operation == "restart":
                raise ModelRestartFailed("systemctl restart failed")
            raise RuntimeError(f"systemctl {operation} failed ({process.returncode})")
        if len(stdout) > 16384:
            raise RuntimeError("Invalid systemctl output")
        return stdout.decode("utf-8", errors="strict")

    async def restart(self, timeout):
        await self._run("restart", timeout=timeout)

    async def show(self, timeout):
        raw = await self._run("show", timeout=timeout)
        data = {}
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in data:
                raise RuntimeError("Invalid systemd state")
            data[key] = value
        required = {"LoadState", "ActiveState", "MainPID", "Job", "KillMode"}
        if set(data) != required:
            raise RuntimeError("Incomplete systemd state")
        return data


class SystemdModelService:
    def __init__(self, registry, writer, systemctl, *, health_timeout=120,
                 settle_timeout=30, api_key="", transport=None):
        self.registry, self.writer, self.systemctl = registry, writer, systemctl
        self.health_timeout, self.settle_timeout = health_timeout, settle_timeout
        self.api_key, self.transport = api_key, transport
        if health_timeout <= 0 or settle_timeout <= 0:
            raise ValueError("Timeouts must be positive")

    async def installed(self):
        return self.registry.list_models()

    async def current(self):
        return self.registry.current_model()

    async def validate_switch_preconditions(self):
        self.writer.validate_replacement()

    async def apply(self, model):
        if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
            raise ValueError("Invalid management model name")
        if model not in self.registry.list_models():
            raise ValueError("Unknown model")
        await self.ensure_settled()
        self.writer.set_active(model)
        await self.systemctl.restart(self.settle_timeout)

    async def ensure_settled(self):
        end = time.monotonic() + self.settle_timeout
        while True:
            state = await self.systemctl.show(min(5, self.settle_timeout))
            if state.get("LoadState") != "loaded" or state.get("KillMode") != "control-group":
                raise RuntimeError("Unsafe systemd service configuration")
            active = state.get("ActiveState")
            pid = state.get("MainPID", "")
            if not pid.isdigit() or "Job" not in state:
                raise RuntimeError("Cannot verify systemd job state")
            if not state["Job"] and active in {"active", "inactive", "failed"}:
                if (active == "active" and int(pid) > 0) or (active != "active" and int(pid) == 0):
                    return state
            if time.monotonic() >= end:
                raise SwitchOutcomeUnknown("Systemd service has not settled")
            await asyncio.sleep(0.25)

    async def probe(self, model):
        try:
            return await asyncio.wait_for(self._probe(model), 5)
        except (asyncio.TimeoutError, OSError):
            return False

    async def _probe(self, model):
        try:
            expected = self.registry.model_id(model)
            state = await self.systemctl.show(2)
            if (state.get("LoadState") != "loaded" or state.get("KillMode") != "control-group"
                    or state.get("ActiveState") != "active" or state.get("Job")
                    or not state.get("MainPID", "").isdigit() or int(state["MainPID"]) <= 0):
                return False
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            async with httpx.AsyncClient(timeout=5, headers=headers, transport=self.transport, trust_env=False) as client:
                health = await client.get("http://127.0.0.1:8080/health")
                models = await client.get("http://127.0.0.1:8080/v1/models")
                data = models.json().get("data", [])
                return (health.status_code == 200 and health.json().get("status") == "ok"
                        and models.status_code == 200 and isinstance(data, list)
                        and any(isinstance(row, dict) and row.get("id") == expected for row in data))
        except (httpx.HTTPError, ValueError, AttributeError, RuntimeError):
            return False

    async def healthy(self, model):
        if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
            raise ValueError("Invalid management model name")
        # Management name and llama's ID are distinct; mapping is an injected
        # trusted resolver, never derived from caller-provided paths.
        expected_id = self.registry.model_id(model)
        if not isinstance(expected_id, str) or not expected_id:
            raise RuntimeError("Cannot verify expected model ID")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        end = time.monotonic() + self.health_timeout
        async with httpx.AsyncClient(timeout=5, headers=headers, transport=self.transport,
                                     trust_env=False) as client:
            while True:
                state = await self.ensure_settled()
                if state["ActiveState"] == "active" and self.registry.current_model() == model:
                    try:
                        health = await client.get("http://127.0.0.1:8080/health")
                        models = await client.get("http://127.0.0.1:8080/v1/models")
                        data = models.json().get("data", [])
                        if (health.status_code == 200 and health.json().get("status") == "ok"
                                and models.status_code == 200 and isinstance(data, list)
                                and any(isinstance(row, dict) and row.get("id") == expected_id
                                        for row in data)):
                            return
                    except (httpx.HTTPError, ValueError, AttributeError):
                        pass
                if time.monotonic() >= end:
                    raise ModelHealthTimeout("Model health check timed out")
                await asyncio.sleep(0.5)
