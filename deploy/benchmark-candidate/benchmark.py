#!/usr/bin/env python3

import json
import time
import urllib.request
import urllib.error
import re
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

BASE = Path("/opt/ai-benchmark")
CONFIG = Path("/opt/ai/config")
GATE = "http://127.0.0.1:8081"
API = GATE + "/v1/chat/completions"
TOKENIZE_API = GATE + "/v1/chat/completions/input_tokens"
SESSION = None


class BenchmarkInterrupted(RuntimeError):
    """The suite must stop; never retry an unknown or stale inference."""


def start_snapshot(expected_model=None):
    """Bind the whole suite to one gate epoch and exact configured GGUF ID."""
    global SESSION
    SESSION = None
    try:
        active = active_model()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', active):
            raise ValueError("invalid active model name")
        if expected_model is not None and active != expected_model:
            raise ValueError("configured model changed")
        config = CONFIG / 'models' / (active + '.conf')
        entries = [line for line in config.read_text().splitlines() if line.startswith('MODEL_PATH=')]
        if len(entries) != 1:
            raise ValueError("model mapping unavailable")
        match = re.fullmatch(r'MODEL_PATH=(/[A-Za-z0-9_+./:-]+\.gguf)', entries[0])
        if not match:
            raise ValueError("invalid model mapping")
        with urllib.request.urlopen(GATE + '/model-snapshot', timeout=10) as response:
            snapshot = json.load(response)
        models = snapshot['models']['data']
        epoch = snapshot['epoch']
        if (snapshot.get('ready') is not True or not isinstance(epoch, str)
                or not re.fullmatch(r'[0-9a-f]{32}', epoch) or not isinstance(models, list)
                or len(models) != 1 or models[0].get('id') != match.group(1)
                or active_model() != active):
            raise ValueError("model snapshot changed or differs from configuration")
        SESSION = {'name': active, 'model_id': match.group(1), 'epoch': epoch}
        return SESSION.copy()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        raise BenchmarkInterrupted('Cannot verify model snapshot; stop the benchmark.') from exc


def assert_snapshot():
    previous = SESSION.copy() if SESSION else None
    if previous is None or start_snapshot(previous['name']) != previous:
        raise BenchmarkInterrupted('Model or gate epoch changed; stop the benchmark.')


def gate_post(url, payload, timeout):
    if SESSION is None:
        raise BenchmarkInterrupted('No bound gate snapshot; stop the benchmark.')
    request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'X-Model-Epoch': SESSION['epoch']}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Includes 409, timeout, lost response and malformed JSON. No automatic
        # replay: the upstream may have received part or all of the request.
        raise BenchmarkInterrupted('Gate request failed or outcome unknown; stop and recheck the model.') from exc


def active_model():
    result = subprocess.run(
        ["ai-model", "list"],
        capture_output=True,
        text=True,
        check=True, timeout=10
    )

    models = result.stdout.splitlines()

    config = CONFIG / "active-model.conf"
    active = config.read_text().strip().split("=", 1)[1]

    if active not in models:
        raise RuntimeError(f"Invalid active model: {active}")

    return active


def check_health():
    try:
        start_snapshot()
        return True
    except BenchmarkInterrupted:
        return False


def chat(messages, temperature=0.8, max_tokens=512):

    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "cache_prompt": False,
        "chat_template_kwargs": {
            "enable_thinking": False
        }
    }

    payload["model"] = SESSION["model_id"] if SESSION else ""
    start = time.perf_counter()
    result = gate_post(API, payload, 180)

    elapsed = time.perf_counter() - start

    try:
        message = result["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("invalid completion")
        usage = result.get("usage", {})
        timings = result.get("timings", {})
        if not isinstance(usage, dict) or not isinstance(timings, dict):
            raise ValueError("invalid completion metrics")
    except (TypeError, KeyError, IndexError, AttributeError, ValueError) as exc:
        raise BenchmarkInterrupted('Invalid completion response; stop the benchmark.') from exc

    return {
        "content": message.get("content") or "",
        "reasoning": message.get("reasoning_content") or "",
        "elapsed": round(elapsed, 3),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "prompt_tps": timings.get("prompt_per_second", 0),
        "generation_tps": timings.get("predicted_per_second", 0),
        "finish_reason": result["choices"][0].get("finish_reason")
    }


def run_image_tests(tests):

    results = []

    print("\n=== IMAGE PROMPT TEST ===")

    for test in tests:

        print(f"\n[{test['id']}] {test['name']}")

        messages = [
            {"role": "system", "content": test["system"]},
            {"role": "user", "content": test["user"]}
        ]

        try:
            result = chat(messages)

            try:
                parsed = json.loads(result["content"])
                valid_json = isinstance(parsed, dict)
            except (json.JSONDecodeError, TypeError):
                valid_json = False

            result["valid_json"] = valid_json

            print("JSON valid:", valid_json)
            print("Generation:", result["generation_tps"], "tok/s")
            print("Elapsed:", result["elapsed"], "sec")
            print("Output:")
            print(result["content"][:1500])

        except BenchmarkInterrupted:
            raise
        except Exception as e:
            result = {"error": str(e)}
            print("ERROR:", e)

        results.append({
            "id": test["id"],
            "name": test["name"],
            "result": result
        })

    return results


def run_rp_tests(tests):

    results = []

    print("\n=== ROLEPLAY TEST ===")

    for test in tests:

        print(f"\n[{test['id']}] {test['name']}")

        messages = [
            {"role": "system", "content": test["system"]}
        ]

        turns = []

        for index, user_text in enumerate(test["turns"], start=1):

            messages.append({
                "role": "user",
                "content": user_text
            })

            try:
                result = chat(
                    messages,
                    temperature=0.7,
                    max_tokens=1024
                )

                print(f"\nTurn {index}")
                print("User:", user_text)
                print("Assistant:", result["content"])
                print("Speed:", result["generation_tps"], "tok/s")

                messages.append({
                    "role": "assistant",
                    "content": result["content"]
                })

            except BenchmarkInterrupted:
                raise
            except Exception as e:
                result = {"error": str(e)}
                print("ERROR:", e)
                turns.append({
                    "turn": index,
                    "user": user_text,
                    "result": result
                })
                break

            turns.append({
                "turn": index,
                "user": user_text,
                "result": result
            })

        results.append({
            "id": test["id"],
            "name": test["name"],
            "turns": turns
        })

    return results


def summarize(data):

    samples = []

    for item in data["image"]:
        if "error" not in item["result"]:
            samples.append(item["result"])

    for item in data["rp"]:
        for turn in item["turns"]:
            if "error" not in turn["result"]:
                samples.append(turn["result"])

    for item in data["long"]:
        if "error" not in item["result"]:
            samples.append(item["result"])

    if not samples:
        return {}

    return {
        "samples": len(samples),
        "avg_generation_tps": round(
            sum(x["generation_tps"] for x in samples) / len(samples), 2
        ),
        "avg_prompt_tps": round(
            sum(x["prompt_tps"] for x in samples) / len(samples), 2
        ),
        "avg_elapsed": round(
            sum(x["elapsed"] for x in samples) / len(samples), 2
        ),
        "total_completion_tokens": sum(
            x["completion_tokens"] for x in samples
        ),
        "total_reasoning_chars": sum(
            len(x["reasoning"]) for x in samples
        )
    }


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--suite",
        choices=["all", "image", "rp", "long"],
        default="all"
    )

    args = parser.parse_args()

    if not check_health():
        raise SystemExit("llama-server is not ready")

    model = SESSION['name']
    if active_model() != model:
        raise BenchmarkInterrupted('Configured model changed; stop the benchmark.')

    print("Active model:", model)

    tests = json.loads(
        (BASE / "prompts.json").read_text(encoding="utf-8")
    )

    data = {
        "model": model,
        "date": datetime.now().isoformat(),
        "suite": args.suite,
        "image": [],
        "rp": [],
        "long": []
    }

    if args.suite in ("all", "image"):
        data["image"] = run_image_tests(tests["image"])

    if args.suite in ("all", "rp"):
        data["rp"] = run_rp_tests(tests["rp"])

    if args.suite in ("all", "long"):
        data["long"] = run_long_tests(tests.get("long", []))

    assert_snapshot()
    data["summary"] = summarize(data)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    output = BASE / "results" / f"{model}_{timestamp}.json"

    output.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    output.chmod(0o600)

    print("\n=== SUMMARY ===")
    print(json.dumps(data["summary"], indent=2))
    print("\nSaved:", output)



LONG_PARAGRAPH = (
    "항공정보 시스템은 외부 데이터를 수집하고 정규화한 뒤 "
    "메시지 큐를 통해 처리 서비스로 전달한다. "
    "처리 서비스는 중복 메시지 제거, 오류 복구, 데이터 검증, "
    "데이터베이스 저장을 수행한다. "
    "운영 환경에서는 처리 지연, 메모리 사용량, 장애 복구 시간, "
    "서비스 간 의존성을 지속적으로 점검해야 한다.\n"
)


def count_tokens(messages):
    result = gate_post(TOKENIZE_API, {"model": SESSION['model_id'] if SESSION else "",
        "messages": messages, "max_tokens": 128, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False}}, 30)
    tokens = result.get('input_tokens') if isinstance(result, dict) else None
    if type(tokens) is not int or tokens < 0:
        raise BenchmarkInterrupted('Invalid token count; stop the benchmark.')
    return tokens


def build_long_prompt(system, target_tokens):
    instruction = "\n위 기술 문서를 요약하고 문제점 3가지를 제시해."
    document = LONG_PARAGRAPH

    # 서버 토크나이저로 길이를 조정
    for _ in range(12):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": document + instruction}
        ]

        current = count_tokens(messages)

        if abs(current - target_tokens) <= 40:
            break

        if current < target_tokens:
            missing = target_tokens - current
            unit_tokens = max(1, count_tokens([
                {"role": "user", "content": LONG_PARAGRAPH}
            ]))
            repeats = max(1, missing // unit_tokens)
            document += LONG_PARAGRAPH * repeats
        else:
            document = document[:max(
                100, int(len(document) * target_tokens / current)
            )]

    return document + instruction


def run_long_tests(tests):
    results = []

    print("\n=== LONG PROMPT TEST ===")

    for test in tests:
        print(f"\n[{test['id']}] {test['name']}")

        try:
            user_prompt = build_long_prompt(
                test["system"],
                test["target_tokens"]
            )

            messages = [
                {"role": "system", "content": test["system"]},
                {"role": "user", "content": user_prompt}
            ]

            result = chat(
                messages,
                temperature=0,
                max_tokens=128
            )

            result["target_tokens"] = test["target_tokens"]

            print("Prompt tokens:", result["prompt_tokens"])
            print("Prompt speed:", result["prompt_tps"], "tok/s")
            print("Generation speed:", result["generation_tps"], "tok/s")
            print("Elapsed:", result["elapsed"], "sec")

        except BenchmarkInterrupted:
            raise
        except Exception as e:
            result = {"error": str(e)}
            print("ERROR:", e)

        results.append({
            "id": test["id"],
            "name": test["name"],
            "result": result
        })

    return results

if __name__ == "__main__":
    main()
