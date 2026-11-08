import httpx
import pytest

from backend.app.model_control.adapters import SwitchGate
from backend.app.model_control.coordinator import SwitchAdmissionBlocked


async def test_owned_gate_contract_preserves_independent_hold():
    holds = {'admin-maintenance'}
    def response(request):
        assert request.headers['authorization'] == 'Bearer secret'
        if request.url.path == '/control':
            return httpx.Response(200, json={'protocol': 3, 'durable': True, 'epoch': 'e' * 32, 'holds': sorted(holds), 'paused': bool(holds)})
        hold = request.url.path.rsplit('/', 1)[1]
        if request.method == 'PUT': holds.add(hold)
        else: holds.remove(hold)
        return httpx.Response(200, json={'held': request.method == 'PUT', 'paused': bool(holds)})
    gate = SwitchGate('http://127.0.0.1:8081/control', 'secret', object(), transport=httpx.MockTransport(response))
    hold = 'switch-' + 'a' * 32
    await gate.ensure_available(); await gate.acquire(hold); await gate.release(hold)
    assert holds == {'admin-maintenance'}


@pytest.mark.parametrize('status', [{'paused': False},
                                    {'protocol': 2, 'durable': True, 'epoch': 'e' * 32, 'holds': []},
                                    {'protocol': 3, 'durable': False, 'epoch': 'e' * 32, 'holds': []},
                                    {'protocol': 3, 'durable': True, 'holds': []},
                                    {'protocol': 3, 'durable': True, 'epoch': 'bad', 'holds': []},
                                    {'protocol': 3, 'durable': True, 'epoch': 'e' * 32, 'holds': [], 'paused': True},
                                    {'protocol': 3, 'durable': True, 'epoch': 'e' * 32, 'holds': ['studio-' + 'a' * 32], 'paused': True}])
async def test_owned_gate_rejects_legacy_or_unresolved_hold(status):
    gate = SwitchGate('http://127.0.0.1:8081/control', 'secret', object(),
                      transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=status)))
    with pytest.raises(SwitchAdmissionBlocked): await gate.ensure_available()
