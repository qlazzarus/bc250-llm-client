from backend.app.model_control.recovery_diagnosis import classify_recovery_records


OP = {"id": "a" * 32, "model": "new"}
HOLD = "switch-" + "b" * 32
JOURNAL = {"operation_id": "a" * 32, "target": "new", "old": "old",
           "hold_id": HOLD, "stage": "switching", "mutation_started": True}


def test_classifies_only_observed_records_without_repair():
    assert classify_recovery_records(None, None, JOURNAL, None)["category"] == "orphan_journal"
    assert classify_recovery_records(OP, HOLD, {}, None)["category"] == "empty_journal"
    assert classify_recovery_records(OP, HOLD, JOURNAL, None)["category"] == "records_match"
    mismatch = {**JOURNAL, "operation_id": "c" * 32, "target": "other"}
    assert classify_recovery_records(OP, None, mismatch, None) == {
        "category": "records_mismatch", "mismatches": ["operation_id", "target", "hold_id"]}
    assert JOURNAL["hold_id"] == HOLD


def test_legacy_journal_cannot_be_assumed_to_match():
    legacy = {key: value for key, value in JOURNAL.items() if key != "operation_id"}
    assert classify_recovery_records(OP, HOLD, legacy, None)["mismatches"] == ["operation_id"]


def test_unreadable_or_malformed_records_never_match():
    assert classify_recovery_records(OP, HOLD, JOURNAL, "unavailable")["category"] == "journal_unverified"
    assert classify_recovery_records(OP, HOLD, {"secret": "bad"}, None) == {
        "category": "journal_unverified", "mismatches": []}
    assert classify_recovery_records({"id": "bad", "model": "new"}, HOLD, JOURNAL, None)["category"] == "operation_unverified"
    assert classify_recovery_records(None, HOLD, {}, None)["category"] == "operation_unverified"
