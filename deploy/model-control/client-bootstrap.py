#!/usr/bin/python3
"""Start the Client from a separately provisioned, root-owned runtime tree."""
import os
import stat
import sys
from pathlib import Path

ROOT = Path('/opt/bc250-llm-client-runtime')


def check(info, *, directory):
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind or info.st_uid != 0 or info.st_mode & 0o022 or (not directory and info.st_nlink != 1):
        raise RuntimeError('Untrusted Client installation')


def validate_tree(root):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise RuntimeError('Invalid Client installation path')
    path = Path('/')
    check(path.lstat(), directory=True)
    for part in root.parts[1:]:
        path /= part
        check(path.lstat(), directory=True)
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        for name in dirs:
            check((Path(directory) / name).lstat(), directory=True)
        for name in files:
            if name.endswith(('.pth', '.pyc')):
                raise RuntimeError('Untrusted Client startup file')
            check((Path(directory) / name).lstat(), directory=False)
    check((root / 'lib').lstat(), directory=True)


def main():
    if not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError('Isolated startup required')
    validate_tree(ROOT)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'lib'))
    import uvicorn
    uvicorn.run('app.main:app', host='127.0.0.1', port=8010, proxy_headers=True)


if __name__ == '__main__':
    main()
