"""Frozen Studio contract excerpts for AST-isolated tests, not a runnable service.

These fixtures do not verify the currently installed external Studio release.
"""
def create_app(settings=None, engine=None, llm=None):

    @app.get('/internal/model-switch/coordination', include_in_schema=False)
    async def coordination(authorization: str | None=Header(default=None)):
        key = settings.coordination_key
        supplied = authorization.removeprefix('Bearer ') if authorization else ''
        if not key or not authorization or (not authorization.startswith('Bearer ')) or (not hmac.compare_digest(supplied.encode(), key.encode())):
            return JSONResponse({'code': 'invalid_credential'}, status_code=401)
        s = app.state
        enabled = s.gpu.model_switch_coordination_enabled()
        if enabled:
            try:
                enabled = await s.gpu.llm.owned_control_identity()
            except Exception:
                enabled = False
        transition_clear = not s.gpu.blocked and (not s.gpu.leased) and (not s.db.get_state('transition', {}))
        return {'protocol': 1, 'enabled': bool(enabled), 'transition_clear': bool(transition_clear)}

    @app.get('/internal/model-switch/recovery', include_in_schema=False)
    async def switch_recovery(authorization: str | None=Header(default=None)):
        key = settings.coordination_key
        supplied = authorization.removeprefix('Bearer ') if authorization else ''
        if not key or not authorization or (not authorization.startswith('Bearer ')) or (not hmac.compare_digest(supplied.encode(), key.encode())):
            return JSONResponse({'code': 'invalid_credential'}, status_code=401)
        s = app.state
        enabled = s.gpu.model_switch_coordination_enabled()
        gate_verified = False
        if enabled:
            try:
                gate_verified = await s.gpu.llm.owned_control_identity()
            except Exception:
                pass
        state = None
        if enabled:
            try:
                state = s.gpu.switch_barrier.read()
            except Exception:
                pass
        gpu_blocked = state is not None and s.gpu.shared_lock_held_by_root()
        digest = ''
        if state is not None:
            encoded = json.dumps(state, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
            digest = hashlib.sha256(encoded).hexdigest()
        clear = not s.gpu.blocked and (not s.gpu.leased) and (not s.db.get_state('transition', {})) and (not s.db.get_state('recovery', {}))
        return {'protocol': 1, 'enabled': bool(enabled), 'gpu_blocked': gpu_blocked, 'studio_recovery_clear': bool(clear), 'gate_verified': bool(gate_verified), 'journal_sha256': digest}
