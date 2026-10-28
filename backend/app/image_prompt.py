from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import math
import re
from typing import Any

from .image_intent import ImageIntent


@dataclass(frozen=True)
class ImagePromptProfile:
    """Legacy Z-Image defaults retained for stored results and callers."""

    model_id: str
    schema_name: str
    allowed_sizes: tuple[int, ...]
    supported_resolutions: tuple[tuple[int, int], ...]
    allowed_steps: tuple[int, ...]
    default_width: int
    default_height: int
    preview_width: int
    preview_height: int
    default_steps: int
    cfg_scale: float
    required_fields: tuple[str, ...]


IMAGE_PROMPT_PROFILE = ImagePromptProfile(
    model_id="z-image-nsfw-v2",
    schema_name="image_generation_config",
    allowed_sizes=(512, 768),
    supported_resolutions=((512, 512), (768, 768)),
    allowed_steps=(4, 6, 8),
    default_width=768,
    default_height=768,
    preview_width=512,
    preview_height=512,
    default_steps=6,
    cfg_scale=1.0,
    required_fields=("model_id", "prompt", "width", "height", "steps", "cfg_scale"),
)

# Keep these aliases while callers migrate to IMAGE_PROMPT_PROFILE.
IMAGE_PROMPT_MODEL_ID = IMAGE_PROMPT_PROFILE.model_id
IMAGE_PROMPT_ALLOWED_SIZES = IMAGE_PROMPT_PROFILE.allowed_sizes
IMAGE_PROMPT_ALLOWED_STEPS = IMAGE_PROMPT_PROFILE.allowed_steps
IMAGE_PROMPT_REQUIRED_FIELDS = IMAGE_PROMPT_PROFILE.required_fields

# Public model contracts copied from bc250-imagegen/config/models.yaml. No paths or secrets.
IMAGE_MODELS = {model["model_id"]: model for model in json.loads(Path(__file__).with_name("image_models.json").read_text())}
PROMPT_PROFILES = json.loads(Path(__file__).with_name("prompt_profiles.json").read_text())
OPTION_FIELDS = ("seed", "negative_prompt", "sampling_method", "scheduler", "lora_enabled")


def model_schema(model: dict[str, Any]) -> dict[str, Any]:
    options = model["generation_options"]
    cfg = options["cfg_scale"]
    cfg_schema = {"type": "number"}
    if cfg.get("allowed_values"):
        cfg_schema["enum"] = cfg["allowed_values"]
    else:
        cfg_schema.update(minimum=cfg.get("minimum", 0), maximum=cfg.get("maximum", 30))
    properties = {
        "model_id": {"type": "string", "enum": [model["model_id"]]},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
        "width": {"type": "integer", "enum": sorted({r[0] for r in model["supported_resolutions"]})},
        "height": {"type": "integer", "enum": sorted({r[1] for r in model["supported_resolutions"]})},
        "steps": {"type": "integer", "minimum": 1, "maximum": 100},
        "cfg_scale": cfg_schema,
        "seed": {"type": "integer", "minimum": -1, "maximum": 2147483631},
    }
    if options["negative_prompt"]["mode"] != "unsupported":
        properties["negative_prompt"] = {"type": "string", "maxLength": 4000}
    for key in ("sampling_method", "scheduler"):
        option = options[key]
        if option.get("default") is not None and option["mode"] != "unsupported":
            properties[key] = {"type": "string", "enum": option["allowed_values"] or [option["default"]]}
    return {"type": "object", "properties": properties, "required": list(IMAGE_PROMPT_REQUIRED_FIELDS), "additionalProperties": False,
            "allOf": [{"anyOf": [{"properties": {"width": {"enum": [width]}, "height": {"enum": [height]}}}
                                  for width, height in model["supported_resolutions"]]}]}


IMAGE_PROMPT_JSON_SCHEMA = {"anyOf": [model_schema(model) for model in IMAGE_MODELS.values()]}


def system_instruction(model_ids: list[str] | None = None) -> str:
    return """You are a professional image prompt engineer.
Convert only the current user request into an optimized English image prompt and settings.
Output exactly one valid JSON object, no Markdown or explanation. Do not generate an image or ask follow-up questions.
Select model_id from the catalog. Honor an explicitly named model; otherwise choose a suitable model using its family, name, presets and verified options. Default to z-image-nsfw-v2 when it is in the catalog and no model preference is evident. Otherwise select an available catalog model. Do not invent quality or speed benchmarks.
The English prompt must preserve explicit scene constraints, including subject count, ages, colors, composition, forbidden elements, style, and exact user-specified text. Do not invent details that change the request. Avoid adding unnecessary unspecified details.
Apply the strategy for the selected model: """ + json.dumps({key: value for key, value in PROMPT_PROFILES.items() if model_ids is None or key in model_ids}, ensure_ascii=False, separators=(",", ":")) + """
Always include model_id, prompt, width, height, steps, cfg_scale. Use the balanced preset by default, fast for a requested preview, quality for requested detail. Honor explicitly requested valid options. Steps may be 1 through 100; prefer preset steps. Use only a supported resolution pair.
Optional fields: seed (-1 for random), negative_prompt, sampling_method, scheduler. Include them only when supported. Respect option allowed_values, bounds and requires (especially negative_prompt requiring CFG 2 for PornMaster). Never include unsupported negative_prompt.
Server settings (LoRA, trigger, flow shift) are managed by Studio: do not emit them or add LoRA tags/triggers to the prompt. Experimental presets are not verified quality claims.
Catalog:\n""" + json.dumps([model for key, model in IMAGE_MODELS.items() if model_ids is None or key in model_ids], ensure_ascii=False, separators=(",", ":"))


def negative_prompt_available(model: dict[str, Any], config: dict[str, Any]) -> bool:
    options = model["generation_options"]
    option = options["negative_prompt"]
    if option["mode"] == "unsupported":
        return False
    for key, allowed in option.get("requires", {}).items():
        current = config.get(key)
        if current not in allowed:
            return False
    return True


def text_system_instruction(model_ids: list[str] | None = None, prompt_mode: str = "ai_optimized", models: dict[str, dict[str, Any]] | None = None) -> str:
    ids = model_ids if model_ids is not None else list(IMAGE_MODELS)
    catalog_models = IMAGE_MODELS if models is None else models
    catalog = [{"model_id": key, "display_name": catalog_models[key]["display_name"],
                "strategy": PROMPT_PROFILES.get(key, "For this CPU test engine, faithfully describe the requested scene without adding details."),
                "negative_prompt_supported": negative_prompt_available(catalog_models[key], catalog_models[key]["presets"]["balanced"])} for key in ids]
    strategy = ("Translate the user's request directly into English. Preserve its meaning and all stated details; "
                "do not embellish, optimize, add visual details, or apply model-specific prompt style to the translation. "
                if prompt_mode == "direct_translation" else
                "Convert the current image request to an English prompt using the selected model's strategy. ")
    return (strategy + "Return only JSON with model_id, prompt, negative_prompt, and recommendation_reason. "
            "Choose a suitable model from the catalog, honoring an explicitly requested available model. "
            "Give a brief Korean recommendation_reason based on the request and the model strategy; do not claim measured quality or speed. "
            "Preserve subject count, ages, colors, composition, forbidden elements, style, and exact user-specified text. "
            "Preserve exclusions in the prompt. If negative_prompt_supported is true, negative_prompt may list only requested exclusions; otherwise return an empty string. Do not invent exclusions. "
            "Avoid adding unnecessary unspecified details. Do not invent sexual content. "
            "Do not produce generation settings or LoRA tags. Catalog:\n"
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":")))


class ImagePromptValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImagePromptConfig:
    model_id: str
    prompt: str
    width: int
    height: int
    steps: int
    cfg_scale: float
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "model_id": self.model_id,
            "prompt": self.prompt,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "cfg_scale": self.cfg_scale,
            **self.options,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def response_format(model_ids: list[str] | None = None) -> dict[str, Any]:
    """Return the exact gate payload fragment without exposing mutable module state."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": IMAGE_PROMPT_PROFILE.schema_name,
            "strict": True,
            "schema": deepcopy(IMAGE_PROMPT_JSON_SCHEMA) if model_ids is None else {"anyOf": [model_schema(IMAGE_MODELS[key]) for key in model_ids]},
        },
    }


def text_response_format(model_ids: list[str] | None = None) -> dict[str, Any]:
    """The model writes text only; generation settings come from the catalog."""
    ids = model_ids if model_ids is not None else list(IMAGE_MODELS)
    return {"type": "json_schema", "json_schema": {"name": "image_prompt_text", "strict": True,
        "schema": {"type": "object", "properties": {
            "model_id": {"type": "string", "enum": ids},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
            "negative_prompt": {"type": "string", "maxLength": 4000},
            "recommendation_reason": {"type": "string", "maxLength": 500},
        }, "required": ["model_id", "prompt", "negative_prompt", "recommendation_reason"], "additionalProperties": False}}}


def apply_balanced_preset(content: str, model_ids: list[str] | None = None, models: dict[str, dict[str, Any]] | None = None) -> ImagePromptConfig:
    """Convert text output to the legacy result shape using server-owned settings."""
    try:
        raw = json.loads(content, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_standard_number)
    except ImagePromptValidationError:
        raise
    except (TypeError, ValueError):
        raise ImagePromptValidationError("image_prompt_invalid_json", "이미지 프롬프트 JSON 형식이 올바르지 않습니다.") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("model_id"), str) or not isinstance(raw.get("prompt"), str):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "이미지 프롬프트 필드가 올바르지 않습니다.")
    model_id = raw["model_id"]
    catalog = IMAGE_MODELS if models is None else models
    if model_id not in catalog or (model_ids is not None and model_id not in model_ids):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "지원하지 않는 이미지 모델입니다.")
    preset = catalog[model_id]["presets"]["balanced"]
    value = {"model_id": model_id, "prompt": raw["prompt"], **{key: preset[key] for key in ("width", "height", "steps", "cfg_scale")}}
    if catalog[model_id].get("lora_toggle_supported") is True:
        value["lora_enabled"] = catalog[model_id]["server_settings"]["lora"]["enabled"]
    negative = raw.get("negative_prompt", "")
    if not isinstance(negative, str) or len(negative) > 4000 or "\x00" in negative:
        raise ImagePromptValidationError("image_prompt_invalid_schema", "Negative prompt가 올바르지 않습니다.")
    if negative.strip() and negative_prompt_available(catalog[model_id], value):
        value["negative_prompt"] = negative.strip()
    return parse_image_prompt_config(json.dumps(value), models=catalog)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ImagePromptValidationError("image_prompt_invalid_json", "이미지 설정 JSON에 중복 필드가 있습니다.")
        value[key] = item
    return value


def _reject_non_standard_number(_: str) -> None:
    raise ImagePromptValidationError("image_prompt_invalid_json", "이미지 설정 JSON에 허용되지 않는 숫자 값이 있습니다.")


def parse_image_prompt_config(content: str, *, models: dict[str, dict[str, Any]] | None = None, historical: bool = False) -> ImagePromptConfig:
    """Parse one strict JSON object and verify the image-generation contract."""
    if not isinstance(content, str) or not content.strip():
        raise ImagePromptValidationError("image_prompt_invalid_json", "이미지 설정 JSON이 비어 있습니다.")
    try:
        raw = json.loads(content, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_standard_number)
    except ImagePromptValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ImagePromptValidationError("image_prompt_invalid_json", "이미지 설정 JSON 형식이 올바르지 않습니다.") from None

    if not isinstance(raw, dict):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "이미지 설정은 JSON 객체여야 합니다.")
    if not set(IMAGE_PROMPT_REQUIRED_FIELDS) <= set(raw) or set(raw) - set(IMAGE_PROMPT_REQUIRED_FIELDS) - set(OPTION_FIELDS) - {"recommendation_reason", "prompt_mode", "model_selection", "structured_intent", "negative_prompt_omitted", "preset_name"}:
        raise ImagePromptValidationError("image_prompt_invalid_schema", "이미지 설정 필드가 올바르지 않습니다.")
    if "recommendation_reason" in raw and (not isinstance(raw["recommendation_reason"], str) or len(raw["recommendation_reason"]) > 500 or "\x00" in raw["recommendation_reason"]):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "모델 추천 이유가 올바르지 않습니다.")
    if "prompt_mode" in raw and raw["prompt_mode"] not in ("ai_optimized", "direct_translation", "original"):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "프롬프트 방식이 올바르지 않습니다.")
    if "model_selection" in raw and raw["model_selection"] not in ("manual", "recommend"):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "모델 선택 방식이 올바르지 않습니다.")
    if "negative_prompt_omitted" in raw and type(raw["negative_prompt_omitted"]) is not bool:
        raise ImagePromptValidationError("image_prompt_invalid_schema", "Negative prompt 상태가 올바르지 않습니다.")
    if "preset_name" in raw and raw["preset_name"] not in ("fast", "balanced", "quality", "custom"):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "프리셋 이름이 올바르지 않습니다.")
    if "structured_intent" in raw:
        try:
            ImageIntent.model_validate(raw["structured_intent"])
        except ValueError:
            raise ImagePromptValidationError("image_prompt_invalid_schema", "이미지 요청 분석 결과가 올바르지 않습니다.") from None

    model_id = raw["model_id"]
    prompt = raw["prompt"]
    width = raw["width"]
    height = raw["height"]
    steps = raw["steps"]
    cfg_scale = raw["cfg_scale"]

    catalog = IMAGE_MODELS if models is None else models
    if not isinstance(model_id, str) or (model_id not in catalog and not (historical and model_id == "fake")):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "지원하지 않는 이미지 모델입니다.")
    if historical:
        # Historical results must remain editable after an operator changes a preset.
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000 or "\x00" in prompt
            or any(type(value) is not int or not 64 <= value <= 2048 or value % 8 for value in (width, height))
            or type(steps) is not int or not 1 <= steps <= 100
            or type(cfg_scale) not in (int, float) or not math.isfinite(cfg_scale) or not 0 <= cfg_scale <= 30):
            raise ImagePromptValidationError("image_prompt_invalid_schema", "이미지 설정 필드가 올바르지 않습니다.")
    else:
        validate_model_config(raw, catalog[model_id])
    return ImagePromptConfig(model_id, prompt.strip(), width, height, steps, float(cfg_scale),
                             {key: raw[key] for key in OPTION_FIELDS if key in raw})


def validate_model_config(raw: dict[str, Any], model: dict[str, Any]) -> None:
    def reject(message: str) -> None:
        raise ImagePromptValidationError("image_prompt_invalid_schema", message)

    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000 or "\x00" in prompt:
        reject("이미지 프롬프트가 올바르지 않습니다.")
    if any(type(raw.get(key)) is not int for key in ("width", "height")) or [raw["width"], raw["height"]] not in model["supported_resolutions"]:
        reject("지원하지 않는 이미지 해상도 조합입니다.")
    if type(raw.get("steps")) is not int or not 1 <= raw["steps"] <= 100:
        reject("생성 단계 수는 1~100이어야 합니다.")
    if "seed" in raw and (type(raw["seed"]) is not int or not -1 <= raw["seed"] <= 2147483631):
        reject("Seed가 올바르지 않습니다.")
    if "lora_enabled" in raw and (type(raw["lora_enabled"]) is not bool or model.get("lora_toggle_supported") is not True):
        reject("이 모델은 작업별 LoRA 설정을 지원하지 않습니다.")
    if raw.get("lora_enabled") is False and re.search(r"<lora:", prompt, re.IGNORECASE):
        reject("LoRA가 꺼져 있을 때는 프롬프트에 LoRA 태그를 사용할 수 없습니다.")
    options = model["generation_options"]
    for key in ("cfg_scale", "sampling_method", "scheduler"):
        if key not in raw:
            continue
        value, option = raw[key], options[key]
        if key == "cfg_scale" and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30):
            reject("CFG scale이 올바르지 않습니다.")
        if key != "cfg_scale" and not isinstance(value, str):
            reject(f"{key} 값이 올바르지 않습니다.")
        allowed = option.get("allowed_values")
        if option["mode"] == "unsupported" or (allowed and value not in allowed):
            reject(f"이 모델에서 지원하지 않는 {key} 값입니다.")
        if not allowed:
            low, high = option.get("minimum"), option.get("maximum")
            if low is None and high is None and value != option.get("default"):
                reject(f"{key}는 모델 기본값을 사용해야 합니다.")
            if (low is not None and value < low) or (high is not None and value > high):
                reject(f"{key} 범위를 벗어났습니다.")
    if "negative_prompt" in raw:
        negative = raw["negative_prompt"]
        option = options["negative_prompt"]
        if not isinstance(negative, str) or len(negative) > 4000 or "\x00" in negative or option["mode"] == "unsupported":
            reject("이 모델의 negative_prompt가 올바르지 않습니다.")
        for key, values in option.get("requires", {}).items():
            if negative and raw.get(key) not in values:
                reject(f"negative_prompt에 필요한 {key}: {values}")
