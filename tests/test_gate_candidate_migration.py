import json

import httpx
import pytest

from gate_candidate.gate import create_app
from gate_candidate.migration import legacy_pause_for_rollback, provision_from_legacy_snapshot


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


@pytest.mark.parametrize('original_pause', [False, True])
async def test_migrated_gate_restores_original_pause_after_startup_release(tmp_path, original_pause):
    """Migration, process restart and administrative release preserve old pause."""
    path = tmp_path / 'state.json'
    provision_from_legacy_snapshot(path, {'paused': original_pause,
                                          'active_requests': 0, 'waiting_requests': 0})
    def app():
        return create_app(path, 'http://localhost:8080', admin_key='admin',
                          studio_key='studio', switch_key='switch')
    first = app()
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url='http://gate') as client:
            state = (await client.get('/control', headers={'Authorization': 'Bearer admin'})).json()
            assert set(state['holds']) == ({'startup', 'legacy'} if original_pause else {'startup'})
    second = app()
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url='http://gate') as client:
            release = await client.delete('/control/holds/startup', headers={'Authorization': 'Bearer admin'})
            assert release.status_code == 200
            state = (await client.get('/control', headers={'Authorization': 'Bearer admin'})).json()
            assert state['paused'] is original_pause
            assert state['holds'] == (['legacy'] if original_pause else [])
            assert legacy_pause_for_rollback(state) is original_pause


@pytest.mark.parametrize('changes', [
    {'holds': ['startup', 'switch-' + 'a' * 32], 'paused': True},
    {'holds': ['admin-maintenance'], 'paused': True},
    {'recovery_exclusive_hold': 'switch-' + 'a' * 32},
    {'deployment_generation': 'a' * 32},
    {'active_requests': 1},
    {'waiting_requests': 1},
    {'paused': False},
])
def test_rollback_rejects_unrepresentable_or_busy_gate(changes):
    state = {'protocol': 3, 'durable': True, 'epoch': 'a' * 32,
             'holds': ['startup'], 'paused': True, 'recovery_exclusive_hold': None,
             'deployment_generation': None, 'active_requests': 0, 'waiting_requests': 0}
    state.update(changes)
    with pytest.raises(ValueError):
        legacy_pause_for_rollback(state)
