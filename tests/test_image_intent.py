import json

import pytest

from backend.app.image_intent import parse_intent, response_format


def scene(**changes):
    return {
        'intent': 'image_generation', 'subject': 'two adults', 'environment': 'Seoul',
        'style': None, 'lighting': None, 'composition': None,
        'required_elements': ['two adults', 'red umbrella'], 'forbidden_elements': ['cars'],
        'exact_text': ['OPEN'], 'preferred_model': None, 'aspect_ratio': None,
        **changes,
    }


def test_intent_preserves_explicit_elements_and_exact_text():
    result = parse_intent(json.dumps(scene()), '서울의 성인 두 명, 빨간 우산, 자동차 제외, 간판에 OPEN')
    assert result.subject == 'two adults'
    assert result.required_elements == ['two adults', 'red umbrella']
    assert result.forbidden_elements == ['cars']
    assert result.exact_text == ['OPEN']
    schema = response_format()['json_schema']['schema']
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == set(schema['properties'])


@pytest.mark.parametrize('raw', [
    'not json', '{"intent":"other","intent":"other"}',
    json.dumps(scene(exact_text=['NOT IN SOURCE'])),
    json.dumps(scene(required_elements=[42])),
    json.dumps(scene(extra='not allowed')),
])
def test_intent_rejects_invalid_responses(raw):
    with pytest.raises(ValueError):
        parse_intent(raw, '간판에 OPEN')


def test_intent_accepts_non_image_classification():
    value = scene(intent='other', subject=None, environment=None, required_elements=[], forbidden_elements=[], exact_text=[])
    assert parse_intent(json.dumps(value), '안녕하세요').intent == 'other'
