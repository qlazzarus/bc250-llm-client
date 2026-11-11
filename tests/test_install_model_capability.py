"""Capability installation must not certify stale or uncoordinated results."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_model_capability as install


def candidate():
    return {'model_id': '/model.gguf', 'config_sha256': 'digest',
            'capabilities': {'reasoning_control': 'supported', 'json_schema': 'supported'}}


@pytest.mark.parametrize('changed', [None, 'epoch', 'model', 'hold', 'worker'])
def test_install_requires_same_model_epoch_and_idle_worker(tmp_path, monkeypatch, changed):
    path = tmp_path / 'candidate.json'; path.write_text(json.dumps(candidate()))
    state = {'released': False, 'writes': []}
    class Lock:
        def __init__(self, *_args, **_kwargs): pass
        def acquire(self): pass
        def release(self): state['released'] = True
    monkeypatch.setattr(install, 'FileLock', Lock)
    monkeypatch.setattr(install, 'ModelRegistry', lambda: object())
    monkeypatch.setattr(install, 'observe', lambda _: ('model', {'model_id': '/model.gguf',
                        'config_sha256': 'digest', 'capabilities': {'reasoning_control': 'unknown', 'json_schema': 'unknown'}}))
    counters = {'coord': 0, 'snapshot': 0, 'worker': 0}
    def coordination():
        counters['coord'] += 1
        return {'holds': ['legacy'] if changed == 'hold' and counters['coord'] > 1 else []}
    def snapshot():
        counters['snapshot'] += 1
        return {'epoch': ('b' if changed == 'epoch' and counters['snapshot'] > 1 else 'a') * 32,
                'context_tokens': 4096, 'models': {'data': [{'id': '/other.gguf' if changed == 'model'
                                              and counters['snapshot'] > 1 else '/model.gguf'}]}}
    async def worker():
        counters['worker'] += 1
        return {'name': 'model', 'model_id': '/model.gguf', 'generation': counters['worker'] if changed == 'worker' else 1}
    monkeypatch.setattr(install, 'read_status', coordination)
    monkeypatch.setattr(install, 'snapshot', snapshot)
    monkeypatch.setattr(install, 'worker_state', worker)
    monkeypatch.setattr(install, 'write_once', lambda *args: state['writes'].append(args))
    if changed:
        with pytest.raises(RuntimeError): install.install(path)
        assert state['writes'] == []
    else:
        assert install.install(path) == 'model'
        assert len(state['writes']) == 1
    assert state['released']


def test_candidate_rejects_incomplete_capabilities(tmp_path):
    path = tmp_path / 'candidate.json'
    value = candidate(); value['capabilities']['json_schema'] = 'unknown'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError): install.read_candidate(path)


def test_write_once_does_not_overwrite_existing_record(tmp_path, monkeypatch):
    monkeypatch.setattr(install.FileLock, '_check_trusted_directory', lambda _: None)
    monkeypatch.setattr(install.subprocess, 'run', lambda *_args, **_kwargs: None)
    install.write_once(tmp_path, 'model', candidate())
    target = tmp_path / 'model.json'
    original = target.read_bytes()
    with pytest.raises(FileExistsError): install.write_once(tmp_path, 'model', candidate())
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.capability-*'))
