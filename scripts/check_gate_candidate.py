#!/usr/bin/env python3
"""Exercise a staged gate venv with isolated state and a fake upstream."""
import asyncio
import tempfile
from pathlib import Path

import httpx
from gate_candidate.gate import create_app, provision


async def run():
    with tempfile.TemporaryDirectory(prefix='bc250-gate-check-') as directory:
        state = Path(directory) / 'gate.json'
        provision(state, legacy_paused=True)
        def upstream(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [{'id': 'model-a', 'meta': {'n_ctx': 4096}}]})
            if request.url.path == '/slots':
                return httpx.Response(200, json=[{'n_ctx': 4096}])
            raise AssertionError('Unexpected upstream request')
        app = create_app(state, 'http://fake', admin_key='admin', client_key='client',
                         studio_key='studio', switch_key='switch', transport=httpx.MockTransport(upstream))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gate') as client:
                for role in ('admin', 'client', 'studio', 'switch'):
                    result = await client.get('/control/identity', headers={'Authorization': 'Bearer ' + role})
                    assert result.json() == {'protocol': 3, 'role': role}
                admin = {'Authorization': 'Bearer admin'}
                status = (await client.get('/control', headers=admin)).json()
                assert status['holds'] == ['legacy', 'startup']
                assert (await client.delete('/control/holds/startup', headers=admin)).status_code == 200
                status = (await client.get('/control', headers=admin)).json()
                assert status['holds'] == ['legacy'] and status['paused']
                assert (await client.put('/control', headers=admin, json={'paused': False})).status_code == 200
                snapshot = await client.get('/model-snapshot?include_context=true')
                assert snapshot.status_code == 200 and snapshot.json()['context_tokens'] == 4096
    print('isolated_gate_roles_holds_and_snapshot=True')


if __name__ == '__main__':
    asyncio.run(run())
