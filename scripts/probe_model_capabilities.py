#!/usr/bin/env python3
"""Probe the loaded model through the coordinated gate; never switch or install.

Outputs a private candidate record only after all fixed Client request forms
pass. A successful sample is evidence for these forms, not a general guarantee.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
from backend.app.image_intent import INTENT_INSTRUCTION, parse_intent, response_format as intent_format
from backend.app.image_prompt import apply_balanced_preset, text_response_format
from backend.app.model_control.client import ModelControlClient
from backend.app.model_control.registry import ModelRegistry
from backend.app.wiki_memory import MemoryDecision
from draft_model_capabilities import observe

GATE = 'http://127.0.0.1:8081'
SOCKET = '/run/bc250-model-control/control.sock'


def cases():
    image_source = 'Draw one blue umbrella on a white background.'
    memory_schema = {'type': 'json_schema', 'json_schema': {'name': 'wiki_memory',
                     'strict': True, 'schema': MemoryDecision.model_json_schema()}}
    return [
        ('plain', 'Respond briefly: what color is the daytime sky?', None, None),
        ('intent', image_source, intent_format(),
         lambda content: parse_intent(content, image_source)),
        ('image_prompt', 'Return JSON: model_id z-image-nsfw-v2, prompt blue sky, '
         'negative_prompt empty, recommendation_reason simple.',
         text_response_format(['z-image-nsfw-v2']),
         lambda content: apply_balanced_preset(content, ['z-image-nsfw-v2'])),
        ('memory_schema', 'Return JSON with action none, kind uncertain, reason insufficient_context, '
         'source_text empty, source_message_id null, title empty, document_id null and old_text null.', memory_schema,
         lambda content: MemoryDecision.model_validate_json(content)),
    ]


def snapshot(client):
    response = client.get(GATE + '/model-snapshot', params={'include_context': 'true'})
    response.raise_for_status()
    value = response.json()
    if (not isinstance(value, dict) or value.get('ready') is not True
            or not isinstance(value.get('epoch'), str) or len(value['epoch']) != 32
            or type(value.get('context_tokens')) is not int or value['context_tokens'] <= 0
            or not isinstance(value.get('models'), dict)
            or not isinstance(value['models'].get('data'), list) or len(value['models']['data']) != 1):
        raise RuntimeError('Model snapshot unavailable')
    return value


def run_case(client, snapshot_value, name, prompt, fmt, validator):
    model_id = snapshot_value['models']['data'][0]['id']
    messages = ([{'role': 'system', 'content': INTENT_INSTRUCTION}, {'role': 'user', 'content': prompt}]
                if name == 'intent' else [{'role': 'user', 'content': prompt}])
    payload = {'model': model_id, 'messages': messages, 'stream': False,
               'temperature': 0.0, 'max_tokens': 512,
               'chat_template_kwargs': {'enable_thinking': False}}
    if fmt is not None:
        payload['response_format'] = fmt
    headers = {'X-Model-Epoch': snapshot_value['epoch']}
    count = client.post(GATE + '/v1/chat/completions/input_tokens', json=payload, headers=headers)
    count.raise_for_status()
    tokens = count.json().get('input_tokens')
    if type(tokens) is not int or tokens < 0 or tokens + 512 + 512 > snapshot_value['context_tokens']:
        raise RuntimeError('Invalid token count or context budget')
    response = client.post(GATE + '/v1/chat/completions', json=payload, headers=headers)
    response.raise_for_status()
    result = response.json()
    choice = result['choices'][0]
    message = choice['message']
    content = message.get('content')
    if (choice.get('finish_reason') != 'stop' or not isinstance(content, str) or not content.strip()
            or message.get('reasoning_content') not in (None, '') or '<think>' in content.lower()
            or result.get('usage', {}).get('prompt_tokens') != tokens):
        raise RuntimeError('Generation or token accounting not verified')
    if validator is not None:
        validator(content)
    print('case_' + name + '=passed', flush=True)


async def worker_profile():
    result = await ModelControlClient(SOCKET).request('profile')
    if 'error' in result:
        raise RuntimeError('Worker profile unavailable')
    return result


def probe(client, registry, profile):
    name, evidence = observe(registry)
    first = snapshot(client)
    if (profile.get('name') != name or profile.get('model_id') != evidence['model_id']
            or first['models']['data'][0].get('id') != evidence['model_id']):
        raise RuntimeError('Model identity mismatch')
    for case in cases():
        run_case(client, first, *case)
    second_name, second_evidence = observe(registry)
    second = snapshot(client)
    if (name != second_name or evidence != second_evidence or first['epoch'] != second['epoch']
            or first['context_tokens'] != second['context_tokens']
            or second['models']['data'][0].get('id') != evidence['model_id']):
        raise RuntimeError('Model changed while probing')
    evidence['capabilities'] = {'reasoning_control': 'supported', 'json_schema': 'supported'}
    return name, first['context_tokens'], evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, help='new private local candidate-record path')
    args = parser.parse_args()
    try:
        registry = ModelRegistry()
        profile = asyncio.run(worker_profile())
        with httpx.Client(timeout=180, trust_env=False) as client:
            name, context, evidence = probe(client, registry, profile)
        # Check for an operation that could have started during the last sample.
        after = asyncio.run(worker_profile())
        if after != profile:
            raise RuntimeError('Worker profile changed while probing')
        fd = os.open(Path(args.report), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as file:
            json.dump(evidence, file, sort_keys=True, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
        print('model=' + name)
        print('context_tokens=' + str(context))
        print('candidate_record_created=True')
    except Exception:
        print('Model probe unavailable or failed; no record installed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
