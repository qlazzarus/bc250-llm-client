"""Caddy identity and static asset checks, not an origin safety verdict."""
import hashlib
import hmac
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

from .client_trust import trusted_entry, trusted_path, verify_asset_tree


def verify_caddy_identity(*, unit, account, group, executable, argv, binary_digest):
    """Check a settled unit, process credentials and reviewed executable.

    The caller supplies independently reviewed fixed policy, not request data.
    This does not verify shared libraries, dynamic responses, or origin safety.
    """
    import grp
    import pwd
    if unit != 'caddy.service' or not isinstance(argv, list) or not argv or argv[0] != executable:
        raise ValueError('Invalid Caddy unit policy')
    if (not isinstance(binary_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', binary_digest)
            or not isinstance(executable, str) or not executable.startswith('/')
            or any(not isinstance(arg, str) or not arg for arg in argv)):
        raise ValueError('Invalid Caddy executable policy')
    uid, gid = pwd.getpwnam(account).pw_uid, grp.getgrnam(group).gr_gid
    if uid == 0 or gid == 0:
        raise RuntimeError('Caddy must use a dedicated non-root account')
    properties = 'LoadState,ActiveState,MainPID,Job,User,Group,NoNewPrivileges,FragmentPath,DropInPaths,EnvironmentFiles,ExecStart'
    result = subprocess.run(['/usr/bin/systemctl', 'show', '--all', '--property=' + properties, unit],
                            check=True, capture_output=True, text=True, timeout=10)
    rows = [line.partition('=') for line in result.stdout.splitlines()]
    if any(not sep for _, sep, _ in rows) or len({key for key, _, _ in rows}) != len(rows):
        raise RuntimeError('Invalid Caddy unit properties')
    values = {key: value for key, _, value in rows}
    if (set(values) != set(properties.split(',')) or values['LoadState'] != 'loaded'
            or values['ActiveState'] != 'active' or values['Job'] or values['User'] != account
            or values['Group'] != group or values['NoNewPrivileges'] != 'yes'
            or values['EnvironmentFiles'] or not re.fullmatch(r'[1-9][0-9]*', values['MainPID'])):
        raise RuntimeError('Caddy unit is not trusted and settled')
    starts = re.findall(r'(?:^|;)\s*argv\[\]=([^;]+)', values['ExecStart'])
    if len(starts) != 1 or shlex.split(starts[0].strip()) != argv:
        raise RuntimeError('Unexpected Caddy unit command')
    for path in [values['FragmentPath'], *shlex.split(values['DropInPaths'])]:
        trusted_path(path, directory=False)
    trusted_path(executable, directory=False)
    pid = int(values['MainPID']); proc = Path('/proc') / str(pid)
    status_rows = [line.partition(':') for line in (proc / 'status').read_text().splitlines()]
    status = {key: value.strip() for key, sep, value in status_rows if sep}
    if (status.get('Uid', '').split() != [str(uid)] * 4 or status.get('Gid', '').split() != [str(gid)] * 4
            or status.get('NoNewPrivs') != '1'):
        raise RuntimeError('Caddy process credentials mismatch')
    raw = (proc / 'stat').read_text(); end = raw.rfind(')')
    parts = raw[end + 2:].split() if end >= 0 else []
    if len(parts) < 20 or not parts[19].isdigit():
        raise RuntimeError('Invalid Caddy process start time')
    if (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0') != [arg.encode() for arg in argv]:
        raise RuntimeError('Caddy process command mismatch')
    fd = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022):
            raise RuntimeError('Untrusted Caddy executable')
        process_exe = os.stat(proc / 'exe')
        ns_exe = trusted_entry(proc / 'root' / executable.lstrip('/'), directory=False)
        if any((entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino) for entry in (process_exe, ns_exe)):
            raise RuntimeError('Caddy executable differs in process namespace')
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block: break
            digest.update(block)
        after = os.fstat(fd)
        if ((info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or not hmac.compare_digest(digest.hexdigest(), binary_digest)):
            raise RuntimeError('Caddy executable digest mismatch')
    finally:
        os.close(fd)
    return (pid, parts[19], uid, gid)


def verify_static_route_assets(routes, reviewed_assets, *, caddy_pid):
    """Compare reviewed static files to Caddy's namespace; dynamic routes stay unverified."""
    if type(caddy_pid) is not int or caddy_pid <= 0 or not isinstance(routes, dict) or not isinstance(reviewed_assets, dict):
        raise ValueError('Invalid static asset policy')
    static = {key: value['target'] for key, value in routes.items()
              if isinstance(value, dict) and value.get('kind') == 'static'}
    if not static or set(static) != set(reviewed_assets):
        raise RuntimeError('Static route inventory and assets differ')
    proc_root = Path('/proc') / str(caddy_pid) / 'root'
    for route_id, root_text in static.items():
        root = Path(root_text)
        if not root.is_absolute() or '..' in root.parts:
            raise RuntimeError('Invalid static asset root')
        expected = reviewed_assets[route_id]
        verify_asset_tree(root, expected)
        current = Path('/')
        # Compare every ancestor too: a mount or bind overlay can change lookup.
        for part in root.parts[1:]:
            current /= part
            local = trusted_entry(current, directory=True)
            remote = trusted_entry(proc_root / str(current).lstrip('/'), directory=True)
            if (local.st_dev, local.st_ino) != (remote.st_dev, remote.st_ino):
                raise RuntimeError('Caddy sees a different asset path')
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
            directory = Path(directory)
            remote_dir = proc_root / str(directory).lstrip('/')
            if set(os.listdir(remote_dir)) != set(dirs + files):
                raise RuntimeError('Caddy sees different asset entries')
            for name in dirs + files:
                path = directory / name
                local = trusted_entry(path, directory=name in dirs)
                remote = trusted_entry(proc_root / str(path).lstrip('/'), directory=name in dirs)
                if (local.st_dev, local.st_ino) != (remote.st_dev, remote.st_ino):
                    raise RuntimeError('Caddy sees different asset files')
        verify_asset_tree(root, expected)
