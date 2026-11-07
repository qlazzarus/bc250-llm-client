import fcntl
import json
import os
import stat
from pathlib import Path


class LockBusyError(RuntimeError):
    """Another process currently holds the advisory lock."""


def protected_xattrs(fd):
    """Capture security-relevant metadata; failure to inspect is an error."""
    names = set(os.listxattr(fd))
    return {name: os.getxattr(fd, name) for name in ("security.selinux", "system.posix_acl_access")
            if name in names}


class FileLock:
    def __init__(self, path, *, secure_precreated=False):
        self.path = Path(path)
        self.secure_precreated = secure_precreated
        self.handle = None

    def acquire(self):
        if self.secure_precreated:
            handle = self._open_secure()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = self.path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise LockBusyError(f"Another process owns lock: {self.path}") from None
        except BaseException:
            handle.close()
            raise
        self.handle = handle

    def _open_secure(self):
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise RuntimeError("Secure GPU lock requires an absolute normalized path")
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            self._check_trusted_directory(os.fstat(directory))
            for part in self.path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
                os.close(directory)
                directory = child
                self._check_trusted_directory(os.fstat(directory))
            fd = os.open(self.path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=directory)
            try:
                self._check_trusted_file(os.fstat(fd))
                return os.fdopen(fd, "r+")
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(directory)

    @staticmethod
    def _check_trusted_directory(info):
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("GPU lock directories must be root-owned and not writable by others")

    @staticmethod
    def _check_trusted_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or info.st_mode & 0o007):
            raise RuntimeError("GPU lock file must be root-owned, private and regular")

    def release(self):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise ValueError("Path escapes data directory")
    return path


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
