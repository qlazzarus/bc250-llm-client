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
    if unit != 'caddy.service':
        raise ValueError('Invalid Caddy unit policy')
    return _verify_service_identity(unit=unit, account=account, group=group, executable=executable,
                                    argv=argv, binary_digest=binary_digest)


def _verify_service_identity(*, unit, account, group, executable, argv, binary_digest):
    """Verify a reviewed single-process service; this does not prove response provenance."""
    import grp
    import pwd
    if (not isinstance(unit, str) or not re.fullmatch(r'[a-zA-Z0-9_.@-]+\.service', unit)
            or not isinstance(argv, list) or not argv or argv[0] != executable):
        raise ValueError('Invalid service unit policy')
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


def inspect_dynamic_backend(*, unit, account, group, executable, argv, binary_digest, tree, files):
    """Inspect one reviewed backend and code tree, without claiming origin safety.

    The caller must independently trust the policy. Listening socket ownership,
    shared libraries, runtime inputs and every possible response remain unchecked.
    """
    args = dict(unit=unit, account=account, group=group, executable=executable,
                argv=argv, binary_digest=binary_digest)
    identity = _verify_service_identity(**args)
    verify_static_route_assets({'backend': {'kind': 'static', 'target': tree}},
                               {'backend': files}, caddy_pid=identity[0])
    if identity != _verify_service_identity(**args):
        raise RuntimeError('Backend identity changed during inspection')
    return identity


def _verify_proc_visibility(root):
    """Reject known restricted proc mounts; this cannot establish host visibility."""
    if os.geteuid() != 0:
        raise RuntimeError('Privileged proc inspection required')
    mountpoint = str(root)
    # An escaped mount point is deliberately unsupported instead of being guessed.
    if not mountpoint.startswith('/') or any(c in mountpoint for c in ' \t\n\\'):
        raise RuntimeError('Unsupported proc mount path')
    matches = []
    for line in (root / 'self' / 'mountinfo').read_text().splitlines():
        fields = line.split()
        if '-' not in fields:
            raise RuntimeError('Invalid mountinfo')
        separator = fields.index('-')
        if separator < 6 or len(fields) < separator + 4:
            raise RuntimeError('Invalid mountinfo')
        if fields[4] == mountpoint:
            matches.append((fields, separator))
    if len(matches) != 1:
        raise RuntimeError('Proc mount is missing or ambiguous')
    fields, separator = matches[0]
    options = set(fields[5].split(',')) | set(fields[separator + 3].split(','))
    if (fields[3] != '/' or fields[separator + 1] != 'proc'
            or any(option.startswith('hidepid=') and option != 'hidepid=0' for option in options)
            or 'hidepid' in options or any(option.startswith('subset=') for option in options)):
        raise RuntimeError('Restricted proc mount')


def inspect_loopback_listener(target, *, backend_pid, caddy_pid, proc_root=Path('/proc')):
    """Check one Linux TCP listener and its sole visible process owner.

    Requires a complete, privileged /proc view; inaccessible entries and
    observed changes fail closed, and separate task FD tables are inspected.
    PID namespace and hidepid visibility remain unproven; FD transfers and
    other races may escape detection, so this does not prove exclusive ownership.
    The snapshot cannot prevent later changes, authenticate HTTP responses,
    or establish the safety of a network namespace or runtime.
    """
    if (not isinstance(target, str) or not re.fullmatch(r'127\.0\.0\.1:[1-9][0-9]{0,4}', target)
            or int(target.split(':')[1]) > 65535
            or type(backend_pid) is not int or backend_pid <= 0
            or type(caddy_pid) is not int or caddy_pid <= 0):
        raise ValueError('Invalid listener policy')
    root = Path(proc_root)
    port = int(target.split(':')[1])
    _verify_proc_visibility(root)

    def namespace(pid, kind='net'):
        info = os.stat(root / str(pid) / 'ns' / kind)
        return info.st_dev, info.st_ino

    def listeners():
        found = []
        for protocol in ('tcp', 'tcp6'):
            lines = (root / str(backend_pid) / 'net' / protocol).read_text().splitlines()
            remote_header = 'rem_address' if protocol == 'tcp' else 'remote_address'
            if not lines or lines[0].split()[:4] != ['sl', 'local_address', remote_header, 'st']:
                raise RuntimeError('Invalid TCP listener table')
            for line in lines[1:]:
                fields = line.split()
                if len(fields) < 10 or not re.fullmatch(r'[0-9A-Fa-f]+:[0-9A-Fa-f]{4}', fields[1]):
                    raise RuntimeError('Invalid TCP listener entry')
                address, encoded_port = fields[1].split(':')
                if fields[3] != '0A' or int(encoded_port, 16) != port:
                    continue
                if protocol != 'tcp' or address.upper() != '0100007F' or not re.fullmatch(r'[1-9][0-9]*', fields[9]):
                    raise RuntimeError('Ambiguous listener address or inode')
                found.append(fields[9])
        if len(found) != 1:
            raise RuntimeError('Listener is absent or has multiple sockets')
        return found[0]

    ns = namespace(backend_pid)
    pid_ns = namespace('self', 'pid')
    if (namespace(backend_pid, 'pid') != pid_ns or namespace(caddy_pid, 'pid') != pid_ns
            or namespace(caddy_pid) != ns or namespace('self') != ns):
        raise RuntimeError('Listener network namespace mismatch')
    inode = listeners()
    def snapshot_owners():
        pids = {entry.name for entry in os.scandir(root) if entry.name.isdigit()}
        owners = set()
        tasks = {}
        for pid in pids:
            task_dir = root / pid / 'task'
            tids = {entry.name for entry in os.scandir(task_dir) if entry.name.isdigit()}
            if pid not in tids:
                raise RuntimeError('Process leader task is missing')
            tasks[pid] = tids
            for tid in tids:
                with os.scandir(task_dir / tid / 'fd') as entries:
                    for entry in entries:
                        if os.readlink(entry.path) == f'socket:[{inode}]':
                            owners.add(int(pid))
            if tids != {entry.name for entry in os.scandir(task_dir) if entry.name.isdigit()}:
                raise RuntimeError('Task inventory changed')
        if pids != {entry.name for entry in os.scandir(root) if entry.name.isdigit()}:
            raise RuntimeError('Process inventory changed')
        return pids, tasks, owners

    first = snapshot_owners()
    second = snapshot_owners()
    _verify_proc_visibility(root)
    if (first != second or first[2] != {backend_pid} or listeners() != inode
            or namespace(backend_pid) != ns or namespace(caddy_pid) != ns or namespace('self') != ns
            or any(namespace(pid, 'pid') != pid_ns for pid in (backend_pid, caddy_pid, 'self'))):
        raise RuntimeError('Listener owner or namespace is unstable or untrusted')
    return inode


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
