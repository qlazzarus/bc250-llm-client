import pytest
from pathlib import Path

from gate_candidate import runtime


def test_factory_uses_credentials_without_initializing_state(tmp_path, monkeypatch):
    for name, value in {'admin-key': 'a' * 24, 'studio-key': 'b' * 24,
                        'switch-key': 'c' * 24, 'client-key': 'd' * 24, 'llm-key': ''}.items():
        (tmp_path / name).write_text(value)
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    calls = []
    monkeypatch.setattr(runtime, 'create_app', lambda *args, **kwargs: calls.append((args, kwargs)))
    runtime.create_gate_app()
    args, kwargs = calls[0]
    assert args == (runtime.STATE, runtime.UPSTREAM)
    assert kwargs == {'admin_key': 'a' * 24, 'studio_key': 'b' * 24,
                      'switch_key': 'c' * 24, 'client_key': 'd' * 24, 'upstream_api_key': None}
    assert not (tmp_path / 'state.json').exists()


@pytest.mark.parametrize('value', ['', 'short', 'a' * 24 + ' space'])
def test_required_credential_rejects_invalid_values(tmp_path, monkeypatch, value):
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    (tmp_path / 'admin-key').write_text(value)
    with pytest.raises(RuntimeError):
        runtime.credential('admin-key')


def test_gate_venv_is_outside_worker_bootstrap_tree():
    unit = (Path(__file__).resolve().parents[1] / 'deploy/model-control/bc250-llm-gate-protocol3.service').read_text()
    assert 'WorkingDirectory=/opt/bc250-llm-gate\n' in unit
    assert 'ExecStart=/opt/bc250-llm-gate/.venv/bin/uvicorn ' in unit
    assert '/opt/bc250-model-control/' not in unit
