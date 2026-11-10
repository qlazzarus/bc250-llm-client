#!/usr/bin/env python3
"""Create a new root-only staging bundle; never install it or print secrets."""
import argparse
import os
from pathlib import Path
import secrets
import sys

from coordination_status import client_identity
from studio_quiescence_status import identity as studio_identity


def environment(pid):
    values = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    return dict(item.split(b'=', 1) for item in values if b'=' in item)


def env_line(key, value):
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Invalid environment value')
    return key + '="' + value.replace('\\', '\\\\').replace('"', '\\"') + '"\n'


def prepare(destination, client_env, studio_env, upstream):
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    existing_operator = client_env.get(b'OPERATOR_KEY', b'').decode('utf-8')
    studio_api = studio_env.get(b'STUDIO_API_KEY', b'').decode('utf-8')
    studio_admin = studio_env.get(b'STUDIO_ADMIN_KEY', b'').decode('utf-8')
    if not studio_api or not studio_admin or studio_api == studio_admin:
        raise ValueError('Existing Studio authentication unavailable')
    if any(char.isspace() for char in upstream):
        raise ValueError('Invalid upstream credential')
    forbidden = {value for value in (existing_operator, studio_api, studio_admin, upstream) if value}
    def unique():
        while True:
            key = secrets.token_hex(32)
            if key not in forbidden:
                forbidden.add(key)
                return key
    gate_admin, gate_client, gate_studio, gate_switch, studio_coord = (unique() for _ in range(5))
    operator = existing_operator or unique()
    hold_id = 'client-' + secrets.token_hex(16)
    files = {'gate-admin-key': gate_admin, 'gate-client-key': gate_client,
             'gate-studio-key': gate_studio, 'switch-gate-key': gate_switch,
             'studio-coordination-key': studio_coord, 'llm-key': upstream}
    client = {'LLM_GATE_API_KEY': gate_client, 'LLM_GATE_HOLD_ID': hold_id,
              'MODEL_CONTROL_ENABLED': 'false', 'OPERATOR_KEY': operator}
    studio = {'STUDIO_ADMISSION_URL': 'http://127.0.0.1:8081/control',
              'STUDIO_ADMISSION_KEY': gate_studio, 'STUDIO_ADMISSION_OWNED_HOLDS': 'true',
              'STUDIO_COORDINATION_KEY': studio_coord, 'STUDIO_GPU_LOCK_PATH': '/run/bc250-gpu/gpu.lock',
              'STUDIO_GPU_LOCK_SECURE_PRECREATED': 'true',
              'STUDIO_SWITCH_JOURNAL_PATH': '/var/lib/bc250-model-control/switch.json'}
    files['client.env'] = ''.join(env_line(key, value) for key, value in client.items())
    files['studio.env'] = ''.join(env_line(key, value) for key, value in studio.items())
    for name, value in files.items():
        fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(value if name.endswith('.env') else value + ('\n' if value else ''))
            file.flush()
            os.fsync(file.fileno())
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return bool(existing_operator)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', help='new root-only directory')
    parser.add_argument('--upstream-key-file', help='file holding the existing llama key; omit only if unused')
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Root required')
        client, studio = client_identity(), studio_identity()
        client_env, studio_env = environment(client[0]), environment(studio[0])
        upstream = Path(args.upstream_key_file).read_text().rstrip('\r\n') if args.upstream_key_file else ''
        if args.upstream_key_file and not upstream:
            raise RuntimeError('Upstream credential file is empty')
        if client_identity() != client or studio_identity() != studio:
            raise RuntimeError('Service changed during staging')
        preserved = prepare(args.destination, client_env, studio_env, upstream)
        print('credential_bundle_prepared=True operator_key_preserved=' + str(preserved))
    except Exception:
        print('Credential staging failed; inspect the private destination without sharing it.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
