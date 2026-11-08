from typing import Literal
from pydantic import BaseModel, Field


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
