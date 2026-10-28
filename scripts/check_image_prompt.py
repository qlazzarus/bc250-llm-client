"""Server-side compatibility check for the image-prompt JSON contract.

Run from the project root with LLM_GATE_URL and LLM_GATE_API_KEY already in the
environment. This script calls only the loopback gate; it never starts Studio
or submits an image-generation task.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from backend.app.image_prompt import apply_balanced_preset as parse_image_prompt_config, text_response_format as response_format, text_system_instruction as system_instruction
from backend.app.image_intent import INTENT_INSTRUCTION, parse_intent, prompt_with_intent, response_format as intent_response_format


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def loopback_gate_url(value: str) -> str:
    """Accept only the private gate endpoint so a key cannot be sent elsewhere."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == 8081
            and parsed.path in ("", "/")
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        fail("LLM_GATE_URL must be exactly the loopback gate http://127.0.0.1:8081.")
    return "http://127.0.0.1:8081"


def request_json(url: str, api_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        # Do not follow redirects: the Authorization value must remain on the
        # confirmed loopback gate endpoint.
        with build_opener(NoRedirect()).open(request, timeout=180) as response:
            result = json.load(response)
    except HTTPError as error:
        fail(f"LLM gate returned HTTP {error.code}.")
    except (URLError, TimeoutError):
        fail("LLM gate could not be reached or timed out.")
    except json.JSONDecodeError:
        fail("LLM gate returned malformed JSON.")
    if not isinstance(result, dict):
        fail("LLM gate returned an unexpected response shape.")
    return result


def numeric_usage(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if type(value.get(key)) in (int, float) and math.isfinite(value[key])}


def run_stage(gate_url: str, gate_key: str, payload: dict[str, Any], stage: str, validate):
    """Run a bounded diagnostic without printing request or response content."""
    started = time.monotonic()
    original_messages = payload["messages"]
    for attempt in range(2):
        result = request_json(f"{gate_url}/v1/chat/completions", gate_key, payload)
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("truncated")
            parsed = validate(choice["message"]["content"])
            finish_reason = choice.get("finish_reason")
            safe_finish = finish_reason if finish_reason in ("stop", "length", "content_filter", "tool_calls") else "other"
            return parsed, {"attempts": attempt + 1, "finish_reason": safe_finish,
                            "elapsed_seconds": round(time.monotonic() - started, 2), "usage": numeric_usage(result.get("usage"))}
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            if attempt == 0:
                payload["messages"] = original_messages + [{"role": "system", "content": "Return a complete valid JSON object using the required schema. Copy exact_text literally from the user request."}]
    fail(f"{stage} response did not satisfy its structured JSON contract after two attempts.")


def main() -> None:
    gate_url = loopback_gate_url(os.environ.get("LLM_GATE_URL", "http://127.0.0.1:8081"))
    gate_key = os.environ.get("LLM_GATE_API_KEY")
    if not gate_key:
        fail("LLM_GATE_API_KEY is not configured.")

    models = request_json(f"{gate_url}/v1/models", gate_key)
    model_ids = [item.get("id") for item in models.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not model_ids:
        fail("No active LLM model was returned by /v1/models.")
    model_id = os.environ.get("LLM_MODEL_ID") or model_ids[0]
    if model_id not in model_ids:
        fail("LLM_MODEL_ID is not available from /v1/models.")

    try:
        max_tokens = int(os.environ.get("IMAGE_PROMPT_MAX_TOKENS", "512"))
        if max_tokens < 1:
            raise ValueError
    except ValueError:
        fail("IMAGE_PROMPT_MAX_TOKENS must be a positive integer.")

    source = '비 오는 서울 거리에서 빨간 우산을 든 성인 한 명을 그려줘. 간판에는 "OPEN"이라고 써줘.'
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": INTENT_INSTRUCTION},
            {"role": "user", "content": source},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": intent_response_format(),
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }
    def validate_intent(content):
        intent = parse_intent(content, source)
        if intent.intent != "image_generation" or "OPEN" not in intent.exact_text:
            raise ValueError("sample intent missing exact text")
        return intent

    intent, intent_stats = run_stage(gate_url, gate_key, payload, "Intent analysis", validate_intent)
    model_ids = ["klein-4b-q4"]
    prompt_payload = {
        **payload,
        "messages": [{"role": "system", "content": system_instruction(model_ids)},
                     {"role": "user", "content": prompt_with_intent(source, intent.model_dump())}],
        "response_format": response_format(model_ids), "temperature": 0.5,
    }
    def validate_prompt(content):
        config = parse_image_prompt_config(content, model_ids)
        value = json.loads(content)
        if set(value) != {"model_id", "prompt", "negative_prompt", "recommendation_reason"}:
            raise ValueError("invalid prompt fields")
        if not isinstance(value["recommendation_reason"], str) or len(value["recommendation_reason"]) > 500:
            raise ValueError("invalid recommendation")
        if any(text not in config.prompt for text in intent.exact_text):
            raise ValueError("exact text missing")
        return config

    config, prompt_stats = run_stage(gate_url, gate_key, prompt_payload, "Prompt generation", validate_prompt)
    print("PASS: intent analysis and image-prompt JSON contracts are supported by the LLM gate.")
    print(json.dumps({
        "active_model": model_id,
        "intent": intent_stats,
        "prompt": prompt_stats,
        "config_valid": bool(config.prompt),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
