import json

import pytest
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.image_prompt import (
    IMAGE_PROMPT_JSON_SCHEMA,
    IMAGE_MODELS,
    IMAGE_PROMPT_MODEL_ID,
    IMAGE_PROMPT_PROFILE,
    ImagePromptValidationError,
    parse_image_prompt_config,
    response_format,
    system_instruction,
)


def valid_json(**overrides):
    value = {
        "model_id": IMAGE_PROMPT_MODEL_ID,
        "prompt": "adult Korean woman in a black silk dress, cinematic hotel lounge at night",
        "width": 768,
        "height": 768,
        "steps": 6,
        "cfg_scale": 1.0,
    }
    value.update(overrides)
    return json.dumps(value)


def test_parse_image_prompt_config_accepts_and_normalizes_valid_contract():
    result = parse_image_prompt_config(valid_json(prompt="  cinematic portrait  ", cfg_scale=1))

    assert result.prompt == "cinematic portrait"
    assert result.cfg_scale == 1.0
    assert json.loads(result.to_json()) == {
        "model_id": "z-image-nsfw-v2",
        "prompt": "cinematic portrait",
        "width": 768,
        "height": 768,
        "steps": 6,
        "cfg_scale": 1.0,
    }


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("", "image_prompt_invalid_json"),
        ("```json\n{}\n```", "image_prompt_invalid_json"),
        ("[]", "image_prompt_invalid_schema"),
        ('{"model_id":"z-image-nsfw-v2","model_id":"other"}', "image_prompt_invalid_json"),
        ('{"model_id":"z-image-nsfw-v2","prompt":"x","width":NaN,"height":768,"steps":6,"cfg_scale":1}', "image_prompt_invalid_json"),
        (valid_json(prompt="   "), "image_prompt_invalid_schema"),
        (valid_json(width=True), "image_prompt_invalid_schema"),
        (valid_json(steps="6"), "image_prompt_invalid_schema"),
        (valid_json(width=512, height=768), "image_prompt_invalid_schema"),
        (valid_json(cfg_scale=1.1), "image_prompt_invalid_schema"),
        (valid_json(extra="not allowed"), "image_prompt_invalid_schema"),
    ],
)
def test_parse_image_prompt_config_rejects_invalid_values(content, code):
    with pytest.raises(ImagePromptValidationError) as caught:
        parse_image_prompt_config(content)

    assert caught.value.code == code


def test_response_format_is_strict_and_does_not_share_mutable_schema_state():
    first = response_format()
    first["json_schema"]["schema"]["anyOf"][0]["properties"]["prompt"]["minLength"] = 99
    second = response_format()

    assert second["json_schema"]["name"] == IMAGE_PROMPT_PROFILE.schema_name
    assert second["json_schema"]["strict"] is True
    assert second["json_schema"]["schema"] == IMAGE_PROMPT_JSON_SCHEMA
    assert second["json_schema"]["schema"]["anyOf"][0]["properties"]["prompt"]["minLength"] == 1


def test_image_prompt_profile_drives_schema_and_system_instruction():
    instruction = system_instruction()
    schemas = IMAGE_PROMPT_JSON_SCHEMA["anyOf"]
    assert {schema["properties"]["model_id"]["enum"][0] for schema in schemas} == set(IMAGE_MODELS)
    for schema in schemas:
        model_id = schema["properties"]["model_id"]["enum"][0]
        assert model_id in instruction
        assert schema["required"] == list(IMAGE_PROMPT_PROFILE.required_fields)
        assert schema["additionalProperties"] is False
    assert "supported resolution pair" in instruction
    assert "Respect option allowed_values" in instruction


def test_image_prompt_token_setting_must_fit_global_completion_limit():
    with pytest.raises(ValidationError):
        Settings(app_api_key="app", llm_gate_api_key="gate", max_completion_tokens=256, image_prompt_max_tokens=257)

    with pytest.raises(ValidationError):
        Settings(app_api_key="app", llm_gate_api_key="gate", image_prompt_max_tokens=0)


@pytest.mark.parametrize('model_id', list(IMAGE_MODELS))
def test_all_model_presets_round_trip(model_id):
    model = IMAGE_MODELS[model_id]
    for preset in model['presets'].values():
        raw = {key: preset[key] for key in ('width', 'height', 'steps', 'cfg_scale')}
        raw.update(model_id=model_id, prompt='A mountain under soft morning light', seed=4242)
        for field in ('sampling_method', 'scheduler'):
            value = model['generation_options'][field].get('default')
            if value is not None:
                raw[field] = value
        assert parse_image_prompt_config(json.dumps(raw)).to_dict() == raw


@pytest.mark.parametrize('changes', [
    {'cfg_scale': 4}, {'negative_prompt': 'blur'}, {'scheduler': 'karras'},
    {'sampling_method': 'heun'}, {'seed': True}, {'seed': 2147483632},
    {'steps': 101}, {'cfg_scale': float('inf')}, {'lora': 0.5}, {'flow_shift': 4},
])
def test_moss_rejects_unsupported_options(changes):
    raw = dict(model_id='moss-image-nsfw', prompt='A landscape', width=512, height=512, steps=20, cfg_scale=3)
    with pytest.raises(ImagePromptValidationError):
        parse_image_prompt_config(json.dumps({**raw, **changes}))


def test_pornmaster_negative_prompt_requires_cfg_two():
    raw = dict(model_id='pornmaster-klein-9b', prompt='A landscape', width=512, height=512, steps=4, cfg_scale=1.5, negative_prompt='blur')
    with pytest.raises(ImagePromptValidationError, match='cfg_scale'):
        parse_image_prompt_config(json.dumps(raw))
    raw['cfg_scale'] = 2
    assert parse_image_prompt_config(json.dumps(raw)).options['negative_prompt'] == 'blur'


def test_available_catalog_limits_prompt_and_schema():
    instruction = system_instruction(['moss-image-nsfw'])
    catalog = json.loads(instruction.split('Catalog:\n')[1])
    assert [model['model_id'] for model in catalog] == ['moss-image-nsfw']
    schema = response_format(['moss-image-nsfw'])['json_schema']['schema']
    assert len(schema['anyOf']) == 1
    assert schema['anyOf'][0]['properties']['cfg_scale']['enum'] == [3.0]


def test_model_strategies_are_scoped_and_preserve_explicit_constraints():
    from backend.app.image_prompt import PROMPT_PROFILES

    assert set(PROMPT_PROFILES) == set(IMAGE_MODELS)
    instruction = system_instruction(['moss-image-nsfw'])
    assert PROMPT_PROFILES['moss-image-nsfw'] in instruction
    assert PROMPT_PROFILES['realistic-vision-v6'] not in instruction
    assert 'exact user-specified text' in instruction
    assert 'subject count, ages, colors' in instruction
    from backend.app.image_prompt import text_response_format, text_system_instruction
    assert 'fake' not in text_response_format()['json_schema']['schema']['properties']['model_id']['enum']
    assert all(model['model_id'] != 'fake' for model in json.loads(text_system_instruction().split('Catalog:\n')[1]))


def test_llm_settings_are_ignored_and_balanced_preset_is_applied():
    from backend.app.image_prompt import apply_balanced_preset, text_response_format

    result = apply_balanced_preset(json.dumps({'model_id': 'klein-4b-q4', 'prompt': 'A landscape',
                                               'width': 768, 'height': 768, 'steps': 99, 'cfg_scale': 20}))
    assert (result.width, result.height, result.steps, result.cfg_scale) == (512, 512, 4, 1)
    assert set(text_response_format()['json_schema']['schema']['properties']) == {'model_id', 'prompt', 'negative_prompt', 'recommendation_reason'}
    with pytest.raises(ImagePromptValidationError):
        apply_balanced_preset(json.dumps({'model_id': 'klein-4b-q8', 'prompt': 'A landscape'}), ['klein-4b-q4'])


@pytest.mark.parametrize(('model_id', 'included'), [
    ('realistic-vision-v6', True), ('klein-4b-q4', False), ('pornmaster-klein-9b', False),
])
def test_generated_negative_prompt_follows_balanced_model_contract(model_id, included):
    from backend.app.image_prompt import apply_balanced_preset, text_system_instruction

    result = apply_balanced_preset(json.dumps({'model_id': model_id, 'prompt': 'A landscape without cars', 'negative_prompt': 'cars'}))
    assert ('negative_prompt' in result.options) is included
    catalog = json.loads(text_system_instruction([model_id]).split('Catalog:\n')[1])
    assert catalog[0]['negative_prompt_supported'] is included


@pytest.mark.parametrize('negative', [42, 'x' * 4001, 'x\x00y'])
def test_rejects_invalid_generated_negative_prompt(negative):
    from backend.app.image_prompt import apply_balanced_preset

    with pytest.raises(ImagePromptValidationError):
        apply_balanced_preset(json.dumps({'model_id': 'realistic-vision-v6', 'prompt': 'A landscape', 'negative_prompt': negative}))


def test_lora_selection_requires_live_capability_and_is_not_llm_controlled():
    from copy import deepcopy
    from backend.app.image_prompt import apply_balanced_preset
    model = deepcopy(IMAGE_MODELS['moss-image-nsfw'])
    model['lora_toggle_supported'] = True
    catalog = {'moss-image-nsfw': model}
    result = apply_balanced_preset(json.dumps({'model_id': 'moss-image-nsfw', 'prompt': 'landscape', 'lora_enabled': False}), models=catalog)
    assert result.options['lora_enabled'] is True
    raw = result.to_dict()
    raw['lora_enabled'] = False
    assert parse_image_prompt_config(json.dumps(raw), models=catalog).options['lora_enabled'] is False
    with pytest.raises(ImagePromptValidationError):
        parse_image_prompt_config(json.dumps(raw))
    raw['lora_enabled'] = 'false'
    with pytest.raises(ImagePromptValidationError):
        parse_image_prompt_config(json.dumps(raw), models=catalog)
    raw.update(lora_enabled=False, prompt='landscape <LoRA:adapter:0.7>')
    with pytest.raises(ImagePromptValidationError):
        parse_image_prompt_config(json.dumps(raw), models=catalog)
