import asyncio

import pytest

from backend.app.model_control.coordinator import ModelSwitch, SwitchJournal
from backend.app.model_control.files import FileLock
from backend.app.model_control.operations import SwitchOperations
from backend.app.model_control.worker import ReconciliationError, SwitchWorker


@pytest.mark.parametrize("content,category", [
    (b'{"secret":"private', "invalid_or_untrusted"),
    (b'{"stage": []}', "invalid_or_untrusted"),
    (None, "unavailable"),
])
async def test_bad_journal_allows_only_sanitized_diagnosis(tmp_path, content, category):
    path = tmp_path / "switch.json"
    if content is not None:
        path.write_bytes(content)
    store = SwitchOperations(tmp_path / "operations.sqlite3")
    operation, _ = store.submit("admin", "new", "request")
    switch = ModelSwitch(SwitchJournal(path), asyncio.Lock(), object(), object())
    worker = SwitchWorker(store, switch, FileLock(tmp_path / "worker.lock"), verify_studio=lambda: None)

    assert worker.startup()["state"] == "recovery_required"
    assert worker.inspect_journal() == {"journal": None, "journal_error": category}
    for action in (worker.submit("admin", "new", "other"),
                   worker.run("admin", operation["id"]),
                   worker.recover("admin", operation["id"]),
                   worker.resolve_verified("admin", operation["id"])):
        with pytest.raises(ReconciliationError):
            await action
    assert (path.read_bytes() if path.exists() else None) == content
    assert store.unresolved()["state"] == "recovery_required"
    worker.close()


def test_corrupt_journal_without_operation_still_starts_and_blocks(tmp_path):
    path = tmp_path / "switch.json"
    path.write_text("bad")
    worker = SwitchWorker(SwitchOperations(tmp_path / "operations.sqlite3"),
                          ModelSwitch(SwitchJournal(path), asyncio.Lock(), object(), object()),
                          FileLock(tmp_path / "worker.lock"))
    assert worker.startup() is None
    assert worker.ready and worker.reconciliation_error
    assert worker.inspect_journal()["journal_error"] == "invalid_or_untrusted"
    worker.close()
