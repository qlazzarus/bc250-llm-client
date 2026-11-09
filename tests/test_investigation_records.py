import pytest

from backend.app.model_control.investigation_records import observation_digest, replay_investigation


def records():
    common = {'version': 1, 'investigation_id': 'a' * 32, 'association': 'operation', 'operation_id': 'b' * 32}
    obs = {**common, 'kind': 'observation', 'evidence_digest': '9' * 64, 'journal_digest': 'c' * 64,
           'service_generations': {'gate': {'pid': 101, 'starttime': 42},
                                   'studio': {'pid': 102, 'starttime': 43}},
           'gate_epoch': 'e' * 32, 'reservation': None,
           'holds': ['switch-' + 'f' * 32], 'observed_at': 1700000000, 'source': 'gate'}
    review = {**common, 'kind': 'review', 'observation_digest': observation_digest(obs),
              'review_digest': '1' * 64}
    intent = {**common, 'kind': 'intent', 'observation_digest': review['observation_digest'],
              'review_digest': review['review_digest'], 'action': 'gate',
              'attempt': '2' * 64, 'request_digest': '3' * 64}
    outcome = {**intent, 'kind': 'outcome', 'result': 'applied'}
    return [obs, review, intent, outcome]


def test_replay_never_authorizes_or_resolves():
    events = records()
    assert replay_investigation(events[:1]).status == 'unverified'
    assert replay_investigation(events[:2]).status == 'no_attempt'
    assert replay_investigation(events[:3]).status == 'outcome_unknown'
    assert replay_investigation(events).status == 'reconciliation_required'
    events[-1]['result'] = 'unknown'
    assert replay_investigation(events).status == 'outcome_unknown'


@pytest.mark.parametrize('index,key,value', [
    (0, 'version', 2), (0, 'version', True), (0, 'holds', ['bad']),
    (0, 'holds', ['startup', 'startup']), (0, 'observed_at', True),
    (0, 'source', 'unknown'), (0, 'gate_epoch', '0' * 64), (0, 'evidence_digest', 'bad'),
    (1, 'investigation_id', '0' * 32), (1, 'observation_digest', '0' * 64),
    (2, 'review_digest', '0' * 64), (2, 'action', 'other'),
    (3, 'attempt', '0' * 64), (3, 'result', 'success'),
])
def test_invalid_or_unbound_records_fail_closed(index, key, value):
    events = records()
    events[index][key] = value
    assert replay_investigation(events).status == 'unverified'


def test_missing_duplicate_reordered_and_extra_fields_fail_closed():
    events = records()
    for bad in ([], events[1:], [events[0], events[2]], events + [events[-1]],
                [events[0], {**events[1], 'raw_secret': 'secret'}]):
        assert replay_investigation(bad).status == 'unverified'


def test_protocol_hold_identifiers_are_accepted():
    events = records()
    events[0]['holds'] = sorted(['switch-' + 'a' * 32, 'studio-' + 'b' * 32,
                                  'startup', 'legacy', 'admin-Review_1.:'])
    events[1]['observation_digest'] = observation_digest(events[0])
    assert replay_investigation(events[:2]).status == 'no_attempt'


@pytest.mark.parametrize('hold', ['a' * 32, 'switch-' + 'a' * 31, 'studio-' + 'A' * 32,
                                  'admin-', 'admin-' + 'a' * 129, 'admin-bad/secret', 'unknown'])
def test_invalid_protocol_hold_identifiers_are_rejected(hold):
    event = records()[0]
    event['holds'] = [hold]
    with pytest.raises(ValueError):
        observation_digest(event)


def test_unassociated_orphan_is_bound_to_investigation_without_operation_id():
    events = records()
    for event in events:
        event['association'] = 'unassociated'
        event['operation_id'] = None
    digest = observation_digest(events[0])
    for event in events[1:]:
        event['observation_digest'] = digest
    assert replay_investigation(events).status == 'reconciliation_required'
    events[2]['operation_id'] = 'b' * 32
    assert replay_investigation(events).status == 'unverified'


def test_reservation_requires_matching_epoch_and_recorded_switch_hold():
    event = records()[0]
    event['reservation'] = {'hold_id': 'switch-' + 'f' * 32, 'epoch': event['gate_epoch']}
    observation_digest(event)
    for reservation in ({'hold_id': 'switch-' + 'f' * 32, 'epoch': 'a' * 32},
                        {'hold_id': 'switch-' + 'a' * 32, 'epoch': event['gate_epoch']},
                        {'hold_id': 'startup', 'epoch': event['gate_epoch']},
                        {'hold_id': 'switch-' + 'f' * 32}, {}):
        event['reservation'] = reservation
        with pytest.raises(ValueError):
            observation_digest(event)


@pytest.mark.parametrize('generations', [{}, {'gate': {'pid': 1, 'starttime': 1}},
    {'gate': {'pid': True, 'starttime': 1}, 'studio': {'pid': 2, 'starttime': 2}},
    {'gate': {'pid': 1, 'starttime': 0}, 'studio': {'pid': 2, 'starttime': 2}},
    {'gate': {'pid': 1, 'starttime': 1}, 'studio': {'pid': 2, 'starttime': 2, 'extra': 3}}])
def test_each_service_requires_process_generation(generations):
    event = records()[0]
    event['service_generations'] = generations
    with pytest.raises(ValueError):
        observation_digest(event)
