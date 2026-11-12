"""A real capability probe must never certify a mismatched or partial result."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import probe_model_capabilities as probe


def make_transport(*, fail=None):
    snapshots = 0
    def reply(request):
        nonlocal snapshots
        path = request.url.path
        if path == '/model-snapshot':
            snapshots += 1
            epoch = ('b' if fail == 'epoch' and snapshots > 1 else 'a') * 32
            model_id = '/other.gguf' if fail == 'model' and snapshots > 1 else '/model.gguf'
            return httpx.Response(200, json={'ready': True, 'epoch': epoch, 'context_tokens': 4096,
                                             'models': {'data': [{'id': model_id, 'created': snapshots}]}})
        assert request.headers['x-model-epoch'] == 'a' * 32
        if path.endswith('/input_tokens'):
            return httpx.Response(200, json={'input_tokens': 20})
        payload = json.loads(request.content)
        name = payload.get('response_format', {}).get('json_schema', {}).get('name', 'plain')
        content = {
            'plain': 'Blue.',
            'structured_image_intent': json.dumps({'intent': 'image_generation', 'subject': 'blue umbrella',
                'environment': 'white background', 'style': None, 'lighting': None, 'composition': None,
                'required_elements': [], 'forbidden_elements': [], 'exact_text': [], 'preferred_model': None,
                'aspect_ratio': None}),
            'image_prompt_text': json.dumps({'model_id': 'z-image-nsfw-v2', 'prompt': 'blue sky',
                                              'negative_prompt': '', 'recommendation_reason': 'simple'}),
            'wiki_memory': json.dumps({'action': 'none', 'kind': 'uncertain', 'reason': 'insufficient_context',
                                       'source_text': '', 'source_message_id': None, 'title': '',
                                       'document_id': None, 'old_text': None}),
        }[name]
        message = {'content': content}
        if fail == 'reasoning': message['reasoning_content'] = 'unexpected'
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 21 if fail == 'count' else 20}})
    return httpx.MockTransport(reply)


@pytest.mark.parametrize('fail', [None, 'epoch', 'model', 'reasoning', 'count'])
def test_probe_only_produces_supported_record_after_all_checks(monkeypatch, fail):
    evidence = {'model_id': '/model.gguf', 'capabilities': {'reasoning_control': 'unknown', 'json_schema': 'unknown'}}
    monkeypatch.setattr(probe, 'observe', lambda _registry: ('model', json.loads(json.dumps(evidence))))
    profile = {'name': 'model', 'model_id': '/model.gguf'}
    with httpx.Client(transport=make_transport(fail=fail)) as client:
        if fail:
            with pytest.raises(RuntimeError): probe.probe(client, object(), profile)
        else:
            name, context, result = probe.probe(client, object(), profile)
            assert name == 'model' and context == 4096
            assert result['capabilities'] == {'reasoning_control': 'supported', 'json_schema': 'supported'}
