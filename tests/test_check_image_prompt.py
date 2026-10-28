import pytest

from backend.app.image_prompt import text_system_instruction as system_instruction
from backend.app.image_intent import INTENT_INSTRUCTION
from scripts import check_image_prompt


@pytest.mark.parametrize("value", [
    "https://127.0.0.1:8081",
    "http://localhost:8081",
    "http://127.0.0.1:8080",
    "http://example.invalid:8081",
    "http://127.0.0.1:8081/v1/models",
])
def test_loopback_gate_url_rejects_noncanonical_or_nonloopback_values(value):
    with pytest.raises(SystemExit):
        check_image_prompt.loopback_gate_url(value)


def test_check_script_uses_application_profile_and_safe_gate_url(monkeypatch):
    captured = []

    def fake_request(url, _api_key, payload=None):
        captured.append((url, payload))
        if payload is None:
            return {"data": [{"id": "active-llm"}]}
        if payload['response_format']['json_schema']['name'] == 'structured_image_intent':
            return {"choices": [{"finish_reason": "stop", "message": {"content": '''{
              "intent":"image_generation", "subject":"adult with red umbrella", "environment":"Seoul", "style":null,
              "lighting":null, "composition":null, "required_elements":[], "forbidden_elements":[],
              "exact_text":["OPEN"], "preferred_model":null, "aspect_ratio":null}'''}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": '''{
            "model_id":"klein-4b-q4", "prompt":"A red umbrella beside an OPEN sign", "negative_prompt":"", "recommendation_reason":"적합"
        }'''}}]}

    monkeypatch.setenv("LLM_GATE_URL", "http://127.0.0.1:8081/")
    monkeypatch.setenv("LLM_GATE_API_KEY", "test-gate-key")
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.setattr(check_image_prompt, "request_json", fake_request)

    check_image_prompt.main()

    assert [item[0] for item in captured] == [
        "http://127.0.0.1:8081/v1/models",
        "http://127.0.0.1:8081/v1/chat/completions",
        "http://127.0.0.1:8081/v1/chat/completions",
    ]
    analysis_payload = captured[1][1]
    assert analysis_payload['messages'][0]['content'] == INTENT_INSTRUCTION
    payload = captured[2][1]
    assert payload is not None
    assert payload['messages'][0] == {"role": "system", "content": system_instruction(['klein-4b-q4'])}
    assert '"exact_text":["OPEN"]' in payload['messages'][1]['content']
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["stream"] is False


def test_check_script_fails_safely_after_two_invalid_intents(monkeypatch, capsys):
    calls = []
    def fake_request(_url, _key, payload=None):
        if payload is None:
            return {'data': [{'id': 'active-llm'}]}
        calls.append(payload['response_format']['json_schema']['name'])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'private-response'}}]}
    monkeypatch.setenv('LLM_GATE_API_KEY', 'private-secret')
    monkeypatch.setattr(check_image_prompt, 'request_json', fake_request)
    with pytest.raises(SystemExit):
        check_image_prompt.main()
    output = capsys.readouterr().out
    assert calls == ['structured_image_intent', 'structured_image_intent']
    assert 'private-secret' not in output and 'private-response' not in output
    assert 'Intent analysis' in output


def test_stage_retries_truncated_response_and_reports_attempts(monkeypatch):
    attempts = []
    def fake_request(_url, _key, payload):
        attempts.append(len(payload['messages']))
        return {'choices': [{'finish_reason': 'length' if len(attempts) == 1 else 'stop', 'message': {'content': 'ok'}}],
                'usage': {'total_tokens': 5, 'private_text': 10}}
    monkeypatch.setattr(check_image_prompt, 'request_json', fake_request)
    result, stats = check_image_prompt.run_stage('http://127.0.0.1:8081', 'key', {'messages': [{'role': 'user', 'content': 'test'}]}, 'Prompt', lambda value: value)
    assert result == 'ok' and attempts == [1, 2]
    assert stats['attempts'] == 2 and stats['usage'] == {'total_tokens': 5}
