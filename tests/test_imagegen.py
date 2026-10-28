import httpx
import pytest
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.imagegen import ImageGenerationNotFound, ImageGenerationUnavailable, ImageStudioAdapter


def configured_settings(**overrides):
    values = {
        "app_api_key": "app-key",
        "llm_gate_api_key": "gate-key",
        "imagegen_enabled": True,
        "imagegen_api_key": "studio-client-key",
    }
    values.update(overrides)
    return Settings(**values)


class FakeResponse:
    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("untrusted upstream", request=httpx.Request("GET", "http://127.0.0.1:8090"), response=httpx.Response(self.status_code))

    def json(self):
        return self.value


def studio_model(**overrides):
    from copy import deepcopy
    from backend.app.image_prompt import IMAGE_MODELS
    value = deepcopy(IMAGE_MODELS["z-image-nsfw-v2"])
    value["execution_enabled"] = True
    value.update(overrides)
    return value


def fake_studio_model():
    return {'model_id': 'fake', 'display_name': 'CPU test', 'family': 'fake', 'engine': 'fake',
            'execution_enabled': True, 'validation_status': 'tested', 'supported_resolutions': [[64, 64], [512, 512]],
            'presets': {name: {'width': 512, 'height': 512, 'steps': 1, 'cfg_scale': 1.0} for name in ('fast', 'balanced', 'quality')},
            'generation_options': {'cfg_scale': {'mode': 'verified', 'default': 1.0, 'allowed_values': [], 'minimum': 0.0, 'maximum': 30.0},
                                   'negative_prompt': {'mode': 'verified'},
                                   'sampling_method': {'mode': 'fixed', 'default': None, 'allowed_values': []},
                                   'scheduler': {'mode': 'fixed', 'default': None, 'allowed_values': []}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [False, True])
async def test_fake_engine_requires_explicit_opt_in_and_live_contract(monkeypatch, allowed):
    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, path, **_kwargs):
            return FakeResponse({'engine': 'fake'} if path.endswith('status') else [fake_studio_model(), studio_model()])
    monkeypatch.setattr('backend.app.imagegen.httpx.AsyncClient', FakeClient)
    result = await ImageStudioAdapter(configured_settings(imagegen_allow_fake=allowed)).capabilities()
    assert result['available'] is allowed
    assert result['simulated'] is allowed
    assert ('fake' in [model['model_id'] for model in result['models']]) is allowed
    assert all(not model['execution_enabled'] for model in result['models'] if model['model_id'] != 'fake')
    if allowed:
        fake = next(model for model in result['models'] if model['model_id'] == 'fake')
        assert fake['simulated'] is True and fake['presets']['balanced']['steps'] == 1


@pytest.mark.asyncio
async def test_adapter_reports_sanitized_available_capabilities(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs): captured.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, path, **kwargs):
            assert kwargs["headers"] == {"Authorization": "Bearer studio-client-key"}
            if path == "/api/v1/system/status":
                return FakeResponse({"engine": "sdcpp", "recovery_required": False, "model_path": "/private"})
            return FakeResponse([studio_model(model_path="/private", model_snapshot={"secret": "hidden"})])

    monkeypatch.setattr("backend.app.imagegen.httpx.AsyncClient", FakeClient)
    result = await ImageStudioAdapter(configured_settings()).capabilities()

    assert result["available"] is True
    assert result["model"]["model_id"] == "z-image-nsfw-v2"
    assert result["model"]["execution_enabled"] is True
    assert len(result["models"]) == 7
    assert "/private" not in str(result) and "secret" not in str(result)
    assert captured["base_url"] == "http://127.0.0.1:8090"
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False


@pytest.mark.asyncio
async def test_adapter_uses_live_presets_and_rejects_incomplete_contract(monkeypatch):
    model = studio_model(model_path="/private", lora={"enabled": True, "strength": 0.7, "trigger": "test", "path": "/private/adapter"})
    model["presets"]["balanced"].update(width=512, height=512, steps=7)

    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, path, **_kwargs):
            return FakeResponse({"engine": "sdcpp"} if path.endswith("status") else [model])

    monkeypatch.setattr("backend.app.imagegen.httpx.AsyncClient", FakeClient)
    adapter = ImageStudioAdapter(configured_settings())
    result = await adapter.capabilities()
    assert result["available"] is True
    assert result["model"]["presets"]["balanced"]["steps"] == 7
    assert "/private" not in str(result)
    assert result["model"]["server_settings"]["lora"] == {"enabled": True, "strength": 0.7, "trigger": "test"}
    del model["generation_options"]
    result = await adapter.capabilities()
    assert result["available"] is False
    assert result["model"]["contract_matches"] is False


def test_lora_toggle_requires_explicit_studio_capability_and_moss_family():
    from copy import deepcopy
    from backend.app.imagegen import _live_contract
    from backend.app.image_prompt import IMAGE_MODELS
    local = deepcopy(IMAGE_MODELS['moss-image-nsfw'])
    upstream = deepcopy(local)
    upstream['lora'] = {'enabled': True, 'strength': 0.7, 'path': '/private'}
    assert _live_contract(local, upstream)['lora_toggle_supported'] is False
    upstream['lora_toggle_supported'] = True
    result = _live_contract(local, upstream)
    assert result['lora_toggle_supported'] is True
    assert '/private' not in str(result)
    upstream['lora']['enabled'] = False
    assert _live_contract(local, upstream)['lora_toggle_supported'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "response", "reason"),
    [
        (200, {"engine": "fake", "recovery_required": False}, "simulated_engine"),
        (200, {"engine": "sdcpp", "recovery_required": True}, "recovery_required"),
    ],
)
async def test_adapter_reports_nonrunnable_studio_states(monkeypatch, status, response, reason):
    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, path, **_kwargs): return FakeResponse(response if path.endswith("status") else [studio_model()], status)

    monkeypatch.setattr("backend.app.imagegen.httpx.AsyncClient", FakeClient)
    result = await ImageStudioAdapter(configured_settings()).capabilities()

    assert result["available"] is False
    assert result["reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_adapter_hides_upstream_http_failures(monkeypatch, status):
    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, _path, **_kwargs): return FakeResponse({}, status)

    monkeypatch.setattr("backend.app.imagegen.httpx.AsyncClient", FakeClient)
    with pytest.raises(ImageGenerationUnavailable, match="imagegen_unavailable"):
        await ImageStudioAdapter(configured_settings()).capabilities()


@pytest.mark.asyncio
async def test_adapter_maps_timeout_without_exposing_upstream_details(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, _path, **_kwargs): raise httpx.TimeoutException("untrusted timeout details")

    monkeypatch.setattr("backend.app.imagegen.httpx.AsyncClient", FakeClient)
    with pytest.raises(ImageGenerationUnavailable, match="imagegen_timeout"):
        await ImageStudioAdapter(configured_settings()).capabilities()


def test_imagegen_settings_are_loopback_only_and_opt_in():
    assert configured_settings().imagegen_url == "http://127.0.0.1:8090"
    with pytest.raises(ValidationError):
        configured_settings(imagegen_url="http://192.0.2.10:8090")
    with pytest.raises(ValidationError):
        configured_settings(imagegen_api_key="")
    with pytest.raises(ValidationError):
        Settings(app_api_key="app-key", llm_gate_api_key="gate-key", imagegen_enabled=True)
    with pytest.raises(ValidationError):
        configured_settings(max_image_jobs=0)
    with pytest.raises(ValidationError):
        configured_settings(max_image_asset_bytes=0)


@pytest.mark.asyncio
async def test_adapter_sanitizes_job_status_and_bounded_asset(monkeypatch):
    captured = []

    class StreamResponse(FakeResponse):
        headers = {"content-type": "image/png", "content-length": "3"}
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def aiter_bytes(self):
            yield b"png"

    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, path, **_kwargs):
            captured.append(path)
            return FakeResponse({"job_id": "studio-job", "status": "completed", "output_asset_ids": ["asset-a", "asset-a"], "model_path": "/private"})
        def stream(self, method, path, **_kwargs):
            captured.append(f"{method} {path}")
            return StreamResponse({}, 200)

    monkeypatch.setattr("backend.app.imagegen.httpx.AsyncClient", FakeClient)
    adapter = ImageStudioAdapter(configured_settings())
    assert await adapter.get_job("studio-job") == {"upstream_job_id": "studio-job", "status": "completed", "asset_ids": ["asset-a"], "metadata": {}}
    assert await adapter.fetch_asset("asset-a", 3) == ("image/png", b"png")
    assert captured == ["/api/v1/jobs/studio-job", "GET /api/v1/assets/asset-a/file"]


def test_job_metadata_uses_item_seed_and_omits_private_model_paths():
    value = {'job_id': 'job', 'status': 'completed', 'seed': 1,
             'items': [{'index': 0, 'seed': 4242, 'status': 'completed'}],
             'started_at': '2026-01-01T00:00:00+00:00', 'completed_at': '2026-01-01T00:00:02.500000+00:00',
             'model_snapshot': {'model_path': '/secret/model', 'quantization': 'Q4_K',
                                'lora': {'enabled': True, 'strength': 0.7, 'path': '/secret/lora'}}}
    result = ImageStudioAdapter._job_value(value, 'job')
    assert result['metadata'] == {'seed': 4242, 'started_at': value['started_at'], 'completed_at': value['completed_at'],
                                  'duration_ms': 2500, 'quantization': 'Q4_K', 'lora': {'enabled': True, 'strength': 0.7}}
    assert '/secret' not in str(result)
    value.update(items=[{'index': 0, 'seed': True}], started_at='invalid', completed_at='2026-01-01T00:00:02')
    result = ImageStudioAdapter._job_value(value, 'job')
    assert 'seed' not in result['metadata'] and 'duration_ms' not in result['metadata']


@pytest.mark.asyncio
async def test_adapter_maps_missing_job_without_body_exposure(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, _path, **_kwargs): return FakeResponse({}, 404)

    monkeypatch.setattr("backend.app.imagegen.httpx.AsyncClient", FakeClient)
    with pytest.raises(ImageGenerationNotFound, match="image_job_not_found"):
        await ImageStudioAdapter(configured_settings()).get_job("missing")


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', [False, True])
async def test_moss_availability_is_independent_of_z_image_and_contract_checked(monkeypatch, mismatch):
    from copy import deepcopy
    from backend.app.image_prompt import IMAGE_MODELS
    moss = deepcopy(IMAGE_MODELS['moss-image-nsfw'])
    moss['execution_enabled'] = True
    if mismatch:
        moss['generation_options']['cfg_scale']['allowed_values'] = [4.0]

    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, path, **_kwargs):
            return FakeResponse({'engine': 'sdcpp'} if path.endswith('status') else [moss])

    monkeypatch.setattr('backend.app.imagegen.httpx.AsyncClient', FakeClient)
    result = await ImageStudioAdapter(configured_settings()).capabilities()
    assert result['available'] is (not mismatch)
    assert result['model']['execution_enabled'] is False
    model = next(item for item in result['models'] if item['model_id'] == 'moss-image-nsfw')
    assert model['execution_enabled'] is (not mismatch)


@pytest.mark.asyncio
async def test_submission_rejects_nonrunnable_selected_model_even_if_another_is_available(monkeypatch):
    from backend.app.imagegen import ImageGenerationRejected
    adapter = ImageStudioAdapter(configured_settings())
    async def capabilities():
        return {'available': True, 'models': [
            {'model_id': 'moss-image-nsfw', 'execution_enabled': False},
            {'model_id': 'z-image-nsfw-v2', 'execution_enabled': True},
        ]}
    monkeypatch.setattr(adapter, 'capabilities', capabilities)
    with pytest.raises(ImageGenerationRejected):
        await adapter.submit_job({'model_id': 'moss-image-nsfw'})
