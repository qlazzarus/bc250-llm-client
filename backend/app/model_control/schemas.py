from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ModelEntry(BaseModel):
    name: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    current: bool


class ModelList(BaseModel):
    models: list[ModelEntry]
    configured_model: str | None = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    loaded_model: str | None = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    ready: bool
    switch_enabled: bool
    active_operation_id: str | None = Field(pattern=r'^[a-f0-9]{32}$')


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reasoning_control: Literal["unknown", "supported", "unsupported"]
    json_schema: Literal["unknown", "supported", "unsupported"]


class ModelRuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    build_info: str = Field(min_length=1, max_length=256)
    chat_template_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_settings_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    model_id: str = Field(min_length=1, max_length=4096)
    configured_context: int = Field(gt=0, le=2147483647)
    parallel: int = Field(gt=0, le=2147483647)
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capabilities: ModelCapabilities
    runtime_identity: ModelRuntimeIdentity | None = None


class Operation(BaseModel):
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    model: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    state: Literal['queued', 'waiting_for_gpu', 'pausing', 'draining', 'switching', 'verifying', 'rolling_back', 'succeeded', 'failed', 'failed_rolled_back', 'recovery_required']
    error_code: Literal['invalid_model', 'drain_timeout', 'restart_failed', 'health_timeout',
                        'recovery_required', 'service_unavailable', 'interrupted',
                        'admission_blocked'] | None = None
    previous_model: str | None = None
    current_model: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CurrentOperation(BaseModel):
    operation: Operation | None
