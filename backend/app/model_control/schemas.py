from typing import Literal
from pydantic import BaseModel, Field


class ModelEntry(BaseModel):
    name: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    current: bool


class ModelList(BaseModel):
    models: list[ModelEntry]
    configured_model: str | None
    loaded_model: str | None
    ready: bool
    switch_enabled: bool
    active_operation_id: str | None


class Operation(BaseModel):
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    model: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')
    state: Literal['queued', 'waiting_for_gpu', 'pausing', 'draining', 'switching', 'verifying', 'rolling_back', 'succeeded', 'failed', 'failed_rolled_back', 'recovery_required']
    error_code: str | None = None
    previous_model: str | None = None
    current_model: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CurrentOperation(BaseModel):
    operation: Operation | None
