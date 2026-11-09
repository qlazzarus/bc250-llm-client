"""Exercise the worker adapter against the actual local protocol 3 gate."""

import httpx
import pytest
import asyncio

from backend.app.model_control.adapters import SwitchGate
from backend.app.model_control.coordinator import SwitchAdmissionBlocked
from backend.app.model_control.coordinator import ModelSwitch, SwitchJournal, SwitchOutcomeUnknown
from backend.app.model_control.files import FileLock
from backend.app.model_control.operations import SwitchOperations
from backend.app.model_control.worker import SwitchWorker
from gate_candidate.gate import create_app, provision


async def test_worker_reservation_survives_gate_restart_and_preserves_other_hold(tmp_path):
    path = tmp_path / 'gate.json'
    provision(path)
    hold = 'switch-' + 'a' * 32

    def application():
        return create_app(path, 'http://localhost:8080', admin_key='admin',
                          studio_key='studio', switch_key='switch')

    async def exercise(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8081') as client:
            class LLM:
                async def gate(self, method):
                    response = await client.request(method, '/control', headers={'Authorization': 'Bearer switch'})
                    response.raise_for_status()
                    return response.json()

            adapter = SwitchGate('http://127.0.0.1:8081/control', 'switch', LLM(), transport=transport)
            assert (await client.delete('/control/holds/startup', headers={'Authorization': 'Bearer admin'})).status_code == 200
            assert (await client.put('/control', headers={'Authorization': 'Bearer admin'}, json={'paused': True})).status_code == 200
            await adapter.ensure_available()
            await adapter.acquire(hold)
            epoch = await adapter.reserve_recovery(hold)
            response = await client.put('/control/holds/studio-' + 'b' * 32,
                                        headers={'Authorization': 'Bearer studio'})
            assert response.status_code == 409
            return epoch

    app = application()
    async with app.router.lifespan_context(app):
        old_epoch = await exercise(app)

    app = application()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8081') as client:
            class LLM:
                async def gate(self, method):
                    response = await client.request(method, '/control', headers={'Authorization': 'Bearer switch'})
                    response.raise_for_status()
                    return response.json()

            adapter = SwitchGate('http://127.0.0.1:8081/control', 'switch', LLM(), transport=transport)
            with pytest.raises(SwitchAdmissionBlocked):
                await adapter.ensure_available()
            state = await LLM().gate('GET')
            assert state['epoch'] != old_epoch
            assert state['recovery_exclusive_hold'] == hold
            assert set(state['holds']) == {'startup', 'legacy', hold}
            with pytest.raises(RuntimeError):
                await adapter.verify_recovery_reservation(hold, old_epoch)
            holds = await adapter.verify_recovery_reservation(hold, state['epoch'])
            await adapter.release_recovery_reservation(hold, state['epoch'], holds,
                                                       required_independent_hold='legacy')
            state = await LLM().gate('GET')
            assert set(state['holds']) == {'startup', 'legacy'}
            assert state['recovery_exclusive_hold'] is None


@pytest.mark.parametrize('failure', ['restart', 'release_response'])
async def test_worker_recovers_unknown_restart_against_gate_candidate(tmp_path, monkeypatch, failure):
    """A lost restart result remains recorded and is recovered after worker restart."""
    monkeypatch.setattr('backend.app.model_control.coordinator.protected_xattrs', lambda _: {})
    path = tmp_path / 'gate.json'
    provision(path)
    journal_path = tmp_path / 'switch.json'
    journal_path.write_text('{}')
    app = create_app(path, 'http://localhost:8080', admin_key='admin',
                     studio_key='studio', switch_key='switch')

    class Service:
        model = 'old'
        async def installed(self): return ['old', 'new']
        async def current(self): return self.model
        async def ensure_settled(self): pass
        async def healthy(self, model): assert self.model == model
        async def apply(self, model):
            self.model = model
            if model == 'new' and failure == 'restart': raise SwitchOutcomeUnknown('restart result lost')

    async with app.router.lifespan_context(app):
        class LoseReleaseResponse(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.ASGITransport(app=app)
                self.lose = failure == 'release_response'
            async def handle_async_request(self, request):
                response = await self.inner.handle_async_request(request)
                if self.lose and request.url.path.endswith('/release-recovery-reservation'):
                    self.lose = False
                    await response.aclose()
                    raise httpx.ReadError('release response lost', request=request)
                return response
        transport = LoseReleaseResponse()
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8081') as client:
            assert (await client.delete('/control/holds/startup', headers={'Authorization': 'Bearer admin'})).status_code == 200
            assert (await client.put('/control', headers={'Authorization': 'Bearer admin'}, json={'paused': True})).status_code == 200

            class LLM:
                async def gate(self, method):
                    response = await client.request(method, '/control', headers={'Authorization': 'Bearer switch'})
                    response.raise_for_status()
                    return response.json()
                async def drain(self): pass
                async def service_state(self): return {'ActiveState': 'active'}

            service, llm = Service(), LLM()
            gate = SwitchGate('http://127.0.0.1:8081/control', 'switch', llm, transport=transport)
            store = SwitchOperations(tmp_path / 'operations.db')
            journal = SwitchJournal(journal_path)
            switch = ModelSwitch(journal, asyncio.Lock(), gate, service, preflight=lambda: None)
            worker = SwitchWorker(store, switch, FileLock(tmp_path / 'worker.lock'), verify_studio=lambda: None)
            worker.startup()
            operation, _ = await worker.submit('admin', 'new', 'key')
            result = await worker.run('admin', operation['id'])
            assert result['state'] == 'recovery_required' and service.model == 'new'
            hold = store.hold_for(operation['id'])
            if failure == 'restart':
                assert journal.read()['hold_id'] == hold
            else:
                assert journal.read() == {}
            assert set((await llm.gate('GET'))['holds']) == {'legacy', hold}
            worker.close()

            restarted = SwitchWorker(store, switch, FileLock(tmp_path / 'worker.lock'), verify_studio=lambda: None)
            restarted.startup()
            if failure == 'restart':
                result = await restarted.recover('admin', operation['id'])
                assert result['state'] == 'failed_rolled_back' and service.model == 'old'
            else:
                result = await restarted.resolve_verified('admin', operation['id'])
                assert result['state'] == 'failed' and service.model == 'new'
            assert journal.read() == {}
            assert (await llm.gate('GET'))['holds'] == ['legacy']
            restarted.close()
