import os
import hashlib
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.model_control import client_trust, runtime


def local_tree_checks(monkeypatch):
    real_entry = client_trust.trusted_entry
    def local_entry(path, *, directory):
        info = os.lstat(path)
        if info.st_uid == os.getuid():
            kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            if not kind or info.st_mode & 0o022 or (not directory and info.st_nlink != 1):
                raise RuntimeError('Untrusted test entry')
            return info
        return real_entry(path, directory=directory)
    monkeypatch.setattr(client_trust, 'trusted_entry', local_entry)
    monkeypatch.setattr(client_trust, 'trusted_path', lambda path, *, directory: local_entry(path, directory=directory))


def test_client_runtime_tree_rejects_writable_symlink_and_namespace_mismatch(tmp_path, monkeypatch):
    local_tree_checks(monkeypatch)
    root = tmp_path / 'runtime'; root.mkdir(mode=0o755)
    (root / 'lib').mkdir(mode=0o755)
    bootstrap = root / 'client-bootstrap.py'; bootstrap.write_text('pass'); bootstrap.chmod(0o644)
    code = root / 'lib' / 'app.py'; code.write_text('pass'); code.chmod(0o644)
    client_trust.verify_client_tree(root, process_root=root)
    code.chmod(0o666)
    with pytest.raises(RuntimeError): client_trust.verify_client_tree(root, process_root=root)
    code.chmod(0o644)
    (root / 'lib' / 'alias').symlink_to(code)
    with pytest.raises(RuntimeError): client_trust.verify_client_tree(root, process_root=root)
    (root / 'lib' / 'alias').unlink()
    (root / 'lib' / 'startup.pth').write_text('')
    with pytest.raises(RuntimeError): client_trust.verify_client_tree(root, process_root=root)
    (root / 'lib' / 'startup.pth').unlink()
    different = tmp_path / 'different'; different.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match='different installation root'):
        client_trust.verify_client_tree(root, process_root=different)


def test_asset_tree_requires_exact_trusted_content(tmp_path, monkeypatch):
    local_tree_checks(monkeypatch)
    root = tmp_path / 'dist'; root.mkdir(mode=0o755)
    assets = root / 'assets'; assets.mkdir(mode=0o755)
    index = root / 'index.html'; index.write_text('app'); index.chmod(0o644)
    script = assets / 'app.js'; script.write_text('script'); script.chmod(0o644)
    digest = lambda data: hashlib.sha256(data.encode()).hexdigest()
    expected = {'index.html': digest('app'), 'assets/app.js': digest('script')}
    client_trust.verify_asset_tree(root, expected)
    script.write_text('tampered')
    with pytest.raises(RuntimeError, match='digest mismatch'):
        client_trust.verify_asset_tree(root, expected)
    script.write_text('script')
    script.chmod(0o666)
    with pytest.raises(RuntimeError): client_trust.verify_asset_tree(root, expected)
    script.chmod(0o644)
    script.unlink()
    script.symlink_to(index)
    with pytest.raises(RuntimeError): client_trust.verify_asset_tree(root, expected)
    script.unlink()
    script.write_text('script'); script.chmod(0o644)
    extra = root / 'extra'; extra.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match='Unexpected asset directory'):
        client_trust.verify_asset_tree(root, expected)
    extra.rmdir()
    script.unlink()
    with pytest.raises(RuntimeError, match='Missing asset'):
        client_trust.verify_asset_tree(root, expected)


@pytest.mark.parametrize('entry', ['/absolute.js', '../escape.js', 'nested/../escape.js', 'bad\\path.js', ''])
def test_asset_manifest_rejects_invalid_paths(tmp_path, monkeypatch, entry):
    local_tree_checks(monkeypatch)
    root = tmp_path / 'dist'; root.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match='Invalid asset manifest'):
        client_trust.verify_asset_tree(root, {entry: 'a' * 64})


def test_client_service_account_allows_login_user_but_rejects_root(monkeypatch):
    monkeypatch.setattr(runtime.pwd, 'getpwnam', lambda _name: SimpleNamespace(pw_uid=1000))
    assert runtime.client_service_uid() == 1000
    monkeypatch.setattr(runtime.pwd, 'getpwnam', lambda _name: SimpleNamespace(pw_uid=0))
    with pytest.raises(RuntimeError):
        runtime.client_service_uid()


@pytest.mark.parametrize('fault', [None, 'env', 'unit_argv', 'process_argv', 'no_new_privileges'])
def test_client_process_requires_exact_unit_and_process_contract(tmp_path, monkeypatch, fault):
    root = tmp_path / 'runtime'; root.mkdir()
    monkeypatch.setattr(client_trust, 'CLIENT_ROOT', root)
    monkeypatch.setattr(client_trust, 'CLIENT_ENV', Path('/etc/client.env'))
    expected_argv = ['/usr/bin/python3', '-I', '-S', str(root / 'client-bootstrap.py')]
    monkeypatch.setattr(client_trust, 'CLIENT_ARGV', expected_argv)
    unit_argv = expected_argv if fault != 'unit_argv' else ['/usr/bin/python3', '/tmp/untrusted.py']
    props = {
        'FragmentPath': '/etc/systemd/system/bc250-llm-client.service',
        'DropInPaths': '/etc/systemd/system/bc250-llm-client.service.d/identity.conf',
        'EnvironmentFiles': '/tmp/untrusted.env (ignore_errors=no)' if fault == 'env' else '/etc/client.env (ignore_errors=no)',
        'WorkingDirectory': str(root),
        'NoNewPrivileges': 'no' if fault == 'no_new_privileges' else 'yes',
        'ExecStart': '{ path=/usr/bin/python3 ; argv[]=' + ' '.join(unit_argv) + ' ; ignore_errors=no ; }',
    }
    monkeypatch.setattr(client_trust.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='\n'.join(f'{key}={value}' for key, value in props.items())))
    checked = []
    monkeypatch.setattr(client_trust, 'trusted_path', lambda path, *, directory: checked.append((str(path), directory)))
    monkeypatch.setattr(client_trust, 'verify_client_tree', lambda path, *, process_root: checked.append((str(path), str(process_root))))
    real_read = Path.read_bytes
    def read_bytes(path):
        if str(path) == '/proc/777/cmdline':
            argv = ['/usr/bin/python3', '/tmp/untrusted.py'] if fault == 'process_argv' else expected_argv
            return b'\0'.join(part.encode() for part in argv) + b'\0'
        return real_read(path)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    real_stat = os.stat
    def mapped_stat(path, *args, **kwargs):
        if str(path) == '/proc/777/exe': path = '/usr/bin/python3'
        if str(path) == '/proc/777/cwd': path = root
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(client_trust.os, 'stat', mapped_stat)
    if fault is None:
        client_trust.verify_client_process(777)
        assert any('/proc/777/root/' in item[1] for item in checked if isinstance(item[1], str))
    else:
        with pytest.raises(RuntimeError): client_trust.verify_client_process(777)


@pytest.mark.parametrize('mode,owner,group', [(0o666, 0, 100), (0o660, 1001, 100), (0o660, 0, 101)])
def test_inherited_socket_rejects_wrong_mode_owner_or_group(monkeypatch, mode, owner, group):
    class Listener:
        family = runtime.socket.AF_UNIX
        type = runtime.socket.SOCK_STREAM
        def getsockname(self): return runtime.SOCKET
        def getsockopt(self, *_args): return 1
        def close(self): pass
    monkeypatch.setenv('LISTEN_PID', str(os.getpid()))
    monkeypatch.setenv('LISTEN_FDS', '1')
    monkeypatch.setattr(runtime.socket, 'socket', lambda **kwargs: Listener())
    monkeypatch.setattr(runtime, 'trusted_path', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.grp, 'getgrnam', lambda _name: SimpleNamespace(gr_gid=100))
    monkeypatch.setattr(runtime.os, 'lstat', lambda _path: SimpleNamespace(st_mode=stat.S_IFSOCK | mode, st_uid=owner, st_gid=group))
    with pytest.raises(RuntimeError, match='socket permissions'): runtime.inherited_socket()


def test_inherited_socket_accepts_verified_activation_contract(monkeypatch):
    class Listener:
        family = runtime.socket.AF_UNIX
        type = runtime.socket.SOCK_STREAM
        blocking = True
        def getsockname(self): return runtime.SOCKET
        def getsockopt(self, *_args): return 1
        def setblocking(self, value): self.blocking = value
        def close(self): pass
    listener = Listener()
    monkeypatch.setenv('LISTEN_PID', str(os.getpid()))
    monkeypatch.setenv('LISTEN_FDS', '1')
    monkeypatch.setattr(runtime.socket, 'socket', lambda **kwargs: listener)
    monkeypatch.setattr(runtime, 'trusted_path', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.grp, 'getgrnam', lambda _name: SimpleNamespace(gr_gid=100))
    monkeypatch.setattr(runtime.os, 'lstat', lambda _path: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_gid=100))
    assert runtime.inherited_socket() is listener and listener.blocking is False


def test_runtime_tree_compares_manifest_and_process_namespace(tmp_path, monkeypatch):
    local_tree_checks(monkeypatch)
    root = tmp_path / 'stdlib'; root.mkdir()
    module = root / 'module.py'; module.write_text('trusted'); module.chmod(0o644)
    manifest = {'module.py': hashlib.sha256(b'trusted').hexdigest()}
    real_entry = client_trust.trusted_entry
    real_listdir = os.listdir
    remote_prefix = '/proc/123/root'
    def namespace_path(path):
        path = str(path)
        return path[len(remote_prefix):] if path.startswith(remote_prefix + '/') else path
    monkeypatch.setattr(client_trust, 'trusted_entry', lambda path, *, directory: real_entry(namespace_path(path), directory=directory))
    monkeypatch.setattr(client_trust.os, 'listdir', lambda path: real_listdir(namespace_path(path)))
    client_trust.verify_runtime_trees(123, {str(root): manifest})
    module.write_text('tampered')
    with pytest.raises(RuntimeError, match='digest mismatch'):
        client_trust.verify_runtime_trees(123, {str(root): manifest})
    module.write_text('trusted')
    def mismatched(path, *, directory):
        info = real_entry(namespace_path(path), directory=directory)
        if str(path) == remote_prefix + str(module):
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
        return info
    monkeypatch.setattr(client_trust, 'trusted_entry', mismatched)
    with pytest.raises(RuntimeError, match='different runtime files'):
        client_trust.verify_runtime_trees(123, {str(root): manifest})


@pytest.mark.parametrize('pid,trees', [(0, {'/usr/lib': {}}), (True, {'/usr/lib': {}}), (1, {}), (1, {'/usr/../lib': {}}), (1, {'/usr//lib': {}})])
def test_runtime_tree_rejects_invalid_policy(pid, trees):
    with pytest.raises(ValueError):
        client_trust.verify_runtime_trees(pid, trees)
