#!/usr/bin/env python3
import argparse
import csv
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import re
from uuid import uuid4
from datetime import datetime
from pathlib import Path

BASE = Path("/opt/ai-benchmark")
CONF = Path("/opt/ai/config/active-model.conf")
SERVICE = "llama-server.service"
MODELS = [
    "gemma4-e4b-heretic-rp",
    "gemma4-e4b-hauhaucs",
    "qwen36-35b-a3b-uncensored",
]

# 기존 3턴에 이어 붙일 7턴
RP_EXTRA = {
    "rp_01": [
        "그날 떠난 이유는 네가 위험해질까 봐서였어.",
        "아직도 그 사건을 조사하고 있어?",
        "당시 내가 남긴 파란 봉투 기억나?",
        "그 봉투 안에는 누군가의 사진이 있었지.",
        "사진 속 인물의 정체를 알아냈어?",
        "난 지금도 너를 믿고 있어.",
        "처음 만났던 날처럼 다시 시작할 수 있을까?",
    ],
    "rp_02": [
        "우리 처음 만났던 카페 기억나?",
        "그날 네가 주문한 건 따뜻한 라테였지.",
        "나는 그때 네가 웃던 모습이 기억나.",
        "오늘도 그 카페에 가고 싶어?",
        "아니면 집에서 음악을 들을까?",
        "우리가 처음 함께 들었던 노래 기억해?",
        "지금 네 마음이 어떤지 말해줘.",
    ],
}

EXTRA_IMAGE = [
    {
        "id": "image_03",
        "name": "정지 이미지 장면 묘사",
        "system": (
            "You are a still-image prompt engineer. "
            "Return valid JSON with keys positive_prompt, "
            "negative_prompt, style, composition, lighting. "
            "Describe only visible details in one frozen frame. "
            "No backstory, past or future events."
        ),
        "user": (
            "28세 성인 여성 탐정이 비 오는 밤 네온사인 아래 "
            "검은 코트를 입고 서 있다. 허리 위 미디엄 샷, "
            "차분하고 진지한 표정, 애니메이션 스타일."
        ),
    },
    {
        "id": "image_04",
        "name": "인물 관계와 구도",
        "system": (
            "Convert Korean scene descriptions to English "
            "image-generation prompts. Return JSON only with "
            "positive_prompt, negative_prompt, style, "
            "composition, lighting. Describe a single frame."
        ),
        "user": (
            "성인 커플이 서로 마주 보고 행복하게 웃는다. "
            "두 사람 모두 화면에 보이고 손을 잡고 있다. "
            "따뜻한 실내 조명, 미디엄 샷, 섬세한 애니메이션."
        ),
    },
    {
        "id": "image_05",
        "name": "스토리 서술 억제",
        "system": (
            "You are not a novelist. Describe only what can "
            "be seen in a single still image. No chronology, "
            "backstory, internal thoughts or future actions. "
            "Return valid JSON with positive_prompt, "
            "negative_prompt, style, composition, lighting."
        ),
        "user": (
            "오랫동안 헤어졌던 두 성인 연인이 다시 만난 "
            "순간을 한 장의 그림으로 표현해. "
            "화면에는 서로를 바라보며 미소 짓는 모습, "
            "눈가의 눈물, 석양빛이 보인다."
        ),
    },
]

def parse_image_json(content):
    import json
    import re

    text = content.strip()

    match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )
    if match:
        text = match.group(1).strip()

    parsed = json.loads(text)

    required = {
        "positive_prompt",
        "negative_prompt",
        "style",
        "composition",
        "lighting",
    }

    valid = (
        isinstance(parsed, dict)
        and required.issubset(parsed)
        and all(
            isinstance(parsed[k], str) and parsed[k].strip()
            for k in required
        )
    )

    return parsed, bool(valid)

def command(args, timeout=240):
    return subprocess.run(
        args, capture_output=True, text=True,
        timeout=timeout
    )

def active_model():
    for line in CONF.read_text().splitlines():
        if line.startswith("ACTIVE_MODEL="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ACTIVE_MODEL not found")

def service_active():
    return command(
        ["systemctl", "is-active", "--quiet", SERVICE]
    ).returncode == 0

class SwitchOutcomeUnknown(RuntimeError):
    pass


def _text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value or ''


def switch(model):
    """Use only the shared worker; never treat timeout as cancellation."""
    key = uuid4().hex
    print(f"\n=== SWITCH: {model} ===", flush=True)
    print(f"idempotency_key={key}", flush=True)
    try:
        result = command(["ai-model", "switch", model, "--key", key], timeout=360)
    except subprocess.TimeoutExpired as exc:
        output = _text(exc.stdout)
        if output: print(output, end='' if output.endswith('\n') else '\n', flush=True)
        match = re.search(r'(?m)^operation_id=([0-9a-f]{32})$', output)
        operation = match.group(1) if match else 'unknown'
        raise SwitchOutcomeUnknown(f"Switch timeout; key={key}, operation={operation}; inspect before retrying") from exc
    if result.stdout:
        print(result.stdout, flush=True)
    if result.returncode == 2 or result.returncode < 0:
        raise SwitchOutcomeUnknown(f"Switch result unknown; key={key}; inspect worker status before retrying")
    if result.returncode:
        raise RuntimeError("Shared worker rejected or failed the switch; stop comparison.")
    if not re.search(r'(?m)^operation_id=[0-9a-f]{32}$', result.stdout):
        raise SwitchOutcomeUnknown(f"Switch success response missing operation ID; key={key}; inspect before retrying")

def load_benchmark():
    spec = importlib.util.spec_from_file_location(
        "existing_benchmark", BASE / "benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def read_memory():
    def meminfo():
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                values[parts[0].rstrip(":")] = int(parts[1])
        return values

    m = meminfo()
    result = {
        "available_kib": m.get("MemAvailable"),
        "swap_used_kib": (
            m.get("SwapTotal", 0) - m.get("SwapFree", 0)
        ),
    }
    for key, filename in [
        ("vram_used_bytes",
         "/sys/class/drm/card0/device/mem_info_vram_used"),
        ("gtt_used_bytes",
         "/sys/class/drm/card0/device/mem_info_gtt_used"),
    ]:
        try:
            result[key] = int(Path(filename).read_text())
        except (OSError, ValueError):
            result[key] = None
    return result

def run_suite(bench, prompts, suite):
    data = {"image": [], "rp": [], "long": []}

    if suite in ("all", "image"):
        tests = list(prompts["image"]) + EXTRA_IMAGE
        data["image"] = bench.run_image_tests(tests)

        required = {
            "positive_prompt", "negative_prompt",
            "style", "composition", "lighting"
        }
        for item in data["image"]:
            r = item["result"]
            if "error" in r:
                continue
            try:
                #parsed = json.loads(r["content"])
                parsed = parse_image_json(r["content"])
                r["required_keys_valid"] = (
                    isinstance(parsed, dict)
                    and required.issubset(parsed.keys())
                    and all(
                        isinstance(parsed[k], str) and parsed[k].strip()
                        for k in required
                    )
                )
            except (ValueError, TypeError, KeyError):
                r["required_keys_valid"] = False

    if suite in ("all", "rp"):
        tests = json.loads(json.dumps(prompts["rp"]))
        for test in tests:
            test["turns"].extend(RP_EXTRA.get(test["id"], []))
        data["rp"] = bench.run_rp_tests(tests)

    if suite in ("all", "long"):
        data["long"] = bench.run_long_tests(
            prompts.get("long", [])
        )

    data["summary"] = bench.summarize(data)
    return data

def collect_rows(model, data):
    rows = []
    for item in data["image"] + data["long"]:
        rows.append((item["id"], item["result"]))
    for item in data["rp"]:
        for turn in item["turns"]:
            rows.append((
                f'{item["id"]}_turn_{turn["turn"]:02d}',
                turn["result"]
            ))

    return [{
        "model": model,
        "test": name,
        "status": "error" if "error" in r else "ok",
        "prompt_tokens": r.get("prompt_tokens", ""),
        "completion_tokens": r.get("completion_tokens", ""),
        "prompt_tps": r.get("prompt_tps", ""),
        "generation_tps": r.get("generation_tps", ""),
        "elapsed_sec": r.get("elapsed", ""),
        "finish_reason": r.get("finish_reason", ""),
        "valid_json": r.get("valid_json", ""),
        "required_keys_valid": r.get("required_keys_valid", ""),
        "error": r.get("error", ""),
    } for name, r in rows]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite", choices=["all", "image", "rp", "long"],
        default="all"
    )
    parser.add_argument(
        "--models", nargs="+", choices=MODELS,
        default=MODELS
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run as root")
    if not service_active():
        sys.exit("llama-server is not running; inspect through ai-model before starting comparison")

    bench = load_benchmark()
    prompts = json.loads(
        (BASE / "prompts.json").read_text(encoding="utf-8")
    )

    original = active_model()
    bench.start_snapshot(original)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = BASE / "comparisons" / stamp
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    report = {
        "started_at": datetime.now().isoformat(),
        "original_model": original,
        "original_service_running": True,
        "suite": args.suite,
        "models": {},
        "restore_error": None,
    }
    all_rows = []

    def save():
        path = out / "comparison.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        path.chmod(0o600)

        csv_path = out / "comparison.csv"
        fields = [
            "model", "test", "status",
            "prompt_tokens", "completion_tokens",
            "prompt_tps", "generation_tps",
            "elapsed_sec", "finish_reason",
            "valid_json", "required_keys_valid", "error"
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_rows)
        csv_path.chmod(0o600)

    completed_all = False
    try:
        for model in args.models:
            entry = {
                "status": "pending",
                "started_at": datetime.now().isoformat()
            }
            report["models"][model] = entry
            try:
                bench.assert_snapshot()
                switch(model)
                bench.start_snapshot(model)
                entry["memory_before"] = read_memory()
                entry["data"] = run_suite(bench, prompts, args.suite)
                bench.assert_snapshot()
                entry["memory_after"] = read_memory()
                entry["status"] = "completed"
                all_rows.extend(
                    collect_rows(model, entry["data"])
                )
                print(
                    f"\n=== FINISHED: {model} ===",
                    entry["data"]["summary"],
                    flush=True
                )
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"] = repr(exc)
                print(f"ERROR {model}: {exc}; no further model changes", flush=True)
                break
            finally:
                entry["finished_at"] = datetime.now().isoformat()
                save()
        else:
            completed_all = True
    finally:
        if completed_all:
            print(f"\n=== RESTORING: {original} ===", flush=True)
            try:
                # Do not override an independently changed model or gate epoch.
                bench.assert_snapshot()
                if active_model() != original:
                    switch(original)
                    bench.start_snapshot(original)
            except Exception as exc:
                report["restore_error"] = repr(exc)
                print("RESTORE ERROR:", exc, flush=True)
        else:
            report["restore_error"] = "Comparison interrupted; original model was not automatically restored. Inspect worker status first."
            print(report["restore_error"], flush=True)
        report["finished_at"] = datetime.now().isoformat()
        save()
        print("\nRESULT DIRECTORY:", out, flush=True)
    return 0 if completed_all and report["restore_error"] is None else 1

if __name__ == "__main__":
    raise SystemExit(main())
