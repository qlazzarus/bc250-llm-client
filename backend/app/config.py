from functools import lru_cache
from typing import Literal
from pathlib import Path
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from .imagegen import validate_imagegen_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_path: str = "./data/bc250-llm-client.db"
    app_api_key: str = ""
    app_auth_mode: Literal["key", "local"] = "key"
    app_allowed_hosts: str = "localhost,127.0.0.1"
    cors_origins: str = ""
    llm_gate_url: str = "http://127.0.0.1:8081"
    llm_gate_api_key: str
    llm_timeout_seconds: float = 180
    max_request_bytes: int = 65536
    max_history_messages: int = 20
    max_completion_tokens: int = 2048
    default_max_tokens: int = 512
    max_concurrent_generations: int = 2
    store_reasoning_content: bool = False
    llm_disable_reasoning: bool = True
    llm_model_id: str = ""
    llm_context_tokens: int = 8192
    llm_context_safety_tokens: int = 512
    response_length_short_tokens: int = 512
    response_length_medium_tokens: int = 1024
    response_length_long_tokens: int = 2048
    image_prompt_max_tokens: int = 512
    imagegen_enabled: bool = False
    imagegen_allow_fake: bool = False
    imagegen_url: str = "http://127.0.0.1:8090"
    imagegen_api_key: str = ""
    imagegen_timeout_seconds: float = 20
    max_image_jobs: int = 10
    max_image_asset_bytes: int = 20 * 1024 * 1024
    image_job_poll_min_interval_seconds: float = 2
    roleplay_summary_trigger_tokens: int = 5000
    roleplay_summary_keep_recent_turns: int = 6
    roleplay_summary_max_tokens: int = 512

    @model_validator(mode="after")
    def validate_token_settings(self) -> "Settings":
        if self.app_auth_mode == "key" and not self.app_api_key:
            raise ValueError("APP_API_KEY is required in key mode")
        if self.app_auth_mode == "local" and self.cors_origins.strip():
            raise ValueError("CORS_ORIGINS must be empty in local mode")
        if self.image_prompt_max_tokens < 1:
            raise ValueError("IMAGE_PROMPT_MAX_TOKENS must be at least 1")
        if self.image_prompt_max_tokens > self.max_completion_tokens:
            raise ValueError("IMAGE_PROMPT_MAX_TOKENS cannot exceed MAX_COMPLETION_TOKENS")
        self.imagegen_url = validate_imagegen_url(self.imagegen_url)
        if self.imagegen_timeout_seconds <= 0:
            raise ValueError("IMAGEGEN_TIMEOUT_SECONDS must be greater than 0")
        if self.max_image_jobs < 1:
            raise ValueError("MAX_IMAGE_JOBS must be at least 1")
        if self.max_image_asset_bytes < 1:
            raise ValueError("MAX_IMAGE_ASSET_BYTES must be at least 1")
        if self.image_job_poll_min_interval_seconds < 0:
            raise ValueError("IMAGE_JOB_POLL_MIN_INTERVAL_SECONDS cannot be negative")
        if self.imagegen_enabled and not self.imagegen_api_key:
            raise ValueError("IMAGEGEN_API_KEY is required when IMAGEGEN_ENABLED=true")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def ensure_data_directory(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
