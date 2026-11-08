"""Bounded, authenticated read of Caddy's effective configuration.

This transport is a building block, not an origin safety verdict. Route,
runtime, asset and dynamic-response provenance must also be verified.
"""
import hashlib
import io
import time
import http.client
import json
import os
import grp
import pwd
import re
import socket
import stat
import struct
from pathlib import Path

from .client_trust import trusted_path

MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_WIRE_BYTES = MAX_CONFIG_BYTES + 65536
CONFIG_DEADLINE_SECONDS = 5


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate configuration key')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('Invalid JSON constant')


def decode_config(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('Invalid Caddy configuration size')
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('Invalid Caddy configuration')
    return value


def config_digest(config):
    raw = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('Caddy configuration exceeds size limit')
    return hashlib.sha256(raw).hexdigest()


def verify_admin_group(group_name, service_name):
    """Reject an administrative group usable by any other local account."""
    group = grp.getgrnam(group_name)
    service = pwd.getpwnam(service_name)
    if (group.gr_gid == 0 or service.pw_uid == 0
            or service.pw_shell not in {'/usr/sbin/nologin', '/sbin/nologin', '/bin/false', '/usr/bin/false'}):
        raise RuntimeError('Unsafe Caddy administrative account')
    if any(entry.gr_gid == group.gr_gid and entry.gr_name != group_name for entry in grp.getgrall()):
        raise RuntimeError('Caddy admin group ID is shared')
    accounts = pwd.getpwall()
    if any(entry.pw_uid == service.pw_uid and entry.pw_name != service_name for entry in accounts):
        raise RuntimeError('Caddy service UID is shared')
    members = set(group.gr_mem) | {entry.pw_name for entry in accounts if entry.pw_gid == group.gr_gid}
    if service_name not in members or members - {service_name, 'root'}:
        raise RuntimeError('Caddy admin group has unexpected members')
    return group.gr_gid, service.pw_uid, service.pw_gid


def verify_admin_socket(path, *, owner_uid, admin_gid):
    """Check the fixed administrative socket and its root-managed parent."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('Invalid Caddy admin socket path')
    trusted_path(path.parent.parent, directory=True)
    directory = os.lstat(path.parent)
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0
            or directory.st_gid != admin_gid or stat.S_IMODE(directory.st_mode) != 0o770):
        raise RuntimeError('Untrusted Caddy admin directory')
    info = os.lstat(path)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner_uid
            or info.st_gid != admin_gid or stat.S_IMODE(info.st_mode) != 0o660):
        raise RuntimeError('Untrusted Caddy admin socket')
    return info.st_dev, info.st_ino


def fetch_caddy_config(path, *, expected_pid, expected_uid, expected_gid, admin_gid):
    """GET /config/ over a socket whose peer is the expected Caddy process."""
    if (any(type(value) is not int or value < 0 for value in (expected_pid, expected_uid, expected_gid))
            or expected_pid == 0):
        raise ValueError('Invalid Caddy process identity')
    before = verify_admin_socket(path, owner_uid=expected_uid, admin_gid=admin_gid)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + CONFIG_DEADLINE_SECONDS
    def set_remaining():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Caddy configuration deadline exceeded')
        connection.settimeout(remaining)
    try:
        set_remaining()
        connection.connect(str(path))
        peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer != (expected_pid, expected_uid, expected_gid):
            raise RuntimeError('Unexpected Caddy admin socket peer')
        set_remaining()
        connection.sendall(b'GET /config/ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nAccept: application/json\r\n\r\n')
        chunks = []
        size = 0
        while True:
            set_remaining()
            chunk = connection.recv(min(65536, MAX_WIRE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_WIRE_BYTES:
                raise RuntimeError('Oversized Caddy response')
            chunks.append(chunk)
        set_remaining()
        # Parse only after the bounded network read has finished. HTTPResponse
        # cannot otherwise enforce an absolute deadline across repeated reads.
        class ResponseBuffer:
            def makefile(self, *_args, **_kwargs):
                return io.BytesIO(b''.join(chunks))
        response = http.client.HTTPResponse(ResponseBuffer())
        response.begin()
        content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
        if response.status != 200 or content_type != 'application/json':
            raise RuntimeError('Invalid Caddy admin response')
        length = response.getheader('Content-Length')
        if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) > MAX_CONFIG_BYTES):
            raise RuntimeError('Invalid Caddy response length')
        if length is not None and response.getheader('Transfer-Encoding'):
            raise RuntimeError('Ambiguous Caddy response framing')
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise RuntimeError('Unexpected Caddy response encoding')
        body = response.read(MAX_CONFIG_BYTES + 1)
        if len(body) > MAX_CONFIG_BYTES or (length is not None and len(body) != int(length)):
            raise RuntimeError('Incomplete or oversized Caddy configuration')
        config = decode_config(body)
        if verify_admin_socket(path, owner_uid=expected_uid, admin_gid=admin_gid) != before:
            raise RuntimeError('Caddy admin socket changed during inspection')
        return config
    finally:
        connection.close()
