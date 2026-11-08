import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.model_control import origin_process as origin


def test_static_assets_require_matching_namespace_and_reviewed_inventory(tmp_path, monkeypatch):
    root = tmp_path / 'assets'; root.mkdir(); (root / 'index.html').write_text('ok')
    routes = {'ui': {'kind': 'static', 'target': str(root)}, 'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}}
    expected = {'ui': {'index.html': hashlib.sha256(b'ok').hexdigest()}}
    calls = []
    monkeypatch.setattr(origin, 'verify_asset_tree', lambda path, value: calls.append((path, value)))
    prefix = '/proc/123/root'
    def map_path(path):
        value = str(path)
        return Path(value[len(prefix):]) if value.startswith(prefix) else Path(value)
    def entry(path, *, directory):
        return os.lstat(map_path(path))
    monkeypatch.setattr(origin, 'trusted_entry', entry)
    real_listdir = os.listdir
    monkeypatch.setattr(origin.os, 'listdir', lambda path: real_listdir(map_path(path)))
    origin.verify_static_route_assets(routes, expected, caddy_pid=123)
    assert len(calls) == 2
    with pytest.raises(RuntimeError): origin.verify_static_route_assets(routes, {}, caddy_pid=123)
    def changed(path, *, directory):
        info = entry(path, directory=directory)
        if str(path).startswith(prefix) and str(path).endswith('index.html'):
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
        return info
    monkeypatch.setattr(origin, 'trusted_entry', changed)
    with pytest.raises(RuntimeError, match='different asset files'):
        origin.verify_static_route_assets(routes, expected, caddy_pid=123)


def test_caddy_identity_checks_unit_process_and_executable(tmp_path, monkeypatch):
    import grp
    import pwd
    executable = tmp_path / 'caddy'; executable.write_bytes(b'reviewed')
    argv = [str(executable), 'run', '--config', '/etc/caddy/Caddyfile']
    properties = {'LoadState': 'loaded', 'ActiveState': 'active', 'MainPID': '123', 'Job': '',
                  'User': 'caddy', 'Group': 'caddy', 'NoNewPrivileges': 'yes',
                  'FragmentPath': '/etc/systemd/system/caddy.service', 'DropInPaths': '', 'EnvironmentFiles': '',
                  'ExecStart': '{ path=/caddy ; argv[]=' + ' '.join(argv) + ' ; }'}
    monkeypatch.setattr(origin.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(stdout=''.join(k + '=' + v + '\n' for k, v in properties.items())))
    monkeypatch.setattr(pwd, 'getpwnam', lambda _name: SimpleNamespace(pw_uid=970))
    monkeypatch.setattr(grp, 'getgrnam', lambda _name: SimpleNamespace(gr_gid=970))
    monkeypatch.setattr(origin, 'trusted_path', lambda *_args, **_kwargs: None)
    info = executable.stat()
    monkeypatch.setattr(origin, 'trusted_entry', lambda *_args, **_kwargs: info)
    real_fstat = os.fstat
    def fstat(fd):
        value = real_fstat(fd)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_nlink=value.st_nlink, st_dev=value.st_dev,
                               st_ino=value.st_ino, st_size=value.st_size, st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns)
    monkeypatch.setattr(origin.os, 'fstat', fstat)
    real_stat = os.stat
    monkeypatch.setattr(origin.os, 'stat', lambda path, *a, **kw: info if str(path) == '/proc/123/exe' else real_stat(path, *a, **kw))
    read_text, read_bytes = Path.read_text, Path.read_bytes
    def text(path, *args, **kwargs):
        if str(path) == '/proc/123/status': return 'Uid:\t970 970 970 970\nGid:\t970 970 970 970\nNoNewPrivs:\t1\n'
        if str(path) == '/proc/123/stat': return '123 (caddy) ' + ' '.join(['S'] + ['0'] * 18 + ['456'])
        return read_text(path, *args, **kwargs)
    def data(path):
        if str(path) == '/proc/123/cmdline': return b'\0'.join(arg.encode() for arg in argv) + b'\0'
        return read_bytes(path)
    monkeypatch.setattr(Path, 'read_text', text); monkeypatch.setattr(Path, 'read_bytes', data)
    kwargs = dict(unit='caddy.service', account='caddy', group='caddy', executable=str(executable), argv=argv,
                  binary_digest=hashlib.sha256(b'reviewed').hexdigest())
    assert origin.verify_caddy_identity(**kwargs) == (123, '456', 970, 970)
    with pytest.raises(RuntimeError): origin.verify_caddy_identity(**{**kwargs, 'binary_digest': 'a' * 64})
    properties['NoNewPrivileges'] = 'no'
    with pytest.raises(RuntimeError): origin.verify_caddy_identity(**kwargs)


def test_static_inspection_binds_manifest_process_config_and_assets(monkeypatch):
    from backend.app.model_control import origin_policy as policy
    from backend.app.model_control import origin_transport
    config = {'admin': {'listen': 'unix//run/caddy-admin/control.sock'}, 'apps': {'http': {'servers': {
        's': {'listen': [':80'], 'routes': [
            {'@id': 'ui', 'handle': [{'handler': 'file_server', 'root': '/opt/client/dist'}]},
            {'@id': 'api', 'handle': [{'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:8010'}]}]},
        ]}}}}}
    manifest = {'version': 2, 'admin_socket': '/run/caddy-admin/control.sock', 'config_digest': policy.config_digest(config),
                'routes': {'ui': {'kind': 'static', 'target': '/opt/client/dist'}, 'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}},
                'static_assets': {'ui': {'index.html': 'a' * 64}},
                'caddy': {'unit': 'caddy.service', 'account': 'caddy', 'group': 'caddy', 'executable': '/usr/bin/caddy',
                          'argv': ['/usr/bin/caddy'], 'binary_digest': 'b' * 64, 'admin_group': 'caddy-admin'}}
    monkeypatch.setattr(policy, 'load_reviewed_manifest', lambda *_a, **_k: manifest)
    monkeypatch.setattr(origin, 'verify_caddy_identity', lambda **_k: (123, '456', 970, 970))
    calls = []
    monkeypatch.setattr(origin, 'verify_static_route_assets', lambda routes, assets, **kw: calls.append((routes, assets, kw)))
    monkeypatch.setattr(origin_transport, 'verify_admin_group', lambda *_args: (995, 970, 970))
    monkeypatch.setattr(origin_transport, 'fetch_caddy_config', lambda *_a, **_k: config)
    result = policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)
    assert result == {'static_routes': ['ui'], 'unverified_routes': ['api']}
    assert calls[0][2] == {'caddy_pid': 123}
    identities = iter([(123, '456', 970, 970), (124, '457', 970, 970)])
    monkeypatch.setattr(origin, 'verify_caddy_identity', lambda **_k: next(identities))
    with pytest.raises(ValueError, match='changed'):
        policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)
