"""Read-only Studio contract for an unresolved external model switch.

This module does not import or run the model-management worker. Keep its journal
format compatible with the controller maintained in bc250-llm-client.
"""

import json
import os
import re
import stat
from pathlib import Path

from .files import FileLock

MODEL_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class SwitchRecoveryRequired(RuntimeError):
    pass


def decode_switch_journal(data):
    if len(data) > 8192:
        raise SwitchRecoveryRequired("Switch journal exceeds size limit")
    state = json.loads(data)
    if state == {}:
        return state
    base_fields = {"hold_id", "old", "target", "stage", "mutation_started"}
    if (not isinstance(state, dict)
            or set(state) not in (base_fields, base_fields | {"operation_id"})
            or ("operation_id" in state and
                (not isinstance(state["operation_id"], str)
                 or len(state["operation_id"]) != 32
                 or any(c not in "0123456789abcdef" for c in state["operation_id"])))
            or not isinstance(state["hold_id"], str)
            or not state["hold_id"].startswith("switch-")
            or len(state["hold_id"]) != 39
            or any(c not in "0123456789abcdef" for c in state["hold_id"][7:])
            or not isinstance(state["old"], str) or not MODEL_NAME.fullmatch(state["old"])
            or not isinstance(state["target"], str) or not MODEL_NAME.fullmatch(state["target"])
            or state["stage"] not in {"pausing", "draining", "switching", "verifying"}
            or type(state["mutation_started"]) is not bool):
        raise SwitchRecoveryRequired("Invalid switch journal")
    return state


class StudioSwitchBarrier:
    """Read a root-controlled journal while holding the shared GPU lock."""

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _check_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o027):
            raise SwitchRecoveryRequired("Untrusted switch journal permissions")

    def read(self):
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise SwitchRecoveryRequired("Invalid switch journal path")
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            FileLock._check_trusted_directory(os.fstat(directory))
            for part in self.path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
                os.close(directory)
                directory = child
                FileLock._check_trusted_directory(os.fstat(directory))
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=directory)
            try:
                self._check_file(os.fstat(fd))
                return decode_switch_journal(os.read(fd, 8193))
            finally:
                os.close(fd)
        finally:
            os.close(directory)
