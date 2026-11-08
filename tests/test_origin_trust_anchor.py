import os
from pathlib import Path

import pytest

from backend.app.model_control import origin_trust_anchor as trust


@pytest.fixture
def controls(tmp_path, monkeypatch):
    # Portable local stand-in for root-owned trusted directories and files.
    monkeypatch.setattr(trust, '_check_entry', lambda fd, *, directory: os.fstat(fd))
    monkeypatch.setattr(trust, 'ANCHOR', tmp_path / 'anchor')
    monkeypatch.setattr(trust, 'LOCK', tmp_path / 'lock')
    trust.ANCHOR.write_text('a' * 64 + '\n')
    trust.LOCK.touch()
    return tmp_path


def test_anchor_only_reads_fixed_validated_content(controls):
    assert trust.read_origin_manifest_digest() == 'a' * 64
    trust.ANCHOR.write_text('a' * 64 + '\nextra')
    with pytest.raises(RuntimeError, match='Invalid'):
        trust.read_origin_manifest_digest()
    trust.ANCHOR.unlink()
    trust.ANCHOR.symlink_to(controls / 'lock')
    with pytest.raises(OSError):
        trust.read_origin_manifest_digest()


def test_lock_is_exclusive_and_rejects_replacement(controls):
    with trust.origin_change_lock():
        with pytest.raises(RuntimeError, match='busy'):
            with trust.origin_change_lock():
                pass
    with pytest.raises(RuntimeError, match='changed'):
        with trust.origin_change_lock():
            trust.LOCK.unlink()
            trust.LOCK.touch()


def test_lock_must_already_exist(controls):
    trust.LOCK.unlink()
    with pytest.raises(FileNotFoundError):
        with trust.origin_change_lock():
            pass


def test_entry_rejects_untrusted_owner_mode_and_acl(tmp_path, monkeypatch):
    path = tmp_path / 'file'; path.write_text('a'); path.chmod(0o600)
    fd = os.open(path, os.O_RDONLY)
    try:
        actual = os.fstat(fd)
        from types import SimpleNamespace
        monkeypatch.setattr(trust.os, 'fstat', lambda _fd: SimpleNamespace(st_uid=0, st_mode=actual.st_mode, st_nlink=1))
        monkeypatch.setattr(trust.os, 'listxattr', lambda _fd: ['system.posix_acl_access'], raising=False)
        with pytest.raises(RuntimeError, match='ACL'):
            trust._check_entry(fd, directory=False)
        monkeypatch.setattr(trust.os, 'fstat', lambda _fd: SimpleNamespace(st_uid=1, st_mode=actual.st_mode, st_nlink=1))
        with pytest.raises(RuntimeError, match='Untrusted'):
            trust._check_entry(fd, directory=False)
    finally:
        os.close(fd)
