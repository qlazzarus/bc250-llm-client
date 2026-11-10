#!/usr/bin/env python3
"""Print an unverified capability-record draft for the currently loaded model.

This does not run inference, write files, or certify model features. An operator
must perform and record feature checks before changing any value from unknown.
Run only while model changes and service restarts are stopped.
"""
import json
import os
import stat
import sys
import urllib.request

from backend.app.model_control.capability_identity import runtime_identity
from backend.app.model_control.registry import ModelRegistry


def observe(registry):
    name = registry.current_model()
    values, config_sha256 = registry._model_config(name)
    model_id = values['MODEL_PATH']
    info = os.stat(model_id, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise RuntimeError('Model file unavailable')
    with urllib.request.urlopen('http://127.0.0.1:8080/props', timeout=10) as response:
        props = json.load(response)
    identity = runtime_identity(props, model_id)
    evidence = {'model_id': model_id, 'config_sha256': config_sha256,
                'model_file': {key: getattr(info, 'st_' + key) for key in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')},
                'runtime_identity': identity,
                'capabilities': {'reasoning_control': 'unknown', 'json_schema': 'unknown'}}
    return name, evidence


def main():
    try:
        registry = ModelRegistry()
        before = observe(registry)
        after = observe(registry)
        if before != after:
            raise RuntimeError('Model changed during observation')
        # The name belongs in the filename, not the evidence payload.
        print(json.dumps(after[1], indent=2, sort_keys=True))
    except Exception:
        print('Cannot prepare capability draft; verify current model and service state.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
