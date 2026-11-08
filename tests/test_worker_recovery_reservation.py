from types import SimpleNamespace

import httpx
import pytest

from backend.app.model_control.adapters import SwitchGate


OWN = 'switch-' + 'a' * 32
EPOCH = 'e' * 32


@pytest.mark.parametrize('change', ['studio', 'other_switch', 'unknown', 'no_own', 'bad_epoch', 'undurable', 'not_reserved', 'protocol_bool'])
async def test_reservation_rejects_untrusted_gate_state(change):
    state = {'protocol': 3, 'durable': True, 'paused': True, 'epoch': EPOCH,
             'recovery_exclusive_hold': OWN, 'holds': [OWN, 'legacy']}
    if change == 'studio': state['holds'].append('studio-' + 'c' * 32)
    if change == 'other_switch': state['holds'].append('switch-' + 'b' * 32)
    if change == 'unknown': state['holds'].append('mystery')
    if change == 'no_own': state['holds'].remove(OWN)
    if change == 'bad_epoch': state['epoch'] = 'bad'
    if change == 'undurable': state['durable'] = False
    if change == 'not_reserved': state['recovery_exclusive_hold'] = None
    if change == 'protocol_bool': state['protocol'] = True
    gate = SwitchGate('http://127.0.0.1:8081/control', 'key', None,
                      transport=httpx.MockTransport(lambda request: httpx.Response(200, json=state)))
    with pytest.raises(RuntimeError):
        await gate.reserve_recovery(OWN)


async def test_reservation_unsupported_fails_closed():
    gate = SwitchGate('http://127.0.0.1:8081/control', 'key', None,
                      transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    with pytest.raises(httpx.HTTPError):
        await gate.reserve_recovery(OWN)


async def test_reservation_rechecks_same_epoch():
    async def status(method):
        return {'protocol': 3, 'durable': True, 'paused': True, 'epoch': 'f' * 32,
                'recovery_exclusive_hold': OWN, 'holds': [OWN, 'legacy']}
    gate = SwitchGate('http://127.0.0.1:8081/control', 'key', SimpleNamespace(gate=status))
    with pytest.raises(RuntimeError):
        await gate.verify_recovery_reservation(OWN, EPOCH)


@pytest.mark.parametrize('failure', [None, 'unsupported', 'lost_response', 'bad_response', 'bad_readback', 'changed_holds'])
async def test_release_reservation_verifies_response_and_persisted_readback(failure):
    import json
    state = {'protocol': 3, 'durable': True, 'paused': True, 'epoch': EPOCH,
             'recovery_exclusive_hold': OWN, 'holds': [OWN, 'legacy']}
    async def get_state(method):
        result = dict(state)
        if failure == 'bad_readback': result['recovery_exclusive_hold'] = OWN
        return result
    def respond(request):
        assert request.method == 'POST'
        assert json.loads(request.content) == {'expected_epoch': EPOCH, 'expected_holds': [OWN, 'legacy']}
        if failure == 'unsupported': return httpx.Response(404)
        state.update(recovery_exclusive_hold=None, holds=['legacy'])
        if failure == 'lost_response': raise httpx.ReadError('lost', request=request)
        result = dict(state, held=False)
        if failure == 'bad_response': result['durable'] = False
        if failure == 'changed_holds': result['holds'] = []
        return httpx.Response(200, json=result)
    gate = SwitchGate('http://127.0.0.1:8081/control', 'key', SimpleNamespace(gate=get_state),
                      transport=httpx.MockTransport(respond))
    if failure:
        with pytest.raises((RuntimeError, httpx.HTTPError)):
            await gate.release_recovery_reservation(OWN, EPOCH, [OWN, 'legacy'])
    else:
        await gate.release_recovery_reservation(OWN, EPOCH, [OWN, 'legacy'])
        assert state['holds'] == ['legacy'] and state['recovery_exclusive_hold'] is None


async def test_release_reservation_sends_independent_hold_condition():
    import json
    state = {'protocol': 3, 'durable': True, 'paused': True, 'epoch': EPOCH,
             'recovery_exclusive_hold': None, 'holds': ['legacy']}
    async def status(method): return state
    def release(request):
        assert json.loads(request.content) == {'expected_epoch': EPOCH, 'expected_holds': [OWN, 'legacy'],
                                               'required_independent_hold': 'legacy'}
        return httpx.Response(200, json=dict(state, held=False))
    gate = SwitchGate('http://127.0.0.1:8081/control', 'key', SimpleNamespace(gate=status),
                      transport=httpx.MockTransport(release))
    await gate.release_recovery_reservation(OWN, EPOCH, [OWN, 'legacy'], required_independent_hold='legacy')
