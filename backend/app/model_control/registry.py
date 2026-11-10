"""Read-only building blocks for a local root service.

The only accepted operations are read-only. Switching is deliberately unavailable
until lock, admission and crash recovery are enforced inside the privileged service.
"""

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import struct
from pathlib import Path

from .files import FileLock, protected_xattrs

MODEL_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_REQUEST = 4096


class ModelRegistry:
    def __init__(self, base=Path("/opt/ai/config")):
        self.base = Path(base)

    @staticmethod
    def _check_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise RuntimeError("Untrusted model configuration")

    def _open_base(self):
        if not self.base.is_absolute() or ".." in self.base.parts:
            raise RuntimeError("Invalid registry path")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            FileLock._check_trusted_directory(os.fstat(fd))
            for part in self.base.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
                os.close(fd)
                fd = child
                FileLock._check_trusted_directory(os.fstat(fd))
            return fd
        except BaseException:
            os.close(fd)
            raise

    def list_models(self):
        base = self._open_base()
        try:
            directory = os.open("models", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                os.O_CLOEXEC, dir_fd=base)
            try:
                FileLock._check_trusted_directory(os.fstat(directory))
                models = []
                for filename in os.listdir(directory):
                    if not filename.endswith(".conf"):
                        continue
                    name = filename[:-5]
                    if not MODEL_NAME.fullmatch(name):
                        raise RuntimeError("Invalid model configuration name")
                    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=directory)
                    try:
                        self._check_file(os.fstat(fd))
                    finally:
                        os.close(fd)
                    models.append(name)
                return sorted(models)
            finally:
                os.close(directory)
        finally:
            os.close(base)

    def current_model(self):
        base = self._open_base()
        try:
            fd = os.open("active-model.conf", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=base)
            try:
                self._check_file(os.fstat(fd))
                value = os.read(fd, 4097)
            finally:
                os.close(fd)
        finally:
            os.close(base)
        match = re.fullmatch(rb"ACTIVE_MODEL=([A-Za-z0-9_-]{1,128})\n?", value)
        if not match:
            raise RuntimeError("Invalid active model configuration")
        name = match.group(1).decode("ascii")
        if name not in self.list_models():
            raise RuntimeError("Active model is not installed")
        return name

    def _model_config(self, name):
        """Read a protected literal configuration without evaluating shell."""
        if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
            raise ValueError("Invalid model name")
        base = self._open_base()
        try:
            directory = os.open("models", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                os.O_CLOEXEC, dir_fd=base)
            try:
                FileLock._check_trusted_directory(os.fstat(directory))
                fd = os.open(name + ".conf", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=directory)
                try:
                    self._check_file(os.fstat(fd))
                    raw = os.read(fd, 16385)
                finally:
                    os.close(fd)
            finally:
                os.close(directory)
        finally:
            os.close(base)
        if len(raw) > 16384:
            raise RuntimeError("Model configuration exceeds size limit")
        values = {}
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise RuntimeError("Invalid model configuration") from error
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([A-Za-z0-9_+./:-]*)", line)
            if not match or match[1] in values:
                raise RuntimeError("Unsupported model configuration syntax")
            values[match[1]] = match[2]
        path = values.get("MODEL_PATH", "")
        if (not path.startswith("/") or os.path.normpath(path) != path
                or not path.endswith(".gguf") or values.get("MODEL_NAME", name) != name):
            raise RuntimeError("Invalid model configuration mapping")
        return values, hashlib.sha256(raw).hexdigest()

    def model_id(self, name):
        """Expected llama ID; this does not assert that it is loaded."""
        return self._model_config(name)[0]["MODEL_PATH"]

    def _capability_evidence(self, name):
        """Read optional administrator-supplied verification, never create it."""
        base = self._open_base()
        try:
            try:
                directory = os.open("capabilities", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=base)
            except FileNotFoundError:
                return None
            try:
                FileLock._check_trusted_directory(os.fstat(directory))
                try:
                    fd = os.open(name + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                except FileNotFoundError:
                    return None
                try:
                    self._check_file(os.fstat(fd))
                    raw = os.read(fd, 16385)
                finally:
                    os.close(fd)
            finally:
                os.close(directory)
        finally:
            os.close(base)
        if len(raw) > 16384:
            raise RuntimeError("Capability evidence too large")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError("Invalid capability evidence") from error
        if not isinstance(value, dict):
            raise RuntimeError("Invalid capability evidence")
        return value

    def model_profile(self, name):
        """Read model identity and configured upper bounds from one config read."""
        values, digest = self._model_config(name)
        def positive(field):
            raw = values.get(field, "")
            if not re.fullmatch(r"[1-9][0-9]{0,9}", raw) or int(raw) > 2147483647:
                raise RuntimeError("Invalid model context configuration")
            return int(raw)
        result = {"name": name, "model_id": values["MODEL_PATH"],
                  "configured_context": positive("CONTEXT_SIZE"),
                  "parallel": positive("PARALLEL"), "config_sha256": digest,
                  "capabilities": {"reasoning_control": "unknown", "json_schema": "unknown"},
                  "runtime_identity": None}
        evidence = self._capability_evidence(name)
        if evidence is None:
            return result
        try:
            info = os.stat(values["MODEL_PATH"], follow_symlinks=False)
            fingerprint = {key: getattr(info, 'st_' + key) for key in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError("Invalid model file")
            capabilities = evidence["capabilities"]
            runtime = evidence["runtime_identity"]
            model_file = evidence["model_file"]
            if (not isinstance(model_file, dict) or set(model_file) != set(fingerprint)
                    or any(type(value) is not int or value < 0 for value in model_file.values())
                    or set(evidence) != {"model_id", "config_sha256", "model_file", "runtime_identity", "capabilities"}
                    or evidence["model_id"] != result["model_id"] or evidence["config_sha256"] != digest
                    or evidence["model_file"] != fingerprint
                    or not isinstance(capabilities, dict) or set(capabilities) != {"reasoning_control", "json_schema"}
                    or any(value not in ("unknown", "supported", "unsupported") for value in capabilities.values())
                    or not isinstance(runtime, dict) or set(runtime) != {"build_info", "chat_template_sha256", "runtime_settings_sha256"}
                    or not isinstance(runtime["build_info"], str) or not 0 < len(runtime["build_info"]) <= 256
                    or not isinstance(runtime["chat_template_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", runtime["chat_template_sha256"])
                    or not isinstance(runtime["runtime_settings_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", runtime["runtime_settings_sha256"])):
                raise ValueError("Stale capability evidence")
        except (KeyError, TypeError, ValueError, OSError):
            # Stale or unrecognised evidence cannot assert feature support.
            return result
        result["capabilities"] = capabilities
        result["runtime_identity"] = runtime
        return result


class ActiveModelWriter:
    """Candidate root-owned config writer; caller must hold the shared GPU lock."""

    def __init__(self, registry):
        self.registry = registry

    def validate_replacement(self):
        """Check replacement metadata before admission is paused or a journal is written."""
        base = self.registry._open_base()
        temporary = ".active-model-" + secrets.token_hex(16)
        try:
            current = os.open("active-model.conf", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=base)
            try:
                metadata = os.fstat(current)
                self.registry._check_file(metadata)
                attributes = protected_xattrs(current)
            finally:
                os.close(current)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         os.O_CLOEXEC, 0o600, dir_fd=base)
            try:
                os.fchown(fd, metadata.st_uid, metadata.st_gid)
                os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
                if protected_xattrs(fd) != attributes:
                    raise RuntimeError("Model configuration ACL or SELinux label would change")
            finally:
                os.close(fd)
                os.unlink(temporary, dir_fd=base)
        finally:
            os.close(base)

    def set_active(self, name):
        if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
            raise ValueError("Invalid model name")
        if name not in self.registry.list_models():
            raise ValueError("Unknown model")
        model_path = self.registry.model_id(name)
        info = os.stat(model_path)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise RuntimeError("Model file missing or empty")
        base = self.registry._open_base()
        temporary = ".active-model-" + secrets.token_hex(16)
        try:
            current = os.open("active-model.conf", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=base)
            try:
                metadata = os.fstat(current)
                self.registry._check_file(metadata)
                attributes = protected_xattrs(current)
            finally:
                os.close(current)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         os.O_CLOEXEC, 0o600, dir_fd=base)
            try:
                os.fchown(fd, metadata.st_uid, metadata.st_gid)
                os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
                if protected_xattrs(fd) != attributes:
                    raise RuntimeError("Model configuration ACL or SELinux label would change")
                with os.fdopen(fd, "w") as file:
                    fd = -1
                    file.write(f"ACTIVE_MODEL={name}\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, "active-model.conf", src_dir_fd=base, dst_dir_fd=base)
                os.fsync(base)
            finally:
                if fd != -1:
                    os.close(fd)
                try:
                    os.unlink(temporary, dir_fd=base)
                except FileNotFoundError:
                    pass
        finally:
            os.close(base)

