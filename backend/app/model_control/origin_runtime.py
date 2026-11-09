"""Conservative Linux process mapping inspection; not an activation verdict.

This snapshot can identify unreviewed mapped files and executable anonymous
memory, but cannot prove that a process has not loaded and unmapped code, that
its interpreter will not load other code later, or that kernel/runtime inputs
and response behavior are trustworthy. Callers must separately authenticate the
process identity and hold a deployment barrier across the intended operation.
Disk hashes and maps do not establish memory bytes: a private mapping can be
modified and later changed to RX, leaving the file and current permissions
unchanged. Runtime memory integrity requires independent evidence.
"""
import hashlib
import hmac
import os
import re
import stat
from pathlib import Path

from .client_trust import trusted_path

_LINE = re.compile(r'^([0-9a-f]+)-([0-9a-f]+) ([r-][w-][x-][ps]) ([0-9a-f]+) ([0-9a-f]+):([0-9a-f]+) ([0-9]+)(?: +(.+))?$')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def inspect_runtime_mappings(pid, reviewed_files, *, proc_root=Path('/proc')):
    """Compare all file-backed mappings to reviewed digests and host namespace.

    The result is a snapshot only. Unknown, deleted or anonymous executable
    mappings are rejected; Linux kernel [vdso] and [vsyscall] are exceptions,
    so kernel provenance is a separate prerequisite. All mapped files must be
    root-controlled, including non-executable mappings. Shared writable file
    mappings are unsupported because they can modify backing files. Only same
    mount namespace is supported. File paths containing whitespace are unsupported.
    """
    if type(pid) is not int or pid <= 0 or not isinstance(reviewed_files, dict) or not reviewed_files:
        raise ValueError('Invalid runtime policy')
    for name, digest in reviewed_files.items():
        if (not isinstance(name, str) or not name.startswith('/') or any(c.isspace() for c in name)
                or any(p in ('', '.', '..') for p in name[1:].split('/'))
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise ValueError('Invalid runtime file policy')
    root = Path(proc_root)
    proc = root / str(pid)

    def namespace(path):
        info = os.stat(path / 'ns' / 'mnt')
        return info.st_dev, info.st_ino

    def start_time():
        raw = (proc / 'stat').read_text()
        end = raw.rfind(')')
        fields = raw[end + 2:].split() if end >= 0 else []
        if len(fields) < 20 or not fields[19].isdigit():
            raise RuntimeError('Invalid process start time')
        return fields[19]

    start = start_time()
    ns = namespace(proc)
    if ns != namespace(root / 'self'):
        raise RuntimeError('Runtime mount namespace mismatch')
    before = (proc / 'maps').read_text()
    mapped = {}
    for line in before.splitlines():
        match = _LINE.fullmatch(line)
        if not match:
            raise RuntimeError('Unsupported process map')
        low, high, perms, _offset, major, minor, inode, name = match.groups()
        if int(low, 16) >= int(high, 16):
            raise RuntimeError('Invalid process map range')
        if 'w' in perms and 'x' in perms:
            raise RuntimeError('Writable executable memory is unsupported')
        if name is None or name.startswith('['):
            if 'x' in perms and name not in ('[vdso]', '[vsyscall]'):
                raise RuntimeError('Unreviewed executable memory')
            if int(inode) != 0:
                raise RuntimeError('Invalid anonymous map')
            continue
        if (not name.startswith('/') or any(c.isspace() for c in name)
                or name not in reviewed_files or int(inode) == 0):
            raise RuntimeError('Unreviewed process map')
        if 'w' in perms and perms[3] == 's':
            raise RuntimeError('Shared writable file mapping is unsupported')
        identity = (int(major, 16), int(minor, 16), int(inode))
        if name in mapped and mapped[name][0] != identity:
            raise RuntimeError('Inconsistent process map identity')
        mapped[name] = (identity, 'x' in perms or mapped.get(name, (None, False))[1])
    if set(mapped) != set(reviewed_files):
        raise RuntimeError('Runtime mapping inventory differs')
    for name, (identity, executable) in mapped.items():
        trusted_path(name, directory=False)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                    or info.st_mode & 0o022 or (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino) != identity):
                raise RuntimeError('Untrusted mapped file')
            remote = os.stat(proc / 'root' / name.lstrip('/'), follow_symlinks=False)
            if (remote.st_dev, remote.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError('Runtime sees different file')
            digest = hashlib.sha256()
            first = True
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                if first and executable and not block.startswith(b'\x7fELF'):
                    raise RuntimeError('Executable map is not ELF')
                first = False
                digest.update(block)
            after = os.fstat(fd)
            current = os.stat(name, follow_symlinks=False)
            if (first and executable or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or not hmac.compare_digest(digest.hexdigest(), reviewed_files[name])):
                raise RuntimeError('Mapped file changed or digest mismatch')
        finally:
            os.close(fd)
    if (before != (proc / 'maps').read_text() or start != start_time()
            or namespace(proc) != ns or namespace(root / 'self') != ns):
        raise RuntimeError('Runtime mappings or identity changed')
    return sorted(mapped)
