from backend.app.model_control.recovery_progress import RecoveryProgress, replay_recovery_progress


def intent():
    return {"kind": "intent", "action": "gate", "attempt": "a" * 64,
            "request_digest": "b" * 64}


def observation(result="applied"):
    return {**intent(), "kind": "observation", "result": result}


def test_interrupted_attempt_and_lost_response_are_unknown():
    expected = RecoveryProgress("outcome_unknown", "gate", "a" * 64)
    assert replay_recovery_progress([intent()]) == expected
    assert replay_recovery_progress([intent(), observation("unknown")]) == expected


def test_reported_outcomes_never_authorize_completion_or_retry():
    for result in ("applied", "not_applied"):
        assert replay_recovery_progress([intent(), observation(result)]).status == "reconciliation_required"
    assert replay_recovery_progress([]).status == "no_attempt"


def test_unbound_duplicate_or_reordered_records_are_unverified():
    for events in ([observation()], [observation(), intent()], [intent(), intent()],
                   [intent(), observation(), observation()],
                   [intent(), {**observation(), "attempt": "c" * 64}],
                   [intent(), {**observation(), "request_digest": "c" * 64}],
                   [intent(), {**observation(), "action": "db"}],
                   [intent(), {**observation(), "result": "success"}],
                   [{**intent(), "extra": True}], [{**intent(), "attempt": "bad"}]):
        assert replay_recovery_progress(events).status == "unverified"
