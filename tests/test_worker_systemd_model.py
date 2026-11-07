import asyncio

import httpx
import pytest
from backend.app.model_control.coordinator import SwitchOutcomeUnknown
from backend.app.model_control.systemd import SystemctlClient, SystemdModelService


class Registry:
    def __init__(self):
        self.active = "old"

    def list_models(self):
        return ["old", "new"]

    def current_model(self):
        return self.active

    def model_id(self, name):
        return f"/opt/models/{name}.gguf"


class Writer:
    def __init__(self, registry):
        self.registry = registry

    def set_active(self, name):
        self.registry.active = name


class Systemctl:
    def __init__(self):
        self.job = ""
        self.restarts = 0

    async def restart(self, timeout):
        self.restarts += 1

    async def show(self, timeout):
        return {"LoadState": "loaded", "KillMode": "control-group", "Job": self.job,
                "ActiveState": "active", "MainPID": "42"}


def service(registry, systemctl, model_id=None):
    def reply(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": [{"id": model_id or registry.model_id("new")}]})

    return SystemdModelService(registry, Writer(registry), systemctl, health_timeout=0.01,
                               settle_timeout=0.01, transport=httpx.MockTransport(reply))


async def test_health_checks_gguf_id_not_management_name():
    registry, systemctl = Registry(), Systemctl()
    llm = service(registry, systemctl)
    await llm.apply("new")
    await llm.healthy("new")
    assert registry.active == "new" and systemctl.restarts == 1


async def test_health_rejects_wrong_loaded_model():
    registry, systemctl = Registry(), Systemctl()
    llm = service(registry, systemctl, model_id="new")
    await llm.apply("new")
    with pytest.raises(RuntimeError, match="health check timed out"):
        await llm.healthy("new")


async def test_unsettled_systemd_job_prevents_recovery():
    registry, systemctl = Registry(), Systemctl()
    systemctl.job = "42 /org/freedesktop/systemd1/job/42"
    with pytest.raises(SwitchOutcomeUnknown, match="not settled"):
        await service(registry, systemctl).ensure_settled()
    assert registry.active == "old" and systemctl.restarts == 0


async def test_restart_timeout_reports_unknown_even_after_client_is_killed(monkeypatch):
    calls = []

    class Process:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()

    async def start(*args, **kwargs):
        calls.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    with pytest.raises(SwitchOutcomeUnknown, match="timed out"):
        await SystemctlClient().restart(0.01)
    assert calls == [("/usr/bin/systemctl", "restart", "llama-server.service")]
    assert process.returncode == -9


async def test_apply_rejects_unknown_model_before_config_change():
    registry, systemctl = Registry(), Systemctl()
    llm = service(registry, systemctl)
    with pytest.raises(ValueError):
        await llm.apply("../old")
    assert registry.active == "old" and systemctl.restarts == 0


async def test_apply_rejects_unsettled_systemd_before_changing_active_configuration():
    registry, systemctl = Registry(), Systemctl()
    systemctl.job = "42 /org/freedesktop/systemd1/job/42"
    with pytest.raises(SwitchOutcomeUnknown):
        await service(registry, systemctl).apply("new")
    assert registry.active == "old" and systemctl.restarts == 0


async def test_probe_returns_promptly_if_systemd_is_stuck():
    class Slow:
        async def show(self, _timeout):
            await asyncio.sleep(30)
    registry = Registry()
    service = SystemdModelService(registry, Writer(registry), Slow())
    assert await service.probe('model') is False
