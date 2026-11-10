#!/usr/bin/env python3
"""Back up selected pre-rollout files to a new root-only directory; no restore."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

REQUIRED = (
    'etc/systemd/system/bc250-llm-gate.service',
    'etc/systemd/system/bc250-ai-studio.service',
    'etc/systemd/system/bc250-llm-client.service',
    'etc/systemd/system/llama-server.service',
    'etc/bc250-llm-gate.env', 'etc/bc250-ai-studio.env', 'etc/bc250-llm-client.env',
    'opt/ai/scripts/ai-model', 'usr/local/bin/ai-model', 'opt/ai/config/active-model.conf',
    'opt/ai-benchmark/benchmark.py', 'opt/ai-benchmark/run-comparison.py',
    'opt/bc250-ai-studio/backend', 'opt/bc250-ai-studio/config', 'opt/bc250-ai-studio/frontend/dist',
    'opt/bc250-llm-client/backend', 'opt/bc250-llm-client/frontend/dist',
)
OPTIONAL = tuple('etc/systemd/system/' + name + '.service.d' for name in (
    'bc250-llm-gate', 'bc250-ai-studio', 'bc250-llm-client', 'llama-server'))


def backup(root, destination):
    root, destination = Path(root), Path(destination)
    if not root.is_absolute() or not destination.is_absolute():
        raise ValueError('Absolute paths required')
    if any(not os.path.lexists(root / name) for name in REQUIRED):
        raise FileNotFoundError('Required source is missing')
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)
    paths = list(REQUIRED) + [name for name in OPTIONAL if os.path.lexists(root / name)]
    archive = destination / 'prechange.tar'
    # GNU tar captures symlinks and extended ACL/SELinux metadata, not targets.
    subprocess.run(['/usr/bin/tar', '--acls', '--xattrs', '--selinux', '--numeric-owner',
                    '-C', str(root), '-cpf', str(archive), '--', *paths],
                   check=True, capture_output=True, timeout=180)
    os.chmod(archive, 0o600)
    subprocess.run(['/usr/bin/tar', '-tf', str(archive)], check=True, capture_output=True, timeout=180)
    with archive.open('rb') as file:
        os.fsync(file.fileno())
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='new absolute root-only backup directory')
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Root required')
        count = backup('/', args.destination)
        print(f'rollout_backup_created=True selected_paths={count}')
    except Exception:
        print('Backup failed; preserve any partial directory and investigate.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
