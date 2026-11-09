"""Verify the dedicated Client process and its root-controlled execution tree."""
import os
import hashlib
import hmac
import re
import shlex
import stat
import subprocess
from pathlib import Path

CLIENT_ROOT = Path('/opt/bc250-llm-client-runtime')
CLIENT_BOOTSTRAP = CLIENT_ROOT / 'client-bootstrap.py'
CLIENT_ENV = Path('/etc/bc250-llm-client.env')
CLIENT_UNIT = 'bc250-llm-client.service'
CLIENT_ARGV = ['/usr/bin/python3', '-I', '-S', str(CLIENT_BOOTSTRAP)]
MAX_ASSET_SIZE = 32 * 1024 * 1024
MAX_TOTAL_ASSET_SIZE = 512 * 1024 * 1024
MAX_ASSETS = 20000


def verify_operator_frontend():
    """Do not activate while the operator UI's serving origin is unverified.

    M4-04 must replace this guard with a locally tested verifier of the
    effective reverse-proxy route and root-controlled assets. M7 provisions
    that contract. A boolean opt-in is insufficient.
    """
    raise RuntimeError('Operator frontend provenance is not verified')


def trusted_entry(path, *, directory):
    info = os.lstat(path)
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not correct_type or info.st_uid != 0 or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise RuntimeError('Untrusted Client installation')
    return info


def trusted_path(path, *, directory):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('Invalid Client installation path')
    current = Path('/')
    trusted_entry(current, directory=True)
    for index, part in enumerate(path.parts[1:]):
        current /= part
        trusted_entry(current, directory=directory if index == len(path.parts) - 2 else True)


def verify_asset_tree(root, expected):
    """Compare every root-owned asset with an independently trusted manifest.

    This is only one building block: it does not validate a manifest's source,
    the effective reverse proxy, dynamic responses, or same-origin services.
    """
    root = Path(root)
    trusted_path(root, directory=True)
    if not isinstance(expected, dict) or not expected or len(expected) > MAX_ASSETS:
        raise RuntimeError('Invalid asset manifest')
    expected_dirs = set()
    for name, digest in expected.items():
        if (not isinstance(name, str) or not name or name.startswith('/') or '\\' in name
                or any(part in {'', '.', '..'} for part in name.split('/'))
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise RuntimeError('Invalid asset manifest')
        parts = name.split('/')
        expected_dirs.update('/'.join(parts[:index]) for index in range(1, len(parts)))
    found = set()
    total_size = 0
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        for name in dirs:
            path = Path(directory) / name
            trusted_entry(path, directory=True)
            if path.relative_to(root).as_posix() not in expected_dirs:
                raise RuntimeError('Unexpected asset directory')
        for name in files:
            path = Path(directory) / name
            info = trusted_entry(path, directory=False)
            relative = path.relative_to(root).as_posix()
            if relative not in expected or info.st_size > MAX_ASSET_SIZE:
                raise RuntimeError('Unexpected or oversized asset')
            total_size += info.st_size
            if total_size > MAX_TOTAL_ASSET_SIZE:
                raise RuntimeError('Asset tree exceeds size limit')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                    raise RuntimeError('Asset changed during verification')
                digest = hashlib.sha256()
                with os.fdopen(fd, 'rb') as stream:
                    fd = -1
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                if not hmac.compare_digest(digest.hexdigest(), expected[relative]):
                    raise RuntimeError('Asset digest mismatch')
            finally:
                if fd != -1:
                    os.close(fd)
            found.add(relative)
            if len(found) > MAX_ASSETS:
                raise RuntimeError('Too many assets')
    if found != set(expected):
        raise RuntimeError('Missing asset')


def verify_client_tree(root=CLIENT_ROOT, *, process_root=None):
    """Reject writable, linked or namespace-divergent executable inputs."""
    root = Path(root)
    trusted_path(root, directory=True)
    if process_root is not None:
        remote_root = Path(process_root)
        local_info = trusted_entry(root, directory=True)
        remote_info = trusted_entry(remote_root, directory=True)
        if (local_info.st_dev, local_info.st_ino) != (remote_info.st_dev, remote_info.st_ino):
            raise RuntimeError('Client sees a different installation root')
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        if process_root is not None:
            remote_directory = remote_root / Path(directory).relative_to(root)
            if set(os.listdir(remote_directory)) != set(dirs + files):
                raise RuntimeError('Client sees different installation entries')
        for name in dirs + files:
            path = Path(directory) / name
            is_directory = name in dirs
            if not is_directory and name.endswith(('.pth', '.pyc')):
                raise RuntimeError('Untrusted Client startup file')
            local_info = trusted_entry(path, directory=is_directory)
            if process_root is not None:
                remote_info = trusted_entry(remote_root / path.relative_to(root), directory=is_directory)
                if (local_info.st_dev, local_info.st_ino) != (remote_info.st_dev, remote_info.st_ino):
                    raise RuntimeError('Client sees different installation files')
    trusted_path(root / 'client-bootstrap.py', directory=False)
    trusted_path(root / 'lib', directory=True)


def verify_client_process(pid):
    """Fail closed unless unit, credentials, interpreter and code tree are trusted."""
    result = subprocess.run(['/usr/bin/systemctl', 'show', '--all', '--property='
        'FragmentPath,DropInPaths,EnvironmentFiles,WorkingDirectory,NoNewPrivileges,ExecStart', CLIENT_UNIT],
        check=True, capture_output=True, text=True, timeout=10)
    pairs = [line.partition('=') for line in result.stdout.splitlines()]
    if any(not sep for _, sep, _ in pairs) or len({key for key, _, _ in pairs}) != len(pairs):
        raise RuntimeError('Invalid Client unit properties')
    data = {key: value for key, _, value in pairs}
    required = {'FragmentPath', 'DropInPaths', 'EnvironmentFiles', 'WorkingDirectory', 'NoNewPrivileges', 'ExecStart'}
    if set(data) != required or data['WorkingDirectory'] != str(CLIENT_ROOT) or data['NoNewPrivileges'] != 'yes':
        raise RuntimeError('Client unit is not isolated')
    if data['EnvironmentFiles'] != f'{CLIENT_ENV} (ignore_errors=no)':
        raise RuntimeError('Unexpected Client environment file')
    starts = re.findall(r'(?:^|;)\s*argv\[\]=([^;]+)', data['ExecStart'])
    if len(starts) != 1 or shlex.split(starts[0].strip()) != CLIENT_ARGV:
        raise RuntimeError('Unexpected Client unit command')
    for path in [data['FragmentPath'], *shlex.split(data['DropInPaths']), str(CLIENT_ENV)]:
        trusted_path(path, directory=False)
    proc = Path('/proc') / str(pid)
    argv = proc.joinpath('cmdline').read_bytes().rstrip(b'\0').split(b'\0')
    if argv != [part.encode() for part in CLIENT_ARGV]:
        raise RuntimeError('Unexpected Client process command')
    trusted_path(Path('/usr/bin/python3').resolve(strict=True), directory=False)
    expected, actual = os.stat('/usr/bin/python3'), os.stat(proc / 'exe')
    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
        raise RuntimeError('Unexpected Client interpreter')
    host_cwd, proc_cwd = os.stat(CLIENT_ROOT), os.stat(proc / 'cwd')
    if (host_cwd.st_dev, host_cwd.st_ino) != (proc_cwd.st_dev, proc_cwd.st_ino):
        raise RuntimeError('Unexpected Client working directory')
    verify_client_tree(CLIENT_ROOT, process_root=proc / 'root' / str(CLIENT_ROOT).lstrip('/'))


def verify_runtime_trees(pid, reviewed_trees):
    """Compare reviewed runtime trees with the target process's mount view.

    The caller must supply a complete, independently reviewed manifest for
    every runtime input (including libraries loaded later). This is a building
    block, not proof of complete runtime provenance: it does not inspect ACLs,
    writable mounts, process memory, loader configuration or mapped objects,
    and does not pin the process or mounts against concurrent changes.
    """
    if type(pid) is not int or pid <= 0 or not isinstance(reviewed_trees, dict) or not reviewed_trees:
        raise ValueError('Invalid runtime tree policy')
    proc_root = Path('/proc') / str(pid) / 'root'
    for root_text, manifest in reviewed_trees.items():
        if (not isinstance(root_text, str) or not root_text.startswith('/')
                or root_text == '/' or any(part in {'', '.', '..'} for part in root_text[1:].split('/'))):
            raise ValueError('Invalid runtime tree path')
        root = Path(root_text)
        verify_asset_tree(root, manifest)
        current = Path('/')
        for part in root.parts[1:]:
            current /= part
            local = trusted_entry(current, directory=True)
            remote = trusted_entry(proc_root / str(current).lstrip('/'), directory=True)
            if (local.st_dev, local.st_ino) != (remote.st_dev, remote.st_ino):
                raise RuntimeError('Process sees a different runtime path')
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            directory = Path(directory)
            remote_dir = proc_root / str(directory).lstrip('/')
            if set(os.listdir(remote_dir)) != set(dirs + files):
                raise RuntimeError('Process sees different runtime entries')
            for name in dirs + files:
                path = directory / name
                local = trusted_entry(path, directory=name in dirs)
                remote = trusted_entry(proc_root / str(path).lstrip('/'), directory=name in dirs)
                if (local.st_dev, local.st_ino) != (remote.st_dev, remote.st_ino):
                    raise RuntimeError('Process sees different runtime files')
        verify_asset_tree(root, manifest)
