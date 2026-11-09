import json

import pytest

from gate_candidate.migration import provision_from_legacy_snapshot


@pytest.mark.parametrize('paused', [True, False])
def test_drained_snapshot_preserves_pause_and_refuses_overwrite(tmp_path, paused):
    path = tmp_path / 'state.json'
    snapshot = {'paused': paused, 'active_requests': 0, 'waiting_requests': 0}
    provision_from_legacy_snapshot(path, snapshot)
    state = json.loads(path.read_text())
    assert set(state['holds']) == ({'startup', 'legacy'} if paused else {'startup'})
    assert len(state['epoch']) == 32
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        provision_from_legacy_snapshot(path, snapshot)
    assert path.read_bytes() == original


@pytest.mark.parametrize('snapshot', [
    {'paused': False, 'active_requests': 1, 'waiting_requests': 0},
    {'paused': True, 'active_requests': 0, 'waiting_requests': 1},
    {'paused': 1, 'active_requests': 0, 'waiting_requests': 0},
    {'paused': False, 'active_requests': False, 'waiting_requests': 0},
    {'paused': False, 'active_requests': 0, 'waiting_requests': 0, 'holds': []},
    {},
])
def test_invalid_or_undrained_snapshot_does_not_create_state(tmp_path, snapshot):
    path = tmp_path / 'state.json'
    with pytest.raises(ValueError):
        provision_from_legacy_snapshot(path, snapshot)
    assert not path.exists()
