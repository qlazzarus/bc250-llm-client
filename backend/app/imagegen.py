"""Narrow server-side adapter for the loopback BC-250 Image Studio API."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
import math
import re

import httpx

from .image_prompt import IMAGE_PROMPT_PROFILE, IMAGE_MODELS, ImagePromptValidationError, validate_model_config

if TYPE_CHECKING:
    from .config import Settings


class ImageGenerationUnavailable(RuntimeError):
    """A safe availability error that never includes an upstream response body."""


class ImageGenerationRejected(ImageGenerationUnavailable):
    """Studio clearly rejected a request before accepting an image job."""


class ImageGenerationSubmissionUnknown(ImageGenerationUnavailable):
    """Studio may have accepted the request, but the client cannot prove it."""


class ImageGenerationNotFound(ImageGenerationUnavailable):
    """A previously accepted Studio job or asset is no longer available."""


IMAGE_JOB_STATUSES = frozenset({
    "queued", "waiting_for_gpu", "running", "completed", "failed", "cancelled", "interrupted",
})
ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


def validate_imagegen_url(value: str) -> str:
    """Only accept the local Studio origin; paths and redirects are not supported."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == 8090
            and parsed.path in ("", "/")
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("IMAGEGEN_URL must be exactly http://127.0.0.1:8090")
    return "http://127.0.0.1:8090"


def _resolutions(value: object) -> list[list[int]]:
    if not isinstance(value, list):
        return []
    result: list[list[int]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or any(type(part) is not int for part in item):
            continue
        if all(64 <= part <= 2048 and part % 8 == 0 for part in item):
            result.append(item)
    return result


def _live_contract(local: dict[str, Any], upstream: dict[str, Any]) -> dict[str, Any]:
    """Copy only the Studio fields used for admission, after validating their shape."""
    resolutions = _resolutions(upstream.get("supported_resolutions"))
    if not resolutions or len(resolutions) != len(upstream["supported_resolutions"]):
        raise ValueError("invalid resolutions")
    raw_options = upstream.get("generation_options")
    raw_presets = upstream.get("presets")
    if not isinstance(raw_options, dict) or not isinstance(raw_presets, dict):
        raise ValueError("missing model contract")
    options = {}
    for key in ("cfg_scale", "negative_prompt", "sampling_method", "scheduler"):
        raw = raw_options.get(key)
        if not isinstance(raw, dict) or raw.get("mode") not in {"fixed", "verified", "experimental", "unsupported"}:
            raise ValueError("invalid option")
        allowed = raw.get("allowed_values", [])
        requires = raw.get("requires", {})
        if not isinstance(allowed, list) or len(allowed) > 100 or not isinstance(requires, dict):
            raise ValueError("invalid option values")
        def valid_value(value: Any) -> bool:
            return value is None or (type(value) in (int, float) and math.isfinite(value)) or (isinstance(value, str) and len(value) <= 100)
        if any(not valid_value(value) for value in allowed) or not valid_value(raw.get("default")):
            raise ValueError("invalid option values")
        expected = (int, float) if key == "cfg_scale" else (str,)
        if key != "negative_prompt" and any(type(value) not in expected for value in allowed + ([raw["default"]] if raw.get("default") is not None else [])):
            raise ValueError("invalid option types")
        if any(key not in ("cfg_scale", "sampling_method", "scheduler") or not isinstance(values, list) or len(values) > 100 or any(not valid_value(v) for v in values) for key, values in requires.items()):
            raise ValueError("invalid option requirements")
        option = {"mode": raw["mode"], "default": raw.get("default"), "allowed_values": allowed, "requires": requires}
        for bound in ("minimum", "maximum"):
            value = raw.get(bound)
            if value is not None:
                if key != "cfg_scale" or type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("invalid option range")
                option[bound] = value
        if option.get("minimum") is not None and option.get("maximum") is not None and option["minimum"] > option["maximum"]:
            raise ValueError("invalid option range")
        if isinstance(raw.get("note"), str):
            option["note"] = raw["note"][:1000]
        options[key] = option
    model = deepcopy(local)
    model.update(supported_resolutions=resolutions, generation_options=options, presets={})
    for name in ("fast", "balanced", "quality"):
        raw = raw_presets.get(name)
        if not isinstance(raw, dict) or any(key not in raw for key in ("width", "height", "steps", "cfg_scale")):
            raise ValueError("invalid preset")
        preset = {key: raw[key] for key in ("width", "height", "steps", "cfg_scale")}
        validate_model_config({"prompt": "test", **preset}, model)
        if isinstance(raw.get("validation_status"), str):
            preset["validation_status"] = raw["validation_status"][:100]
        model["presets"][name] = preset
    # Runtime settings can change independently of the bundled catalog.
    model.pop("server_settings", None)
    lora = upstream.get("lora")
    model["lora_toggle_supported"] = bool(upstream.get("lora_toggle_supported") is True and local.get("family") == "moss-image" and isinstance(lora, dict) and lora.get("enabled") is True)
    if isinstance(lora, dict) and type(lora.get("enabled")) is bool:
        settings: dict[str, Any] = {"lora": {"enabled": lora["enabled"]}}
        strength = lora.get("strength")
        if type(strength) in (int, float) and math.isfinite(strength):
            settings["lora"]["strength"] = strength
        trigger = lora.get("trigger")
        if isinstance(trigger, str) and len(trigger) <= 100:
            settings["lora"]["trigger"] = trigger
        shift = upstream.get("flow_shift")
        if type(shift) in (int, float) and math.isfinite(shift):
            settings["flow_shift"] = shift
        model["server_settings"] = settings
    return model


class ImageStudioAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def capabilities(self) -> dict[str, Any]:
        models = deepcopy(list(IMAGE_MODELS.values()))
        for model in models:
            model["execution_enabled"] = False
        reason = "disabled"
        simulated = False
        if self.settings.imagegen_enabled:
            try:
                async with self._client() as client:
                    status_response = await client.get("/api/v1/system/status", headers=self._headers())
                    status_response.raise_for_status()
                    model_response = await client.get("/api/v1/models", headers=self._headers())
                    model_response.raise_for_status()
                    status, upstream_models = status_response.json(), model_response.json()
                if not isinstance(status, dict) or not isinstance(upstream_models, list):
                    raise ValueError("invalid catalog")
                for model in models:
                    upstream = next((item for item in upstream_models if isinstance(item, dict) and item.get("model_id") == model["model_id"]), None)
                    if upstream:
                        try:
                            live = _live_contract(model, upstream)
                        except (ValueError, TypeError, KeyError, ImagePromptValidationError):
                            model["contract_matches"] = False
                            continue
                        model.update(live)
                        model["contract_matches"] = True
                        model["execution_enabled"] = upstream.get("execution_enabled") is True and upstream.get("validation_status") != "failed"
                if status.get("engine") == "fake":
                    for model in models:
                        model["execution_enabled"] = False
                    if self.settings.imagegen_allow_fake:
                        upstream_fake = next((item for item in upstream_models if isinstance(item, dict) and item.get("model_id") == "fake" and item.get("engine") == "fake" and item.get("family") == "fake"), None)
                        if upstream_fake:
                            try:
                                fake = _live_contract({"model_id": "fake", "display_name": "CPU Test Engine · 테스트 전용", "family": "fake"}, upstream_fake)
                                fake.update(contract_matches=True, execution_enabled=upstream_fake.get("execution_enabled") is True and upstream_fake.get("validation_status") != "failed", simulated=True)
                                models.append(fake)
                                simulated = True
                            except (ValueError, TypeError, KeyError, ImagePromptValidationError):
                                pass
                reason = ("recovery_required" if status.get("recovery_required") is True else
                          "simulated_engine" if status.get("engine") == "fake" and not simulated else
                          None if any(model["execution_enabled"] for model in models) else "model_unavailable")
            except httpx.TimeoutException as exc:
                raise ImageGenerationUnavailable("imagegen_timeout") from exc
            except httpx.HTTPError as exc:
                raise ImageGenerationUnavailable("imagegen_unavailable") from exc
            except (ValueError, TypeError) as exc:
                raise ImageGenerationUnavailable("imagegen_invalid_response") from exc
        return {"enabled": self.settings.imagegen_enabled, "available": reason is None, "reason": reason, "simulated": simulated,
                "models": models, "model": next(model for model in models if model["model_id"] == IMAGE_PROMPT_PROFILE.model_id)}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.imagegen_url,
            timeout=self.settings.imagegen_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.imagegen_api_key}"}

    async def submit_job(self, payload: dict[str, Any]) -> dict[str, str]:
        capabilities = await self.capabilities()
        model = next((model for model in capabilities["models"] if model["model_id"] == payload.get("model_id")), None)
        if not capabilities["available"] or not model or not model["execution_enabled"]:
            raise ImageGenerationRejected("imagegen_not_ready")
        try:
            validate_model_config(payload, model)
        except ImagePromptValidationError as exc:
            raise ImageGenerationRejected("imagegen_invalid_options") from exc
        try:
            async with self._client() as client:
                response = await client.post("/api/v1/jobs", headers=self._headers(), json=payload)
                response.raise_for_status()
                value = response.json()
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                raise ImageGenerationRejected("imagegen_rejected") from exc
            raise ImageGenerationSubmissionUnknown("submission_unknown") from exc
        except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
            raise ImageGenerationSubmissionUnknown("submission_unknown") from exc
        if not isinstance(value, dict) or not isinstance(value.get("job_id"), str) or value.get("status") not in {"queued", "waiting_for_gpu", "running"}:
            raise ImageGenerationSubmissionUnknown("submission_unknown")
        return {"upstream_job_id": value["job_id"], "status": value["status"]}

    async def find_jobs_by_client_reference(self, client_reference: str) -> list[dict[str, str]]:
        """Return exact matches only; this is recovery evidence, never a resubmit signal."""
        if not self.settings.imagegen_enabled:
            return []
        try:
            async with self._client() as client:
                response = await client.get("/api/v1/jobs?limit=200&offset=0", headers=self._headers())
                response.raise_for_status()
                value = response.json()
        except httpx.HTTPError as exc:
            raise ImageGenerationUnavailable("imagegen_unavailable") from exc
        except ValueError as exc:
            raise ImageGenerationUnavailable("imagegen_invalid_response") from exc
        if not isinstance(value, list):
            raise ImageGenerationUnavailable("imagegen_invalid_response")
        matches: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict) or item.get("client_reference") != client_reference:
                continue
            job_id, job_status = item.get("job_id"), item.get("status")
            if isinstance(job_id, str) and job_status in IMAGE_JOB_STATUSES:
                matches.append({"upstream_job_id": job_id, "status": job_status})
        return matches

    @staticmethod
    def _job_value(value: object, upstream_job_id: str) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("job_id") != upstream_job_id:
            raise ImageGenerationUnavailable("imagegen_invalid_response")
        job_status = value.get("status")
        if job_status not in IMAGE_JOB_STATUSES:
            raise ImageGenerationUnavailable("imagegen_invalid_response")
        asset_ids: list[str] = []
        raw_asset_ids = value.get("output_asset_ids")
        if isinstance(raw_asset_ids, list):
            asset_ids.extend(item for item in raw_asset_ids if isinstance(item, str) and item)
        raw_items = value.get("output_items")
        if isinstance(raw_items, list):
            asset_ids.extend(
                item["asset_id"] for item in raw_items
                if isinstance(item, dict) and isinstance(item.get("asset_id"), str) and item["asset_id"]
            )
        metadata: dict[str, Any] = {}
        # Studio resolves random seeds before enqueueing; item zero is the actual seed
        # used for this client's single-image job.
        items = value.get("items")
        item = next((item for item in items if isinstance(item, dict) and type(item.get("index")) is int and item["index"] == 0), None) if isinstance(items, list) else None
        seed = item.get("seed") if item else value.get("seed")
        if type(seed) is int and 0 <= seed <= 2147483631:
            metadata["seed"] = seed
        parsed_times: dict[str, datetime] = {}
        for key in ("started_at", "completed_at"):
            timestamp = value.get(key)
            if isinstance(timestamp, str) and len(timestamp) <= 64:
                try:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                        parsed_times[key] = parsed
                        metadata[key] = timestamp
                except (ValueError, OverflowError):
                    pass
        if len(parsed_times) == 2:
            elapsed = (parsed_times["completed_at"] - parsed_times["started_at"]).total_seconds()
            if 0 <= elapsed <= 30 * 24 * 3600:
                metadata["duration_ms"] = round(elapsed * 1000)
        snapshot = value.get("model_snapshot")
        if isinstance(snapshot, dict):
            quantization = snapshot.get("quantization")
            if isinstance(quantization, str) and len(quantization) <= 100 and re.fullmatch(r"[A-Za-z0-9_.-]*", quantization):
                metadata["quantization"] = quantization
            lora = snapshot.get("lora")
            if isinstance(lora, dict) and type(lora.get("enabled")) is bool:
                safe_lora: dict[str, Any] = {"enabled": lora["enabled"]}
                strength = lora.get("strength")
                if type(strength) in (int, float) and math.isfinite(strength):
                    safe_lora["strength"] = strength
                metadata["lora"] = safe_lora
        # Preserve Studio order but never expose paths or arbitrary output fields.
        return {"upstream_job_id": upstream_job_id, "status": job_status, "asset_ids": list(dict.fromkeys(asset_ids)), "metadata": metadata}

    async def get_job(self, upstream_job_id: str) -> dict[str, Any]:
        if not self.settings.imagegen_enabled:
            raise ImageGenerationUnavailable("imagegen_disabled")
        try:
            async with self._client() as client:
                response = await client.get(f"/api/v1/jobs/{upstream_job_id}", headers=self._headers())
                if response.status_code == 404:
                    raise ImageGenerationNotFound("image_job_not_found")
                response.raise_for_status()
                value = response.json()
        except ImageGenerationNotFound:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageGenerationUnavailable("imagegen_unavailable") from exc
        return self._job_value(value, upstream_job_id)

    async def cancel_job(self, upstream_job_id: str) -> dict[str, Any]:
        if not self.settings.imagegen_enabled:
            raise ImageGenerationUnavailable("imagegen_disabled")
        try:
            async with self._client() as client:
                response = await client.post(f"/api/v1/jobs/{upstream_job_id}/cancel", headers=self._headers())
                if response.status_code == 404:
                    raise ImageGenerationNotFound("image_job_not_found")
                response.raise_for_status()
                value = response.json()
        except ImageGenerationNotFound:
            raise
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                raise ImageGenerationRejected("imagegen_rejected") from exc
            raise ImageGenerationUnavailable("imagegen_unavailable") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageGenerationUnavailable("imagegen_unavailable") from exc
        return self._job_value(value, upstream_job_id)

    async def fetch_asset(self, asset_id: str, max_bytes: int) -> tuple[str, bytes]:
        """Fetch a known, local-job-owned image without forwarding upstream URLs or headers."""
        if not self.settings.imagegen_enabled:
            raise ImageGenerationUnavailable("imagegen_disabled")
        try:
            async with self._client() as client:
                async with client.stream("GET", f"/api/v1/assets/{asset_id}/file", headers=self._headers()) as response:
                    if response.status_code == 404:
                        raise ImageGenerationNotFound("image_asset_not_found")
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in ALLOWED_IMAGE_MIME_TYPES:
                        raise ImageGenerationUnavailable("image_asset_invalid_type")
                    declared_length = response.headers.get("content-length")
                    if declared_length and (not declared_length.isdecimal() or int(declared_length) > max_bytes):
                        raise ImageGenerationUnavailable("image_asset_too_large")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ImageGenerationUnavailable("image_asset_too_large")
                        chunks.append(chunk)
        except ImageGenerationNotFound:
            raise
        except ImageGenerationUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageGenerationUnavailable("imagegen_unavailable") from exc
        return content_type, b"".join(chunks)
