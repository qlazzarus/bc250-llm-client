"""Production recovery uses the coordination verifier."""
from types import SimpleNamespace
from unittest.mock import Mock

from backend.app.model_control import runtime


def test_production_worker_has_recovery_verifier(monkeypatch):
    monkeypatch.setattr(runtime.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(runtime.os, 'umask', lambda _: None)
    monkeypatch.setattr(runtime, 'validate_state_directory', lambda: None)
    monkeypatch.setattr(runtime, 'credential', lambda *args, **kwargs: 'credential')
    for name in ('SwitchJournal', 'ModelRegistry', 'ActiveModelWriter', 'SystemctlClient',
                 'SystemdModelService', 'SwitchGate', 'SystemdLLM', 'CoordinationPreflight', 'RecoveryPreflight',
                 'ModelSwitch', 'SwitchOperations', 'FileLock'):
        monkeypatch.setattr(runtime, name, Mock())
    monkeypatch.setattr(runtime, 'AsyncGPULock', lambda _: SimpleNamespace(file_lock=object()))
    worker_constructor = Mock()
    monkeypatch.setattr(runtime, 'SwitchWorker', worker_constructor)

    runtime.build_worker()

    assert worker_constructor.call_args.kwargs['verify_studio'] is runtime.RecoveryPreflight.return_value
    assert runtime.RecoveryPreflight.call_args.kwargs['journal'] is runtime.SwitchJournal.return_value
    assert 'origin_lock' not in runtime.ModelSwitch.call_args.kwargs


def test_socket_templates_match_client_account_and_allow_directory_traversal():
    from pathlib import Path

    deploy = Path(__file__).resolve().parents[1] / 'deploy' / 'model-control'
    socket_unit = (deploy / 'bc250-model-worker.socket').read_text()
    tmpfiles = (deploy / 'origin-control.tmpfiles.conf').read_text()
    assert f'SocketGroup={runtime.CLIENT_USER}\n' in socket_unit
    assert 'SocketMode=0660\n' in socket_unit
    assert 'DirectoryMode=0755\n' in socket_unit
    assert 'd /run/bc250-model-control 0755 root root -\n' in tmpfiles
