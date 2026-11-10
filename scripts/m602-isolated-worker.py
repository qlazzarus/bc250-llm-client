#!/usr/bin/python3
"""Run the candidate root worker with paths confined to an M6-02 scratch tree."""
import importlib.util
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
if os.geteuid() != 0 or not root.is_absolute() or root.parent != Path('/run') or not root.name.startswith('bc250-m602-'):
    raise SystemExit('invalid isolated test root')
sys.dont_write_bytecode = True
installation = root / 'install'
sys.path.insert(0, str(installation / 'lib'))
from backend.app.model_control import runtime

runtime.STATE = root / 'state'
runtime.GPU_LOCK = root / 'gpu.lock'
runtime.WORKER_LOCK = root / 'worker.lock'
runtime.SOCKET = str(root / 'socket' / 'control.sock')
spec = importlib.util.spec_from_file_location('candidate_bootstrap', installation / 'worker-bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
bootstrap.ROOT = installation
bootstrap.main()
