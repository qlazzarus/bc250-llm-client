"""Optional in-process integration against a local AI Studio checkout, using its fake CPU engine."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest


def test_studio_fake_queue_end_to_end(monkeypatch, tmp_path):
    studio_path = os.environ.get('BC250_STUDIO_SOURCE')
    if not studio_path or not (Path(studio_path) / 'backend/app/main.py').is_file():
        pytest.skip('Set BC250_STUDIO_SOURCE to an AI Studio checkout and use its Python environment')
    pytest.importorskip('sqlalchemy')
    monkeypatch.syspath_prepend(str(Path(studio_path) / 'backend'))
    from app.core.config import Settings as StudioSettings
    from app.main import create_app as create_studio
    from fastapi.testclient import TestClient

    monkeypatch.setenv('APP_API_KEY', 'client-key')
    monkeypatch.setenv('LLM_GATE_API_KEY', 'gate-key')
    from backend.app import main
    from backend.app.imagegen import ImageStudioAdapter

    studio_key = 'studio-client-key-for-integration'
    studio_settings = StudioSettings(data_dir=tmp_path / 'studio', registry_path=Path(studio_path) / 'config/models.yaml',
                                    api_key=studio_key, admin_key='studio-admin-key-for-integration', engine='fake',
                                    policy='image_priority', poll_interval=0.01, fake_delay=0.01)
    client_settings = main.get_settings()
    monkeypatch.setattr(client_settings, 'app_api_key', 'client-key')
    monkeypatch.setattr(client_settings, 'database_path', str(tmp_path / 'client.db'))
    monkeypatch.setattr(client_settings, 'imagegen_enabled', True)
    monkeypatch.setattr(client_settings, 'imagegen_allow_fake', True)
    monkeypatch.setattr(client_settings, 'imagegen_api_key', studio_key)
    monkeypatch.setattr(client_settings, 'image_job_poll_min_interval_seconds', 0)

    class Bridge:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, path, **kwargs): return await asyncio.to_thread(studio.get, path, **kwargs)
        async def post(self, path, **kwargs): return await asyncio.to_thread(studio.post, path, **kwargs)
        def stream(self, _method, path, **kwargs):
            class Stream:
                async def __aenter__(self):
                    self.response = await asyncio.to_thread(studio.get, path, **kwargs)
                    self.headers = self.response.headers
                    self.status_code = self.response.status_code
                    return self
                async def __aexit__(self, *_args): pass
                def raise_for_status(self): self.response.raise_for_status()
                async def aiter_bytes(self): yield self.response.content
            return Stream()

    monkeypatch.setattr(ImageStudioAdapter, '_client', lambda _self: Bridge())
    with TestClient(create_studio(studio_settings)) as studio, TestClient(main.app) as client:
        headers = {'Authorization': 'Bearer client-key'}
        caps = client.get('/api/v1/image-generation/capabilities', headers=headers).json()
        assert caps['available'] and caps['simulated']
        model = next(item for item in caps['models'] if item['model_id'] == 'fake')
        preset = model['presets']['balanced']
        cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
        response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers,
                               json={'content': 'CPU integration test', 'prompt_mode': 'original', 'image_model_id': 'fake'})
        assert response.status_code == 200, response.text
        mid = response.json()['assistant_message']['id']
        draft = client.put(f'/api/v1/conversations/{cid}/messages/{mid}/image-draft', headers=headers,
                           json={'prompt': 'CPU integration test', 'width': preset['width'], 'height': preset['height'], 'revision': 0, 'preset_name': 'balanced'})
        assert draft.status_code == 200, draft.text
        job = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json={
            'source_message_id': mid, 'prompt': 'CPU integration test', 'width': preset['width'], 'height': preset['height'],
            'draft_revision': 1, 'request_id': 'ffffffff-ffff-4fff-8fff-ffffffffffff'})
        assert job.status_code == 202, job.text
        job_id = job.json()['data']['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(f'/api/v1/image-jobs/{job_id}', headers=headers).json()['data']
            if current['status'] in {'completed', 'failed', 'interrupted'}:
                break
            time.sleep(0.05)
        assert current['status'] == 'completed', current
        assert current['studio_metadata']['seed'] >= 0
        asset = client.get(f"/api/v1/image-jobs/{job_id}/assets/{current['asset_ids'][0]}/file", headers=headers)
        assert asset.status_code == 200 and asset.content.startswith(b'\x89PNG\r\n\x1a\n')
        assert client.get(f'/api/v1/image-jobs/{job_id}/comparison-record', headers=headers).json()['simulated'] is True

        gate_calls = []
        intent = {'intent': 'image_generation', 'subject': 'red umbrella', 'environment': 'Seoul', 'style': None,
                  'lighting': None, 'composition': None, 'required_elements': ['red umbrella'], 'forbidden_elements': [],
                  'exact_text': ['OPEN'], 'preferred_model': None, 'aspect_ratio': None}
        class GateResponse:
            def __init__(self, value): self.value = value
            def raise_for_status(self): pass
            def json(self): return self.value
        class GateClient:
            def __init__(self, **_kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): pass
            async def get(self, *_args, **_kwargs): return GateResponse({'data': [{'id': 'test-llm'}]})
            async def post(self, *_args, **kwargs):
                payload = kwargs['json']
                name = payload['response_format']['json_schema']['name']
                gate_calls.append(name)
                output = intent if name == 'structured_image_intent' else {
                    'model_id': 'fake', 'prompt': 'A red umbrella in Seoul by an OPEN sign',
                    'negative_prompt': '', 'recommendation_reason': 'CPU 테스트'}
                return GateResponse({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]})
        monkeypatch.setattr(main.httpx, 'AsyncClient', GateClient)
        optimized = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers,
                                json={'content': '서울의 빨간 우산과 OPEN 간판', 'prompt_mode': 'ai_optimized', 'image_model_id': 'fake'})
        assert optimized.status_code == 200, optimized.text
        assert gate_calls == ['structured_image_intent', 'image_prompt_text']
        optimized_mid = optimized.json()['assistant_message']['id']
        optimized_prompt = json.loads(optimized.json()['assistant_message']['content'])['prompt']
        draft = client.put(f'/api/v1/conversations/{cid}/messages/{optimized_mid}/image-draft', headers=headers,
                           json={'prompt': optimized_prompt, 'width': preset['width'], 'height': preset['height'], 'revision': 0, 'preset_name': 'balanced'})
        assert draft.status_code == 200, draft.text
        job = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json={
            'source_message_id': optimized_mid, 'prompt': optimized_prompt, 'width': preset['width'], 'height': preset['height'],
            'draft_revision': 1, 'request_id': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'})
        assert job.status_code == 202, job.text
        job_id = job.json()['data']['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(f'/api/v1/image-jobs/{job_id}', headers=headers).json()['data']
            if current['status'] in {'completed', 'failed', 'interrupted'}: break
            time.sleep(0.05)
        assert current['status'] == 'completed', current
        record = client.get(f'/api/v1/image-jobs/{job_id}/comparison-record', headers=headers).json()
        assert record['simulated'] and record['prompt_mode'] == 'ai_optimized'
        assert record['original_request'] == '서울의 빨간 우산과 OPEN 간판'
