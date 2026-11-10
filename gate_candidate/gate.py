"""Local protocol 3 gate candidate. Provision state offline; run one process only."""
import asyncio
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

ID = re.compile(r'(?:startup|legacy|admin-[A-Za-z0-9_.:-]{1,128}|(?:client|studio|switch)-[0-9a-f]{32})\Z')
SWITCH = re.compile(r'switch-[0-9a-f]{32}\Z')
INDEPENDENT = re.compile(r'(?:startup|legacy|admin-[A-Za-z0-9_.:-]{1,128})\Z')
HEX = re.compile(r'[0-9a-f]{32}\Z')
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length', 'x-model-epoch', 'x-bc250-admission'}


def provision(path, *, legacy_paused=False):
    """Create startup-held state exclusively, optionally preserving legacy pause."""
    if type(legacy_paused) is not bool:
        raise ValueError('Invalid legacy pause')
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            holds = ['legacy', 'startup'] if legacy_paused else ['startup']
            json.dump({'holds': holds, 'epoch': secrets.token_hex(16), 'recovery_exclusive_hold': None, 'deployment_generation': None}, f)
            f.flush(); os.fsync(f.fileno())
        d = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(d)
        finally: os.close(d)
    except BaseException:
        # A partial file remains as evidence and will fail closed.
        raise


class Admission:
    def __init__(self, path):
        self.path = Path(path)
        self.marker = Path(str(self.path) + '.uncertain')
        self.lock_fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.marker.exists(): raise RuntimeError('Unresolved gate state write')
            state = json.loads(self.path.read_text())
            if (not isinstance(state, dict) or set(state) != {'holds', 'epoch', 'recovery_exclusive_hold', 'deployment_generation'}
                or not isinstance(state['holds'], list) or any(not isinstance(h, str) or not ID.fullmatch(h) for h in state['holds'])
                or len(set(state['holds'])) != len(state['holds']) or not isinstance(state['epoch'], str) or not HEX.fullmatch(state['epoch'])
                or (state['deployment_generation'] is not None and (not isinstance(state['deployment_generation'], str) or not HEX.fullmatch(state['deployment_generation']) or 'admin-deployment' not in state['holds']))
                or (state['recovery_exclusive_hold'] is not None and (not isinstance(state['recovery_exclusive_hold'], str)
                    or not SWITCH.fullmatch(state['recovery_exclusive_hold']) or state['recovery_exclusive_hold'] not in state['holds']))):
                raise ValueError('Invalid durable gate state')
            self.holds = set(state['holds']); self.holds.add('startup'); self.reservation = state['recovery_exclusive_hold']; self.generation = state['deployment_generation']
            self.epoch = secrets.token_hex(16); self.failed = False; self.active = 0
            self.condition = asyncio.Condition()
            self._persist(self.holds, self.reservation, self.epoch, self.generation)
        except BaseException:
            os.close(self.lock_fd); raise

    def close(self):
        os.close(self.lock_fd)

    def _persist(self, holds, reservation, epoch, generation):
        tmp = None
        try:
            marker_fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try: os.fsync(marker_fd)
            finally: os.close(marker_fd)
            d = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(d)
            finally: os.close(d)
            fd, tmp = tempfile.mkstemp(prefix='.gate-', dir=self.path.parent)
            with os.fdopen(fd, 'w') as f:
                json.dump({'holds': sorted(holds), 'epoch': epoch, 'recovery_exclusive_hold': reservation, 'deployment_generation': generation}, f)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path); tmp = None
            d = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(d)
            finally: os.close(d)
            os.unlink(self.marker)
            d = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(d)
            finally: os.close(d)
        except BaseException:
            self.failed = True
            raise
        finally:
            if tmp is not None:
                try: os.unlink(tmp)
                except FileNotFoundError: pass

    def _check(self):
        if self.failed: raise HTTPException(503, 'Gate state uncertain; restart and investigate')

    def _state(self):
        return {'protocol': 3, 'durable': True, 'epoch': self.epoch, 'holds': sorted(self.holds),
                'paused': bool(self.holds), 'recovery_exclusive_hold': self.reservation,
                'active_requests': self.active, 'waiting_requests': 0, 'deployment_generation': self.generation}

    async def status(self):
        async with self.condition:
            self._check(); return self._state()

    async def enter(self, epoch):
        async with self.condition:
            self._check()
            if self.holds: raise HTTPException(409, 'model_switch_in_progress')
            if not epoch or not hmac.compare_digest(epoch, self.epoch): raise HTTPException(409, 'model_epoch_mismatch')
            self.active += 1

    async def leave(self):
        async with self.condition:
            self.active -= 1; self.condition.notify_all()

    async def close_uncertain(self):
        """Do not report a drained gate when an upstream close failed."""
        async with self.condition:
            self.failed = True
            self.condition.notify_all()

    async def mutate(self, operation, hold, body=None, *, exclusive=False, expected_generation=None):
        async with self.condition:
            self._check(); body = body or {}; holds = self.holds.copy(); reservation = self.reservation; epoch = self.epoch; generation = self.generation
            if operation == 'acquire':
                if exclusive:
                    if hold in holds: raise HTTPException(409, 'Hold already exists')
                    generation = secrets.token_hex(16)
                if reservation and hold != reservation and (hold.startswith(('client-', 'switch-', 'studio-'))): raise HTTPException(409, 'Recovery reserved')
                if hold not in holds and hold.startswith(('switch-', 'studio-')): epoch = secrets.token_hex(16)
                holds.add(hold)
            elif operation == 'release':
                if hold == 'admin-deployment' and (generation is not None or expected_generation is not None):
                    if generation is None or expected_generation != generation: raise HTTPException(412, 'Deployment generation changed')
                    generation = None
                if reservation == hold: raise HTTPException(409, 'Recovery reservation requires conditional release')
                holds.discard(hold)
            elif operation == 'reserve':
                if hold not in holds or reservation not in (None, hold) or any(h != hold and not INDEPENDENT.fullmatch(h) for h in holds): raise HTTPException(409, 'Conflicting hold')
                reservation = hold
            elif operation in ('conditional', 'release_reservation'):
                expected_keys = {'expected_epoch', 'required_independent_hold'} if operation == 'conditional' else {'expected_epoch', 'expected_holds'}
                if operation == 'release_reservation' and 'required_independent_hold' in body: expected_keys.add('required_independent_hold')
                independent = body.get('required_independent_hold')
                if (set(body) != expected_keys or body.get('expected_epoch') != epoch or hold not in holds
                    or (independent is not None and (not isinstance(independent, str) or not INDEPENDENT.fullmatch(independent) or independent not in holds))
                    or (operation == 'conditional' and (independent is None or reservation is not None))
                    or (operation == 'release_reservation' and (reservation != hold or not isinstance(body.get('expected_holds'), list)
                        or any(not isinstance(h, str) for h in body['expected_holds']) or len(set(body['expected_holds'])) != len(body['expected_holds']) or set(body['expected_holds']) != holds))):
                    raise HTTPException(409, 'State changed')
                holds.remove(hold)
                if operation == 'release_reservation': reservation = None
            else: raise ValueError('Unknown operation')
            self._persist(holds, reservation, epoch, generation)
            self.holds, self.reservation, self.epoch, self.generation = holds, reservation, epoch, generation
            result = dict(self._state(), held=hold in holds)
            if exclusive: result.update(created=True, generation=generation)
            return result


class UpstreamResponse(StreamingResponse):
    """Keep the admission slot until the upstream stream is closed."""
    def __init__(self, upstream, admission):
        self.upstream = upstream
        self.admission = admission
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code,
                         headers={k: v for k, v in upstream.headers.items() if k.lower() not in HOP})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            async def cleanup():
                try:
                    await self.upstream.aclose()
                except BaseException:
                    await self.admission.close_uncertain()
                    raise
                await self.admission.leave()
            task = asyncio.create_task(cleanup())
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            # Retrieve any cleanup error, including when the task completed between
            # the loop condition and shield.  Repeated caller cancellation cannot
            # release admission while the upstream connection remains open.
            task.result()
            if cancelled:
                raise asyncio.CancelledError


def create_app(state_path, upstream_url, *, admin_key, studio_key, switch_key, client_key=None, upstream_api_key=None, transport=None):
    if upstream_api_key is not None and (not isinstance(upstream_api_key, str) or not upstream_api_key): raise ValueError('Invalid upstream credential')
    keys = {'admin': admin_key, 'studio': studio_key, 'switch': switch_key}
    if client_key is not None: keys['client'] = client_key
    if any(not isinstance(k, str) or not k for k in keys.values()) or len(set(keys.values())) != len(keys): raise ValueError('Distinct control keys required')
    if upstream_api_key is not None and upstream_api_key in keys.values(): raise ValueError('Upstream credential must differ from control keys')
    @asynccontextmanager
    async def lifespan(app):
        app.state.gate = Admission(state_path)
        async with httpx.AsyncClient(transport=transport, timeout=120, trust_env=False) as client:
            app.state.client = client
            try: yield
            finally: app.state.gate.close()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    def auth(header):
        if header and header.startswith('Bearer '):
            for role, key in keys.items():
                if hmac.compare_digest(header[7:].encode(), key.encode()): return role
        raise HTTPException(401, 'Invalid credential')
    def own(role, hold):
        if not ID.fullmatch(hold) or not ((role == 'admin' and (hold == 'startup' or hold.startswith('admin-'))) or (role in ('client', 'studio', 'switch') and re.fullmatch(role + r'-[0-9a-f]{32}', hold))): raise HTTPException(403, 'Hold belongs to another controller')
    @app.get('/control')
    async def status(authorization: str | None = Header(None)):
        auth(authorization); return await app.state.gate.status()
    @app.get('/control/identity')
    async def identity(authorization: str | None = Header(None)):
        return {'protocol': 3, 'role': auth(authorization)}
    @app.put('/control')
    async def legacy(body: dict, authorization: str | None = Header(None)):
        if auth(authorization) != 'admin': raise HTTPException(403)
        if set(body) != {'paused'} or type(body['paused']) is not bool: raise HTTPException(422)
        return await app.state.gate.mutate('acquire' if body['paused'] else 'release', 'legacy')
    @app.api_route('/control/holds/{hold}', methods=['PUT', 'DELETE'])
    async def change(hold: str, request: Request, authorization: str | None = Header(None)):
        role = auth(authorization); own(role, hold)
        none_match = request.headers.get('if-none-match'); match = request.headers.get('if-match')
        if none_match is not None and not (role == 'admin' and hold == 'admin-deployment' and request.method == 'PUT' and none_match == '*'): raise HTTPException(400, 'Unsupported precondition')
        if match is not None and not (role == 'admin' and hold == 'admin-deployment' and request.method == 'DELETE' and HEX.fullmatch(match)): raise HTTPException(400, 'Unsupported precondition')
        return await app.state.gate.mutate('acquire' if request.method == 'PUT' else 'release', hold, exclusive=none_match == '*', expected_generation=match)
    @app.put('/control/holds/{hold}/recovery-reservation')
    async def reserve(hold: str, authorization: str | None = Header(None)):
        role = auth(authorization); own(role, hold)
        if role != 'switch': raise HTTPException(403)
        return await app.state.gate.mutate('reserve', hold)
    @app.post('/control/holds/{hold}/{action}')
    async def conditional(hold: str, action: str, body: dict, authorization: str | None = Header(None)):
        role = auth(authorization); own(role, hold)
        if role != 'switch' or action not in ('release-if-independent', 'release-recovery-reservation'): raise HTTPException(403)
        return await app.state.gate.mutate('conditional' if action == 'release-if-independent' else 'release_reservation', hold, body)
    @app.get('/model-snapshot')
    async def snapshot(include_context: bool = False):
        before = await app.state.gate.status()
        if before['paused']: return JSONResponse({'ready': False, 'epoch': before['epoch']}, status_code=409)
        try: upstream = await app.state.client.get(upstream_url.rstrip('/') + '/v1/models', headers={'Authorization': 'Bearer ' + upstream_api_key} if upstream_api_key else {})
        except httpx.HTTPError: raise HTTPException(503, 'Upstream unavailable')
        context_tokens = None
        if include_context:
            try:
                slots_response = await app.state.client.get(upstream_url.rstrip('/') + '/slots',
                    headers={'Authorization': 'Bearer ' + upstream_api_key} if upstream_api_key else {})
                slots_response.raise_for_status()
                slots = slots_response.json()
                if (not isinstance(slots, list) or not slots
                        or any(not isinstance(slot, dict) or type(slot.get('n_ctx')) is not int
                               or slot['n_ctx'] <= 0 for slot in slots)):
                    raise ValueError('Invalid slot context')
                context_tokens = min(slot['n_ctx'] for slot in slots)
            except (httpx.HTTPError, ValueError):
                raise HTTPException(503, 'Slot context unavailable')
        after = await app.state.gate.status()
        if after['paused'] or after['epoch'] != before['epoch']: raise HTTPException(409, 'model_epoch_mismatch')
        if upstream.status_code != 200: raise HTTPException(503, 'Upstream unavailable')
        try: models = upstream.json()
        except ValueError: raise HTTPException(503, 'Invalid upstream models')
        if (not isinstance(models, dict) or not isinstance(models.get('data'), list) or not models['data']
            or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) or not m['id'].strip() for m in models['data'])):
            raise HTTPException(503, 'Invalid upstream models')
        result = {'ready': True, 'epoch': after['epoch'], 'models': models}
        if include_context:
            # This worker controls one loaded model; never attach slots to an
            # ambiguous multi-model list or use the training context as a limit.
            if len(models['data']) != 1:
                raise HTTPException(503, 'Ambiguous loaded model')
            meta = models['data'][0].get('meta')
            configured = meta.get('n_ctx') if isinstance(meta, dict) else None
            if type(configured) is not int or configured <= 0:
                raise HTTPException(503, 'Loaded model context unavailable')
            result['context_tokens'] = min(configured, context_tokens)
        return result
    @app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
    async def proxy(path: str, request: Request, x_model_epoch: str | None = Header(None)):
        if path == 'control' or path.startswith('control/'): raise HTTPException(404)
        try:
            await app.state.gate.enter(x_model_epoch)
        except HTTPException as exc:
            if exc.status_code == 409:
                # This response is generated before any upstream request. The
                # header is stripped from upstream responses and requests.
                return JSONResponse({'detail': exc.detail}, status_code=409,
                                    headers={'X-BC250-Admission': 'rejected'})
            raise
        forwarding = False
        try:
            headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
            authorization = headers.get('authorization', '')
            if authorization.startswith('Bearer ') and any(hmac.compare_digest(authorization[7:].encode(), key.encode()) for key in keys.values()):
                headers.pop('authorization', None)
            if upstream_api_key is not None: headers['authorization'] = 'Bearer ' + upstream_api_key
            upstream_request = app.state.client.build_request(
                request.method, upstream_url.rstrip('/') + '/' + path,
                params=request.query_params, headers=headers, content=await request.body())
            # A timeout or cancellation after sending starts does not prove that
            # the upstream stopped processing the request.
            forwarding = True
            upstream = await app.state.client.send(upstream_request, stream=True)
            return UpstreamResponse(upstream, app.state.gate)
        except httpx.HTTPError:
            if forwarding:
                await app.state.gate.close_uncertain()
            else:
                await app.state.gate.leave()
            return Response(status_code=503, content=b'LLM upstream unavailable')
        except BaseException:
            if forwarding:
                await app.state.gate.close_uncertain()
            else:
                await app.state.gate.leave()
            raise
    return app
