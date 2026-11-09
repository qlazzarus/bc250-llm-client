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
    assert len(calls) == 2 and all(call[2] == {'caddy_pid': 123} for call in calls)
    def changed_assets(*_args, **_kwargs):
        calls.append(None)
        if len(calls) == 4:
            raise RuntimeError('Asset changed during inspection')
    monkeypatch.setattr(origin, 'verify_static_route_assets', changed_assets)
    with pytest.raises(RuntimeError, match='Asset changed'):
        policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)
    identities = iter([(123, '456', 970, 970), (124, '457', 970, 970)])
    monkeypatch.setattr(origin, 'verify_caddy_identity', lambda **_k: next(identities))
    with pytest.raises(ValueError, match='changed'):
        policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)


def test_dynamic_backend_checks_reviewed_tree_and_stable_process(monkeypatch):
    args = dict(unit='wiki.service', account='wiki', group='wiki', executable='/usr/bin/python3',
                argv=['/usr/bin/python3', '/opt/wiki/main.py'], binary_digest='a' * 64,
                tree='/opt/wiki', files={'main.py': 'b' * 64})
    calls = []
    monkeypatch.setattr(origin, '_verify_service_identity', lambda **kw: (123, '456', 970, 970))
    monkeypatch.setattr(origin, 'verify_static_route_assets', lambda routes, assets, **kw: calls.append((routes, assets, kw)))
    assert origin.inspect_dynamic_backend(**args) == (123, '456', 970, 970)
    assert calls == [({'backend': {'kind': 'static', 'target': '/opt/wiki'}},
                      {'backend': args['files']}, {'caddy_pid': 123})]
    identities = iter([(123, '456', 970, 970), (124, '457', 970, 970)])
    monkeypatch.setattr(origin, '_verify_service_identity', lambda **kw: next(identities))
    with pytest.raises(RuntimeError, match='changed'):
        origin.inspect_dynamic_backend(**args)


def test_origin_backend_inventory_is_bound_to_routes_but_stays_unverified(monkeypatch):
    from backend.app.model_control import origin_policy as policy, origin_transport
    config = {'admin': {'listen': 'unix//run/caddy-admin/control.sock'}, 'apps': {'http': {'servers': {
        's': {'listen': [':80'], 'routes': [
            {'@id': 'ui', 'handle': [{'handler': 'file_server', 'root': '/opt/client/dist'}]},
            {'@id': 'api', 'handle': [{'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:8010'}]}]},
        ]}}}}}
    backend = {'kind': 'dynamic', 'target': '127.0.0.1:8010', 'unit': 'client.service', 'account': 'client',
               'group': 'client', 'executable': '/usr/bin/python3', 'argv': ['/usr/bin/python3'],
               'binary_digest': 'a' * 64, 'tree': '/opt/client', 'files': {'main.py': 'b' * 64}}
    manifest = {'version': 3, 'admin_socket': '/run/caddy-admin/control.sock', 'config_digest': policy.config_digest(config),
                'routes': {'ui': {'kind': 'static', 'target': '/opt/client/dist'}, 'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}},
                'static_assets': {'ui': {'index.html': 'a' * 64}}, 'backends': {'api': backend},
                'caddy': {'unit': 'caddy.service', 'account': 'caddy', 'group': 'caddy', 'executable': '/usr/bin/caddy',
                          'argv': ['/usr/bin/caddy'], 'binary_digest': 'b' * 64, 'admin_group': 'caddy-admin'}}
    monkeypatch.setattr(policy, 'load_reviewed_manifest', lambda *_a, **_k: manifest)
    monkeypatch.setattr(origin, 'verify_caddy_identity', lambda **_k: (123, '456', 970, 970))
    monkeypatch.setattr(origin, 'verify_static_route_assets', lambda *_a, **_k: None)
    seen = []
    monkeypatch.setattr(origin, 'inspect_dynamic_backend', lambda **kw: (seen.append(kw), (124, '457', 971, 971))[1])
    monkeypatch.setattr(origin, 'inspect_loopback_listener', lambda **_kw: '12345')
    monkeypatch.setattr(origin_transport, 'verify_admin_group', lambda *_args: (995, 970, 970))
    monkeypatch.setattr(origin_transport, 'fetch_caddy_config', lambda *_a, **_k: config)
    assert policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)['unverified_routes'] == ['api']
    assert seen[0]['unit'] == 'client.service'
    identities = iter([(124, '457', 971, 971), (124, '458', 971, 971)])
    monkeypatch.setattr(origin, 'inspect_dynamic_backend', lambda **kw: next(identities))
    with pytest.raises(ValueError, match='Backend identity changed'):
        policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)
    backend['target'] = '127.0.0.1:9999'
    with pytest.raises(ValueError, match='differs from route'):
        policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)
    manifest['backends'] = {}
    with pytest.raises(ValueError, match='inventory'):
        policy.inspect_reviewed_static_origin('/manifest', expected_manifest_digest='c' * 64)


def test_listener_rejects_ambiguous_owners_namespaces_and_sockets(tmp_path, monkeypatch):
    root = tmp_path / 'proc'; root.mkdir()
    monkeypatch.setattr(origin.os, 'geteuid', lambda: 0)
    nsfile = tmp_path / 'netns'; nsfile.write_text('namespace')
    for pid in ('123', '456', 'self'):
        (root / pid / 'ns').mkdir(parents=True)
        os.link(nsfile, root / pid / 'ns' / 'net')
        os.link(nsfile, root / pid / 'ns' / 'pid')
    (root / 'self' / 'mountinfo').write_text(f'1 0 0:1 / {root} rw - proc proc rw\n')
    for pid in ('123', '456'):
        (root / pid / 'task' / pid / 'fd').mkdir(parents=True)
    (root / '123' / 'net').mkdir()
    header = '  sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n'
    row = '  0: 0100007F:1F4A 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 12345\n'
    tcp = root / '123' / 'net' / 'tcp'; tcp.write_text(header + row)
    header6 = header.replace('rem_address', 'remote_address')
    tcp6 = root / '123' / 'net' / 'tcp6'; tcp6.write_text(header6)
    os.symlink('socket:[12345]', root / '123' / 'task' / '123' / 'fd' / '5')
    args = {'target': '127.0.0.1:8010', 'backend_pid': 123, 'caddy_pid': 456, 'proc_root': root}
    assert origin.inspect_loopback_listener(**args) == '12345'
    os.symlink('socket:[12345]', root / '456' / 'task' / '456' / 'fd' / '8')
    with pytest.raises(RuntimeError): origin.inspect_loopback_listener(**args)
    (root / '456' / 'task' / '456' / 'fd' / '8').unlink()
    tcp.write_text(header + row + row.replace('12345', '12346'))
    with pytest.raises(RuntimeError): origin.inspect_loopback_listener(**args)
    tcp.write_text(header + row)
    tcp6.write_text(header6 + row.replace('0100007F', '0' * 32))
    with pytest.raises(RuntimeError): origin.inspect_loopback_listener(**args)
    tcp6.write_text(header6)
    (root / '456' / 'ns' / 'net').unlink()
    (root / '456' / 'ns' / 'net').write_text('other')
    with pytest.raises(RuntimeError, match='namespace'):
        origin.inspect_loopback_listener(**args)


def test_listener_checks_nonleader_task_fd(tmp_path, monkeypatch):
    root = tmp_path / 'proc'; root.mkdir()
    monkeypatch.setattr(origin.os, 'geteuid', lambda: 0)
    ns = tmp_path / 'ns'; ns.touch()
    for pid in ('123', '456', 'self'):
        (root / pid / 'ns').mkdir(parents=True)
        os.link(ns, root / pid / 'ns' / 'net')
        os.link(ns, root / pid / 'ns' / 'pid')
    (root / 'self' / 'mountinfo').write_text(f'1 0 0:1 / {root} rw - proc proc rw\n')
    for pid in ('123', '456'):
        (root / pid / 'task' / pid / 'fd').mkdir(parents=True)
    (root / '456' / 'task' / '457' / 'fd').mkdir(parents=True)
    (root / '123' / 'net').mkdir()
    header = 'sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n'
    row = '0: 0100007F:1F4A 00000000:0000 0A 0 0 0 0 0 12345\n'
    (root / '123' / 'net' / 'tcp').write_text(header + row)
    (root / '123' / 'net' / 'tcp6').write_text(header.replace('rem_address', 'remote_address'))
    os.symlink('socket:[12345]', root / '123' / 'task' / '123' / 'fd' / '5')
    args = dict(target='127.0.0.1:8010', backend_pid=123, caddy_pid=456, proc_root=root)
    assert origin.inspect_loopback_listener(**args) == '12345'
    os.symlink('socket:[12345]', root / '456' / 'task' / '457' / 'fd' / '6')
    with pytest.raises(RuntimeError):
        origin.inspect_loopback_listener(**args)


def test_listener_rejects_pid_namespace_mismatch(tmp_path, monkeypatch):
    # The mismatch is rejected before any listener table is consulted.
    root = tmp_path / 'proc'; root.mkdir()
    monkeypatch.setattr(origin.os, 'geteuid', lambda: 0)
    ns = tmp_path / 'ns'; ns.touch()
    for pid in ('123', '456', 'self'):
        (root / pid / 'ns').mkdir(parents=True)
        os.link(ns, root / pid / 'ns' / 'net')
        os.link(ns, root / pid / 'ns' / 'pid')
    (root / 'self' / 'mountinfo').write_text(f'1 0 0:1 / {root} rw - proc proc rw\n')
    (root / '456' / 'ns' / 'pid').unlink()
    (root / '456' / 'ns' / 'pid').touch()
    with pytest.raises(RuntimeError, match='namespace'):
        origin.inspect_loopback_listener('127.0.0.1:8010', backend_pid=123, caddy_pid=456, proc_root=root)


@pytest.mark.parametrize('mount', [
    '1 0 0:1 / {root} rw - proc proc rw,hidepid=2',
    '1 0 0:1 / {root} rw,hidepid=1 - proc proc rw',
    '1 0 0:1 / {root} rw - proc proc rw,subset=pid',
    '1 0 0:1 /private {root} rw - proc proc rw',
    '1 0 0:1 / {root} rw - tmpfs tmpfs rw',
    '1 0 0:1 / {root} rw - proc proc rw\n2 0 0:2 / {root} rw - proc proc rw',
    'bad mountinfo',
])
def test_proc_visibility_rejects_restricted_or_ambiguous_mounts(tmp_path, monkeypatch, mount):
    root = tmp_path / 'proc'; (root / 'self').mkdir(parents=True)
    monkeypatch.setattr(origin.os, 'geteuid', lambda: 0)
    (root / 'self' / 'mountinfo').write_text(mount.format(root=root) + '\n')
    with pytest.raises(RuntimeError):
        origin._verify_proc_visibility(root)


def test_proc_visibility_requires_root_and_accepts_unrestricted_proc(tmp_path, monkeypatch):
    root = tmp_path / 'proc'; (root / 'self').mkdir(parents=True)
    (root / 'self' / 'mountinfo').write_text(f'1 0 0:1 / {root} rw - proc proc rw,hidepid=0\n')
    monkeypatch.setattr(origin.os, 'geteuid', lambda: 0)
    origin._verify_proc_visibility(root)
    monkeypatch.setattr(origin.os, 'geteuid', lambda: 1000)
    with pytest.raises(RuntimeError, match='Privileged'):
        origin._verify_proc_visibility(root)
