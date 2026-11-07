#!/usr/bin/python3
"""Validate a root-owned package tree before importing privileged code.

Run with /usr/bin/python3 -I -S; provision lib with a separate reviewed install.
"""
import os
import stat
import sys
from pathlib import Path

ROOT = Path('/opt/bc250-model-control')


def check(info, *, directory, uid=0):
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != uid or info.st_mode & 0o022 or (not directory and info.st_nlink != 1):
        raise RuntimeError('Untrusted installation path')


def validate_tree(root, *, uid=0):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise RuntimeError('Invalid installation path')
    path = Path('/')
    check(path.lstat(), directory=True, uid=uid)
    for part in root.parts[1:]:
        path /= part
        check(path.lstat(), directory=True, uid=uid)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            check((Path(directory) / name).lstat(), directory=True, uid=uid)
        for name in files:
            if name.endswith(('.pth', '.pyc')):
                raise RuntimeError('Untrusted startup file')
            check((Path(directory) / name).lstat(), directory=False, uid=uid)


def main():
    if os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError('Root isolated startup required')
    validate_tree(ROOT)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'lib'))
    from backend.app.model_control.runtime import main as run
    run()


if __name__ == '__main__':
    main()
