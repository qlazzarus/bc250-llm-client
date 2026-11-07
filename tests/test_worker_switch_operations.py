from concurrent.futures import ThreadPoolExecutor

import pytest
from backend.app.model_control.operations import (
    IdempotencyConflict,
    OperationConflict,
    SwitchOperations,
)


@pytest.fixture
def store(tmp_path):
    return SwitchOperations(tmp_path / "operations.sqlite3")


def test_idempotency_and_global_conflict(store):
    first, created = store.submit("admin", "model-a", "request-1")
    assert created and first["state"] == "queued"
    repeated, created = store.submit("admin", "model-a", "request-1")
    assert not created and repeated == first
    with pytest.raises(IdempotencyConflict):
        store.submit("admin", "model-b", "request-1")
    with pytest.raises(OperationConflict):
        store.submit("admin", "model-b", "request-2")
    assert store.get("admin", first["id"]) == first
    assert store.get("another", first["id"]) is None


def test_concurrent_requests_allow_only_one_unresolved_switch(store):
    def submit(number):
        try:
            return store.submit("admin", "model-a", f"request-{number}")[0]
        except OperationConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert len([result for result in results if result]) == 1


def test_recovery_required_blocks_new_requests_until_recovered(store):
    operation, _ = store.submit("admin", "model-a", "request-1")
    op_id = operation["id"]
    for state in ("waiting_for_gpu", "pausing", "draining", "switching", "recovery_required"):
        store.transition(op_id, state)
    with pytest.raises(OperationConflict):
        store.submit("admin", "model-b", "request-2")
    store.transition(op_id, "rolling_back")
    restored = store.transition(op_id, "failed_rolled_back", error_code="restart_failed")
    assert restored["error_code"] == "restart_failed"
    assert store.submit("admin", "model-b", "request-2")[1]
    with pytest.raises(OperationConflict):
        store.transition(op_id, "succeeded")


def test_records_survive_reopening_and_do_not_expose_keys(store):
    operation, _ = store.submit("admin", "model-a", "secret-like-idempotency-key")
    reopened = SwitchOperations(store.path)
    assert reopened.get("admin", operation["id"]) == operation
    assert "idempotency_key" not in operation and "principal" not in operation


@pytest.mark.parametrize("model,key", [("../model", "key"), ("model", ""), ("model", "a/b")])
def test_invalid_model_or_key_is_rejected(store, model, key):
    with pytest.raises(ValueError):
        store.submit("admin", model, key)


def test_error_codes_cannot_contain_arbitrary_details(store):
    operation, _ = store.submit("admin", "model-a", "key")
    with pytest.raises(ValueError):
        store.transition(operation["id"], "failed", error_code="private diagnostic")
    assert store.get("admin", operation["id"])["state"] == "queued"
