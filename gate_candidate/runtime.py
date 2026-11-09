"""Systemd entry point for the uninstalled protocol 3 candidate."""

import os
from pathlib import Path

from .gate import create_app


STATE = Path('/var/lib/bc250-llm-gate/state.json')
UPSTREAM = 'http://127.0.0.1:8080'


def credential(name, *, optional=False):
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        raise RuntimeError('Systemd credentials required')
    value = (Path(directory) / name).read_bytes()
    if len(value) > 4096:
        raise RuntimeError('Invalid gate credential')
    value = value.decode('utf-8').rstrip('\r\n')
    if optional and not value:
        return None
    if len(value) < 24 or any(char.isspace() for char in value):
        raise RuntimeError('Invalid gate credential')
    return value


def create_gate_app():
    """Use pre-provisioned durable state; never initialize state on startup."""
    return create_app(STATE, UPSTREAM, admin_key=credential('admin-key'),
                      studio_key=credential('studio-key'), switch_key=credential('switch-key'),
                      upstream_api_key=credential('llm-key', optional=True))
