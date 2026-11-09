#!/usr/bin/env python3
"""Build a new worker lib staging tree; never install or modify a service."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def build(destination):
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o755)  # Exclusive: never overwrite a previous tree.
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-compile', '--target', str(destination),
                    '-r', str(ROOT / 'deploy/model-control/worker-requirements.txt')], check=True)
    app = destination / 'backend' / 'app'
    app.mkdir(parents=True)
    (app.parent / '__init__.py').write_text('')
    (app / '__init__.py').write_text('')
    shutil.copytree(ROOT / 'backend/app/model_control', app / 'model_control',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for directory, dirs, files in os.walk(destination, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or name.endswith(('.pth', '.pyc')):
                raise RuntimeError('Unexpected link or Python startup file in staged tree')
    subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
                    'import sys; sys.path.insert(0, sys.argv[1]); import backend.app.model_control.runtime',
                    str(destination)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='new, non-existing staging directory')
    build(parser.parse_args().destination)
