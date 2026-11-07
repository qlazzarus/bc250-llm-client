import asyncio

import pytest
from backend.app.model_control.coordinator import (
    ModelSwitch,
    SwitchAdmissionBlocked,
    SwitchFailed,
    SwitchJournal,
    SwitchOutcomeUnknown,
    SwitchRecoveryRequired,
)


@pytest.fixture(autouse=True)
def local_xattrs(monkeypatch):
    # macOS Python does not expose the Linux fd xattr API used in production.
    monkeypatch.setattr("backend.app.model_control.coordinator.protected_xattrs", lambda _fd: {})


class Gate:
    def __init__(self):
        self.holds = {"administrator"}
        self.calls = []
        self.lose_release_response = False

    async def acquire(self, hold):
        self.calls.append("pause")
        self.holds.add(hold)

    async def drain(self):
        self.calls.append("drain")

    async def release(self, hold):
        self.calls.append("release")
        self.holds.discard(hold)
        if self.lose_release_response:
            self.lose_release_response = False
            raise RuntimeError("response lost")


class Service:
    def __init__(self, gate):
        self.gate, self.model = gate, "old"
        self.calls = []
        self.fail_apply = set()
        self.fail_health = set()
        self.crash = False
        self.outcome_unknown = False
        self.settled = True

    async def installed(self):
        return ["old", "new"]

    async def current(self):
        return self.model

    async def ensure_settled(self):
        if not self.settled:
            raise SwitchOutcomeUnknown("systemd still running")

    async def apply(self, model):
        assert len(self.gate.holds) >= 2
        self.calls.append(("apply", model))
        self.model = model
        if self.outcome_unknown and model == "new":
            raise SwitchOutcomeUnknown("restart timeout")
        if self.crash and model == "new":
            raise SimulatedCrash()
        if model in self.fail_apply:
            raise RuntimeError("restart failed")

    async def healthy(self, model):
        self.calls.append(("health", model))
        if model in self.fail_health or self.model != model:
            raise RuntimeError("health failed")


class SimulatedCrash(BaseException):
    pass


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "switch.json"
    path.write_text("{}")
    journal, gate = SwitchJournal(path), Gate()
    service = Service(gate)
    return ModelSwitch(journal, asyncio.Lock(), gate, service), journal, gate, service


async def test_success_preserves_administrator_pause(setup):
    switch, journal, gate, service = setup
    assert await switch.switch("new") == "succeeded"
    assert service.model == "new" and gate.holds == {"administrator"}
    assert journal.read() == {}
    assert gate.calls == ["pause", "drain", "release"]


@pytest.mark.parametrize("blocked", ["journal", "model"])
async def test_replacement_preflight_blocks_before_hold_or_journal(setup, monkeypatch, blocked):
    switch, journal, gate, service = setup

    if blocked == "journal":
        monkeypatch.setattr(journal, "secure_precreated", True)
        monkeypatch.setattr(journal, "read", lambda: {})
        def fail_journal():
            raise RuntimeError("journal metadata differs")
        monkeypatch.setattr(journal, "validate_replacement", fail_journal)
    else:
        async def fail_model():
            raise RuntimeError("model metadata differs")
        service.validate_switch_preconditions = fail_model

    with pytest.raises(SwitchAdmissionBlocked, match="metadata"):
        await switch.switch("new")
    assert journal.path.read_text() == "{}"
    assert gate.calls == [] and service.calls == [("health", "old")] and service.model == "old"


def test_secure_journal_preflight_rejects_changed_security_attributes(tmp_path, monkeypatch):
    path = tmp_path / "switch.json"
    path.write_text("{}")
    journal = SwitchJournal(path, secure_precreated=True)
    monkeypatch.setattr(journal, "read", lambda: {})
    attributes = iter([{"security.selinux": b"existing"}, {"security.selinux": b"new"}])
    monkeypatch.setattr("backend.app.model_control.coordinator.protected_xattrs", lambda fd: next(attributes))
    with pytest.raises(SwitchRecoveryRequired, match="ACL or SELinux"):
        journal.validate_replacement()
    assert path.read_text() == "{}"
    assert not list(tmp_path.glob(".switch-*"))


@pytest.mark.parametrize("failure", ["restart", "health"])
async def test_failed_switch_restores_and_checks_old_model(setup, failure):
    switch, journal, gate, service = setup
    getattr(service, "fail_apply" if failure == "restart" else "fail_health").add("new")
    with pytest.raises(SwitchFailed):
        await switch.switch("new")
    assert service.model == "old"
    assert service.calls[-1] == ("health", "old")
    assert journal.read() == {} and gate.holds == {"administrator"}
    assert gate.calls == ["pause", "drain", "pause", "drain", "release"]


async def test_failed_rollback_keeps_hold_and_blocks_new_switch(setup):
    switch, journal, gate, service = setup
    service.fail_apply.update({"new", "old"})
    with pytest.raises(SwitchRecoveryRequired):
        await switch.switch("new")
    assert journal.read()["mutation_started"] and len(gate.holds) == 2
    with pytest.raises(SwitchRecoveryRequired):
        await switch.switch("new")
    service.fail_apply.clear()
    assert await switch.recover() == "restored"
    assert journal.read() == {} and gate.holds == {"administrator"}


async def test_interruption_after_mutation_is_recoverable(setup):
    switch, journal, gate, service = setup
    service.crash = True
    with pytest.raises(SimulatedCrash):
        await switch.switch("new")
    assert journal.read()["mutation_started"] and len(gate.holds) == 2
    service.crash = False
    assert await switch.recover() == "restored"
    assert service.model == "old" and gate.holds == {"administrator"}


async def test_lost_resume_response_reacquires_and_drains_before_rollback(setup):
    switch, journal, gate, service = setup
    gate.lose_release_response = True
    with pytest.raises(SwitchFailed):
        await switch.switch("new")
    assert gate.calls == ["pause", "drain", "release", "pause", "drain", "release"]
    assert service.model == "old" and gate.holds == {"administrator"}
    assert journal.read() == {}


async def test_unknown_systemd_result_keeps_hold_and_does_not_restart_old_model(setup):
    switch, journal, gate, service = setup
    service.outcome_unknown = True
    with pytest.raises(SwitchRecoveryRequired):
        await switch.switch("new")
    assert service.calls == [("health", "old"), ("apply", "new")]
    assert journal.read()["mutation_started"] and len(gate.holds) == 2
    service.settled = False
    with pytest.raises(SwitchRecoveryRequired):
        await switch.recover()
    assert service.calls == [("health", "old"), ("apply", "new")]
    service.settled = True
    assert await switch.recover() == "restored"
    assert service.model == "old" and gate.holds == {"administrator"}


async def test_invalid_and_unknown_models_do_not_change_gate(setup):
    switch, journal, gate, service = setup
    for model in ("../old", "missing"):
        with pytest.raises(ValueError):
            await switch.switch(model)
    assert gate.calls == [] and journal.read() == {}


def test_corrupt_or_missing_journal_is_not_treated_as_empty(tmp_path):
    path = tmp_path / "journal"
    journal = SwitchJournal(path)
    with pytest.raises(FileNotFoundError):
        journal.read()
    path.write_text('{"old":"old"}')
    with pytest.raises(SwitchRecoveryRequired):
        journal.read()
