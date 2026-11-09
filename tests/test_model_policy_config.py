import pytest
from pydantic import ValidationError

from backend.app.config import Settings


def test_switching_rejects_fixed_model_id():
    with pytest.raises(ValidationError, match="LLM_MODEL_ID must be empty"):
        Settings(app_api_key="app", llm_gate_api_key="gate", model_control_enabled=True,
                 llm_model_id="/models/fixed.gguf", _env_file=None)


def test_fixed_model_remains_available_without_switching():
    settings = Settings(app_api_key="app", llm_gate_api_key="gate", model_control_enabled=False,
                        llm_model_id="/models/fixed.gguf", _env_file=None)
    assert settings.llm_model_id == "/models/fixed.gguf"


def test_switching_allows_following_active_model():
    settings = Settings(app_api_key="app", llm_gate_api_key="gate", model_control_enabled=True,
                        llm_model_id="", _env_file=None)
    assert settings.model_control_enabled
