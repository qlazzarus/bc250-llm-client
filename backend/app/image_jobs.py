"""Durable edit drafts and local image-job records; Studio submission comes later."""

from __future__ import annotations

import sqlite3
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .image_prompt import IMAGE_MODELS, ImagePromptValidationError, parse_image_prompt_config


ACTIVE_IMAGE_JOB_STATUSES = {
    "submission_pending", "submission_unknown", "queued", "waiting_for_gpu", "running",
}


@dataclass(frozen=True)
class ImageDraftError(ValueError):
    code: str
    message: str
    http_status: int


def _source_message(db: sqlite3.Connection, conversation_id: str, message_id: str):
    row = db.execute("""SELECT messages.*, conversations.mode AS conversation_mode FROM messages JOIN conversations
                        ON conversations.id=messages.conversation_id
                        WHERE messages.id=? AND messages.conversation_id=?""", (message_id, conversation_id)).fetchone()
    if not row:
        raise ImageDraftError("not_found", "이미지 프롬프트 결과를 찾을 수 없습니다.", 404)
    if row["role"] != "assistant":
        raise ImageDraftError("invalid_image_source", "이미지 프롬프트의 assistant 결과만 편집할 수 있습니다.", 422)
    if row["conversation_mode"] != "image_prompt":
        raise ImageDraftError("invalid_image_source", "이미지 프롬프트 대화의 결과만 편집할 수 있습니다.", 422)
    generation = db.execute("""SELECT 1 FROM generation_requests
                               WHERE assistant_message_id=? AND status='completed'""", (message_id,)).fetchone()
    if not generation:
        raise ImageDraftError("invalid_image_source", "완료된 이미지 프롬프트 결과만 편집할 수 있습니다.", 422)
    try:
        parse_image_prompt_config(row["content"], historical=True)
    except ImagePromptValidationError as exc:
        raise ImageDraftError("invalid_image_source", "유효한 이미지 프롬프트 결과만 편집할 수 있습니다.", 422) from exc
    return row


def validate_draft_values(prompt: str, width: int, height: int, models: dict[str, dict[str, Any]] | None = None) -> tuple[str, int, int]:
    normalized = prompt.strip()
    if not normalized or "\x00" in normalized:
        raise ImageDraftError("invalid_image_draft", "이미지 프롬프트를 입력해 주세요.", 422)
    if len(normalized) > 8000:
        raise ImageDraftError("invalid_image_draft", "이미지 프롬프트는 8,000자를 초과할 수 없습니다.", 422)
    catalog = IMAGE_MODELS if models is None else models
    if type(width) is not int or type(height) is not int or not any([width, height] in model["supported_resolutions"] for model in catalog.values()):
        raise ImageDraftError("unsupported_image_resolution", "지원하는 해상도 조합을 선택해 주세요.", 422)
    return normalized, width, height


def draft_dict(row) -> dict[str, Any]:
    return {
        "source_message_id": row["source_message_id"],
        "conversation_id": row["conversation_id"],
        "prompt": row["prompt"],
        "width": row["width"],
        "height": row["height"],
        "revision": row["revision"],
        "generation_config": json.loads(row["generation_config_json"]),
        "preset_name": row["preset_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def validate_preset_name(name: str | None, config: dict[str, Any], models: dict[str, dict[str, Any]] | None) -> None:
    if name is None or name == "custom":
        return
    catalog = IMAGE_MODELS if models is None else models
    preset = catalog.get(config["model_id"], {}).get("presets", {}).get(name)
    if not preset or any(config[key] != preset.get(key) for key in ("width", "height", "steps", "cfg_scale")):
        raise ImageDraftError("image_preset_mismatch", "선택한 프리셋과 생성 설정이 다릅니다. 프리셋을 다시 적용하거나 사용자 설정을 선택해 주세요.", 422)


def validate_fake_source(db: sqlite3.Connection, message_id: str, config: dict[str, Any]) -> None:
    if config["model_id"] != "fake":
        return
    generation = db.execute("SELECT image_model_preference FROM generation_requests WHERE assistant_message_id=? AND status='completed'", (message_id,)).fetchone()
    if not generation or generation["image_model_preference"] != "fake":
        raise ImageDraftError("invalid_image_model", "CPU 테스트 모델은 프롬프트 작성 전에 직접 선택해야 합니다.", 422)


def save_draft(db: sqlite3.Connection, conversation_id: str, message_id: str, prompt: str, width: int, height: int, revision: int, timestamp: str, generation_config: dict[str, Any] | None = None, models: dict[str, dict[str, Any]] | None = None, preset_name: str | None = None) -> dict[str, Any]:
    source = _source_message(db, conversation_id, message_id)
    prompt, width, height = validate_draft_values(prompt, width, height, models)
    config = validate_generation_config(source["content"], generation_config, prompt, width, height, models)
    validate_fake_source(db, message_id, config)
    validate_preset_name(preset_name, config, models)
    existing = db.execute("SELECT * FROM image_drafts WHERE source_message_id=?", (message_id,)).fetchone()
    if not existing:
        if revision != 0:
            raise ImageDraftError("draft_revision_conflict", "편집 초안이 최신 상태가 아닙니다. 다시 불러온 뒤 저장해 주세요.", 409)
        db.execute("""INSERT INTO image_drafts
                      (source_message_id,conversation_id,prompt,width,height,revision,created_at,updated_at)
                      VALUES (?,?,?,?,?,?,?,?)""", (message_id, conversation_id, prompt, width, height, 1, timestamp, timestamp))
    else:
        if existing["revision"] != revision:
            raise ImageDraftError("draft_revision_conflict", "편집 초안이 최신 상태가 아닙니다. 다시 불러온 뒤 저장해 주세요.", 409)
        db.execute("""UPDATE image_drafts SET prompt=?,width=?,height=?,revision=revision+1,updated_at=?
                      WHERE source_message_id=? AND revision=?""", (prompt, width, height, timestamp, message_id, revision))
    db.execute("UPDATE image_drafts SET generation_config_json=?,preset_name=? WHERE source_message_id=?", (json.dumps(config), preset_name, message_id))
    row = db.execute("SELECT * FROM image_drafts WHERE source_message_id=?", (message_id,)).fetchone()
    return draft_dict(row)


def get_draft(db: sqlite3.Connection, conversation_id: str, message_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM image_drafts WHERE source_message_id=? AND conversation_id=?", (message_id, conversation_id)).fetchone()
    return draft_dict(row) if row else None


def has_active_image_jobs(db: sqlite3.Connection, conversation_id: str) -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_IMAGE_JOB_STATUSES)
    return bool(db.execute(
        f"SELECT 1 FROM image_jobs WHERE conversation_id=? AND status IN ({placeholders}) LIMIT 1",
        [conversation_id, *sorted(ACTIVE_IMAGE_JOB_STATUSES)],
    ).fetchone())


def active_image_job_count(db: sqlite3.Connection) -> int:
    placeholders = ",".join("?" for _ in ACTIVE_IMAGE_JOB_STATUSES)
    return int(db.execute(
        f"SELECT COUNT(*) FROM image_jobs WHERE status IN ({placeholders})",
        sorted(ACTIVE_IMAGE_JOB_STATUSES),
    ).fetchone()[0])


def image_job_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "conversation_id": row["conversation_id"],
        "source_message_id": row["source_message_id"],
        "draft_revision": row["draft_revision"],
        "prompt": row["prompt"],
        "width": row["width"],
        "height": row["height"],
        "steps": row["steps"],
        "cfg_scale": row["cfg_scale"],
        "generation_config": json.loads(row["generation_config_json"]),
        "prompt_mode": row["prompt_mode"],
        "preset_name": row["preset_name"],
        "studio_metadata": json.loads(row["studio_metadata_json"]),
        "evaluation": {"score": row["evaluation_score"], "note": row["evaluation_note"], "updated_at": row["evaluation_updated_at"]},
        "upstream_job_id": row["upstream_job_id"],
        "status": row["status"],
        "error": {"code": row["error_code"], "message": row["error_message"]} if row["error_code"] else None,
        "asset_ids": json.loads(row["asset_ids_json"]),
        "cancel_requested": row["cancel_requested_at"] is not None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def job_snapshot(db: sqlite3.Connection, conversation_id: str, message_id: str, prompt: str, width: int, height: int, revision: int, generation_config: dict[str, Any] | None = None, models: dict[str, dict[str, Any]] | None = None, historical: bool = False) -> dict[str, Any]:
    source = _source_message(db, conversation_id, message_id)
    saved_draft = db.execute("SELECT * FROM image_drafts WHERE source_message_id=? AND conversation_id=?", (message_id, conversation_id)).fetchone()
    if not saved_draft or saved_draft["revision"] != revision:
        raise ImageDraftError("draft_revision_conflict", "편집 초안이 최신 상태가 아닙니다. 다시 불러온 뒤 저장해 주세요.", 409)
    if historical:
        prompt = prompt.strip()
    else:
        prompt, width, height = validate_draft_values(prompt, width, height, models)
    if (prompt, width, height) != (saved_draft["prompt"], saved_draft["width"], saved_draft["height"]):
        raise ImageDraftError("draft_snapshot_conflict", "저장된 편집 초안과 제출 내용이 다릅니다. 저장한 뒤 다시 요청해 주세요.", 409)
    saved_config = json.loads(saved_draft["generation_config_json"])
    config = validate_generation_config(source["content"], generation_config if generation_config is not None else saved_config, prompt, width, height, models, historical)
    expected = validate_generation_config(source["content"], saved_config, prompt, width, height, models, historical)
    if config != expected:
        raise ImageDraftError("draft_snapshot_conflict", "저장된 모델 옵션과 제출 내용이 다릅니다.", 409)
    if not historical:
        validate_fake_source(db, message_id, config)
        validate_preset_name(saved_draft["preset_name"], config, models)
    generation = db.execute("SELECT prompt_mode FROM generation_requests WHERE assistant_message_id=? AND status='completed'", (message_id,)).fetchone()
    return {
        "prompt_mode": generation["prompt_mode"] if generation else None,
        "preset_name": saved_draft["preset_name"],
        "generation_config": config,
        "conversation_id": conversation_id,
        "source_message_id": message_id,
        "draft_revision": revision,
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": config["steps"],
        "cfg_scale": config["cfg_scale"],
    }


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    stable = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def create_or_replay_image_job(db: sqlite3.Connection, request_id: str, snapshot: dict[str, Any], timestamp: str) -> tuple[dict[str, Any], bool]:
    fingerprint = snapshot_fingerprint(snapshot)
    existing = db.execute("SELECT * FROM image_jobs WHERE request_id=?", (request_id,)).fetchone()
    if existing:
        without_preset = {key: value for key, value in snapshot.items() if key != "preset_name"}
        without_mode = {key: value for key, value in without_preset.items() if key != "prompt_mode"}
        legacy_fingerprint = snapshot_fingerprint({key: value for key, value in without_mode.items() if key != "generation_config"})
        legacy_match = existing["preset_name"] is None and snapshot["preset_name"] is None and (
            existing["payload_fingerprint"] == snapshot_fingerprint(without_preset) or
            (existing["prompt_mode"] is None and (existing["payload_fingerprint"] == snapshot_fingerprint(without_mode) or
             (existing["generation_config_json"] == "{}" and existing["payload_fingerprint"] == legacy_fingerprint))))
        if existing["conversation_id"] != snapshot["conversation_id"] or (existing["payload_fingerprint"] != fingerprint and not legacy_match):
            raise ImageDraftError("image_job_idempotency_conflict", "같은 요청 ID에는 같은 이미지 작업 내용만 사용할 수 있습니다.", 409)
        return image_job_dict(existing), True
    job_id = request_id
    client_reference = f"llm-client:{job_id}"
    try:
        db.execute("""INSERT INTO image_jobs
                      (id,request_id,conversation_id,source_message_id,draft_revision,prompt,width,height,steps,cfg_scale,payload_fingerprint,client_reference,status,created_at,updated_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'submission_pending',?,?)""",
                   (job_id, request_id, snapshot["conversation_id"], snapshot["source_message_id"], snapshot["draft_revision"],
                    snapshot["prompt"], snapshot["width"], snapshot["height"], snapshot["steps"], snapshot["cfg_scale"],
                    fingerprint, client_reference, timestamp, timestamp))
    except sqlite3.IntegrityError:
        return create_or_replay_image_job(db, request_id, snapshot, timestamp)
    db.execute("UPDATE image_jobs SET generation_config_json=?,prompt_mode=?,preset_name=? WHERE id=?", (json.dumps(snapshot.get("generation_config", {})), snapshot.get("prompt_mode"), snapshot.get("preset_name"), job_id))
    row = db.execute("SELECT * FROM image_jobs WHERE id=?", (job_id,)).fetchone()
    return image_job_dict(row), False


def set_image_job_state(db: sqlite3.Connection, job_id: str, status: str, timestamp: str, *, upstream_job_id: str | None = None, error_code: str | None = None, error_message: str | None = None, asset_ids: list[str] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if metadata:
        current = db.execute("SELECT studio_metadata_json FROM image_jobs WHERE id=?", (job_id,)).fetchone()
        merged = {**json.loads(current["studio_metadata_json"]), **metadata}
        db.execute("UPDATE image_jobs SET studio_metadata_json=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), job_id))
    db.execute("""UPDATE image_jobs SET status=?,upstream_job_id=COALESCE(?,upstream_job_id),
                  error_code=?,error_message=?,asset_ids_json=COALESCE(?,asset_ids_json),updated_at=? WHERE id=?""",
               (status, upstream_job_id, error_code, error_message,
                json.dumps(asset_ids, ensure_ascii=False) if asset_ids is not None else None, timestamp, job_id))
    row = db.execute("SELECT * FROM image_jobs WHERE id=?", (job_id,)).fetchone()
    return image_job_dict(row)


def mark_pending_jobs_unknown(db: sqlite3.Connection, timestamp: str) -> None:
    db.execute("""UPDATE image_jobs SET status='submission_unknown',error_code='submission_unknown',
                  error_message='제출 결과를 확인해야 합니다.',updated_at=? WHERE status='submission_pending'""", (timestamp,))


def mark_image_job_cancel_requested(db: sqlite3.Connection, job_id: str, timestamp: str) -> dict[str, Any]:
    db.execute("UPDATE image_jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?),updated_at=? WHERE id=?", (timestamp, timestamp, job_id))
    row = db.execute("SELECT * FROM image_jobs WHERE id=?", (job_id,)).fetchone()
    return image_job_dict(row)


def validate_generation_config(source: str, options: dict[str, Any] | None, prompt: str, width: int, height: int, models: dict[str, dict[str, Any]] | None = None, historical: bool = False) -> dict[str, Any]:
    try:
        # Empty dictionaries are legacy drafts; otherwise the supplied contract
        # is complete so switching models cannot retain incompatible options.
        value = dict(options) if options else parse_image_prompt_config(source, historical=True).to_dict()
        value.update(prompt=prompt, width=width, height=height)
        return parse_image_prompt_config(json.dumps(value), models=None if historical else models, historical=historical).to_dict()
    except ImagePromptValidationError as exc:
        raise ImageDraftError(exc.code, exc.message, 422) from exc
