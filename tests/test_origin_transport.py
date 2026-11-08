import io
import json
import os
import socket
import stat
import struct
from types import SimpleNamespace

import pytest

from backend.app.model_control import origin_transport as transport


def test_strict_caddy_json_and_stable_digest():
    one = transport.decode_config(b'{"z":[2,1],"a":{"b":3}}')
    two = transport.decode_config(b'{"a":{"b":3},"z":[2,1]}')
    assert transport.config_digest(one) == transport.config_digest(two)
    assert transport.config_digest(one) != transport.config_digest({'a': {'b': 3}, 'z': [1, 2]})
    for raw in [b'{"a":1,"a":2}', b'{"x":NaN}', b'[]']:
        with pytest.raises(ValueError): transport.decode_config(raw)
    with pytest.raises(ValueError): transport.decode_config(b' ' * (transport.MAX_CONFIG_BYTES + 1))


@pytest.mark.parametrize('members,primary,alias,shell', [
    (['caddy', 'yeppyshiba'], [], False, '/usr/sbin/nologin'),
    (['caddy'], ['ai-client'], False, '/usr/sbin/nologin'),
    (['caddy'], [], True, '/usr/sbin/nologin'),
    (['caddy'], [], False, '/bin/bash'),
])
def test_admin_group_rejects_unexpected_members_or_login(monkeypatch, members, primary, alias, shell):
    service = SimpleNamespace(pw_name='caddy', pw_uid=970, pw_gid=970, pw_shell=shell)
    users = [service] + [SimpleNamespace(pw_name=name, pw_uid=2000 + i, pw_gid=995) for i, name in enumerate(primary)]
    if alias: users.append(SimpleNamespace(pw_name='alias', pw_uid=970, pw_gid=970))
    monkeypatch.setattr(transport.grp, 'getgrnam', lambda _name: SimpleNamespace(gr_gid=995, gr_mem=members))
    monkeypatch.setattr(transport.grp, 'getgrall', lambda: [SimpleNamespace(gr_gid=995, gr_name='proxy-admin')])
    monkeypatch.setattr(transport.pwd, 'getpwnam', lambda _name: service)
    monkeypatch.setattr(transport.pwd, 'getpwall', lambda: users)
    with pytest.raises(RuntimeError): transport.verify_admin_group('proxy-admin', 'caddy')


def test_admin_group_accepts_only_dedicated_service(monkeypatch):
    service = SimpleNamespace(pw_name='caddy', pw_uid=970, pw_gid=970, pw_shell='/usr/sbin/nologin')
    monkeypatch.setattr(transport.grp, 'getgrnam', lambda _name: SimpleNamespace(gr_gid=995, gr_mem=['caddy']))
    monkeypatch.setattr(transport.grp, 'getgrall', lambda: [SimpleNamespace(gr_gid=995, gr_name='proxy-admin')])
    monkeypatch.setattr(transport.pwd, 'getpwnam', lambda _name: service)
    monkeypatch.setattr(transport.pwd, 'getpwall', lambda: [service])
    assert transport.verify_admin_group('proxy-admin', 'caddy') == (995, 970, 970)


def test_admin_group_rejects_shared_group_id(monkeypatch):
    service = SimpleNamespace(pw_name='caddy', pw_uid=970, pw_gid=970, pw_shell='/usr/sbin/nologin')
    monkeypatch.setattr(transport.grp, 'getgrnam', lambda _name: SimpleNamespace(gr_gid=995, gr_mem=['caddy']))
    monkeypatch.setattr(transport.grp, 'getgrall', lambda: [SimpleNamespace(gr_gid=995, gr_name='other')])
    monkeypatch.setattr(transport.pwd, 'getpwnam', lambda _name: service)
    with pytest.raises(RuntimeError, match='group ID is shared'):
        transport.verify_admin_group('proxy-admin', 'caddy')


@pytest.mark.parametrize('fault', [None, 'peer', 'status', 'content_type', 'duplicate', 'socket_changed', 'short_body'])
def test_caddy_config_read_pins_peer_and_socket(monkeypatch, fault):
    payload = b'{"apps":{},"admin":{}}' if fault != 'duplicate' else b'{"apps":{},"apps":{}}'
    status = b'200 OK' if fault != 'status' else b'302 Found'
    content_type = b'application/json' if fault != 'content_type' else b'text/html'
    length = len(payload) + (1 if fault == 'short_body' else 0)
    raw = b'HTTP/1.1 ' + status + b'\r\nContent-Type: ' + content_type + b'\r\nContent-Length: ' + str(length).encode() + b'\r\n\r\n' + payload
    class FakeSocket:
        def settimeout(self, _timeout): pass
        def connect(self, _path): pass
        def getsockopt(self, *_args): return struct.pack('3i', 778 if fault == 'peer' else 777, 970, 970)
        def sendall(self, data): assert data.startswith(b'GET /config/ HTTP/1.1')
        def recv(self, _size):
            value = getattr(self, 'remaining', raw)
            self.remaining = b''
            return value
        def close(self): pass
    calls = []
    def metadata(_path, **_kwargs):
        calls.append(1)
        return (2, len(calls) if fault == 'socket_changed' else 4)
    monkeypatch.setattr(transport, 'verify_admin_socket', metadata)
    monkeypatch.setattr(socket, 'SO_PEERCRED', 17, raising=False)
    monkeypatch.setattr(transport.socket, 'socket', lambda *_args: FakeSocket())
    kwargs = dict(expected_pid=777, expected_uid=970, expected_gid=970, admin_gid=995)
    if fault is None:
        assert transport.fetch_caddy_config('/run/test/config.sock', **kwargs) == json.loads(payload)
        assert len(calls) == 2
    else:
        with pytest.raises((RuntimeError, ValueError)):
            transport.fetch_caddy_config('/run/test/config.sock', **kwargs)


@pytest.mark.parametrize('mode,uid,gid', [(0o666, 970, 995), (0o660, 1000, 995), (0o660, 970, 1000)])
def test_admin_socket_rejects_wrong_permissions(monkeypatch, mode, uid, gid):
    directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o770, st_uid=0, st_gid=995)
    sock = SimpleNamespace(st_mode=stat.S_IFSOCK | mode, st_uid=uid, st_gid=gid, st_nlink=1)
    real_lstat = os.lstat
    def lstat(path, *args, **kwargs):
        if str(path) == '/run/proxy-admin': return directory
        if str(path) == '/run/proxy-admin/config.sock': return sock
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(transport, 'trusted_path', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(transport.os, 'lstat', lstat)
    with pytest.raises(RuntimeError): transport.verify_admin_socket('/run/proxy-admin/config.sock', owner_uid=970, admin_gid=995)


def test_slow_drip_response_exceeds_absolute_deadline(monkeypatch):
    class SlowSocket:
        def settimeout(self, _timeout): pass
        def connect(self, _path): pass
        def getsockopt(self, *_args): return struct.pack('3i', 777, 970, 970)
        def sendall(self, _data): pass
        def recv(self, _size): return b'x'
        def close(self): pass
    ticks = iter([0, 0, 0, 1, 2, 3, 4, 5, 6])
    monkeypatch.setattr(transport.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(transport, 'verify_admin_socket', lambda *_a, **_kw: (1, 2))
    monkeypatch.setattr(socket, 'SO_PEERCRED', 17, raising=False)
    monkeypatch.setattr(transport.socket, 'socket', lambda *_args: SlowSocket())
    with pytest.raises(TimeoutError, match='deadline'):
        transport.fetch_caddy_config('/run/test/config.sock', expected_pid=777, expected_uid=970, expected_gid=970, admin_gid=995)
