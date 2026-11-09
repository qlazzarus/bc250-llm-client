import os
import stat
from types import SimpleNamespace

import pytest

from backend.app.model_control.files import FileLock
from backend.app.model_control.investigation_archive import InvestigationArchive
from backend.app.model_control.investigation_checkpoint import InvestigationCheckpoint


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(FileLock, '_check_trusted_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_file', staticmethod(lambda _: None))
    monkeypatch.setattr('backend.app.model_control.investigation_archive.protected_xattrs', lambda _: {})
    return InvestigationArchive(tmp_path / 'evidence')


@pytest.mark.parametrize('association', ['operation', 'unassociated'])
def test_archive_roundtrip_and_external_digest(archive, association):
    evidence = {'journal': b'{"old":"A"}', 'gate': b'\x00\xff'}
    digest = archive.write(association, 'a' * 32, evidence)
    assert archive.read(association, 'a' * 32, digest) == evidence
    with pytest.raises(FileExistsError):
        archive.write(association, 'a' * 32, evidence)
    with pytest.raises(RuntimeError, match='unavailable'):
        archive.read(association, 'a' * 32, digest)


def test_tampered_archive_and_wrong_association_fail_closed(archive):
    digest = archive.write('operation', 'a' * 32, {'journal': b'evidence'})
    data = archive.store.path.read_bytes()
    archive.store.path.write_bytes(data.replace(b'ZXZpZGVuY2U=', b'ZXZpZGVuY2Q='))
    with pytest.raises(RuntimeError, match='verification'):
        archive.read('operation', 'a' * 32, digest)
    fresh = InvestigationArchive(archive.store.path)
    with pytest.raises(RuntimeError, match='verification'):
        fresh.read('unassociated', 'a' * 32, digest)


def test_uncertain_directory_fsync_preserves_original_and_poison(archive, monkeypatch):
    fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory sync failed')
        fsync(fd)
    monkeypatch.setattr(os, 'fsync', fail_directory)
    with pytest.raises(OSError):
        archive.write('operation', 'a' * 32, {'journal': b'evidence'})
    assert archive.store.path.exists()
    with pytest.raises(RuntimeError, match='unavailable'):
        archive.write('operation', 'a' * 32, {'journal': b'evidence'})


def test_symlink_and_acl_fail_closed(archive, tmp_path, monkeypatch):
    target = tmp_path / 'target'
    target.write_bytes(b'do not replace')
    archive.store.path.symlink_to(target)
    with pytest.raises(OSError):
        archive.write('operation', 'a' * 32, {'journal': b'x'})
    assert target.read_bytes() == b'do not replace'
    archive.store.path.unlink()
    fresh = InvestigationArchive(archive.store.path)
    monkeypatch.setattr('backend.app.model_control.investigation_archive.protected_xattrs',
                        lambda _: {'system.posix_acl_access': b'untrusted'})
    with pytest.raises(RuntimeError, match='ACL'):
        fresh.write('operation', 'a' * 32, {'journal': b'x'})


def test_private_regular_root_file_contract():
    good = {'st_mode': stat.S_IFREG | 0o600, 'st_uid': 0, 'st_nlink': 1}
    InvestigationCheckpoint._check_file(SimpleNamespace(**good))
    for change in ({'st_uid': 5}, {'st_nlink': 2}, {'st_mode': stat.S_IFREG | 0o640}):
        with pytest.raises(RuntimeError):
            InvestigationCheckpoint._check_file(SimpleNamespace(**(good | change)))


def test_duplicate_json_and_wrong_digest_are_rejected(archive):
    digest = archive.write('operation', 'a' * 32, {'journal': b'x'})
    raw = archive.store.path.read_bytes()
    archive.store.path.write_bytes(raw.replace(b'"version":1', b'"version":1,"version":1'))
    with pytest.raises(RuntimeError):
        archive.read('operation', 'a' * 32, digest)
    archive.store.path.write_bytes(raw)
    with pytest.raises(RuntimeError):
        InvestigationArchive(archive.store.path).read('operation', 'a' * 32, 'f' * 64)


def test_partial_write_failure_preserves_archive_and_blocks_retry(archive, monkeypatch):
    original_write = os.write
    calls = 0
    def fail_after_prefix(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, data[:5])
        raise OSError('write interrupted')
    monkeypatch.setattr(os, 'write', fail_after_prefix)
    with pytest.raises(OSError):
        archive.write('operation', 'a' * 32, {'journal': b'evidence'})
    assert archive.store.path.read_bytes() and calls == 2
    with pytest.raises(RuntimeError, match='unavailable'):
        archive.read('operation', 'a' * 32, '0' * 64)
    with pytest.raises(RuntimeError):
        InvestigationArchive(archive.store.path).read('operation', 'a' * 32, '0' * 64)


def test_archive_replacement_during_read_is_detected(archive, monkeypatch, tmp_path):
    digest = archive.write('operation', 'a' * 32, {'journal': b'evidence'})
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(archive.store.path.read_bytes())
    original_read = os.read
    switched = False
    def replace_on_read(fd, size):
        nonlocal switched
        data = original_read(fd, size)
        if not switched:
            switched = True
            os.replace(replacement, archive.store.path)
        return data
    monkeypatch.setattr(os, 'read', replace_on_read)
    with pytest.raises(RuntimeError, match='changed during read'):
        archive.read('operation', 'a' * 32, digest)


def test_archive_stores_only_explicit_caller_bytes_and_does_not_sanitize(archive):
    # This is not a secret filter: the caller must not pass sensitive input.
    supplied = {'provided': b'credential-like-value'}
    digest = archive.write('operation', 'a' * 32, supplied)
    assert archive.read('operation', 'a' * 32, digest) == supplied


def test_archive_is_not_connected_to_worker_runtime():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'backend/app/model_control'
    for name in ('runtime.py', 'worker.py', 'bridge.py'):
        tree = ast.parse((root / name).read_text())
        imports = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [item.name for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names]
        assert not any('investigation_archive' in item or 'investigation_diagnosis' in item for item in imports)


@pytest.mark.parametrize('kind', ['archive', 'checkpoint', 'review'])
def test_fifo_read_rejected_without_waiting(archive, monkeypatch, kind):
    from backend.app.model_control.investigation_review_anchor import read_pinned_review
    path = archive.store.path
    os.mkfifo(path)
    def regular_only(info):
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('not regular')
    monkeypatch.setattr(InvestigationCheckpoint, '_check_file', staticmethod(regular_only))
    original_open = os.open
    def require_nonblocking(file, flags, *args, **kwargs):
        if file == path.name and not flags & os.O_NONBLOCK:
            raise AssertionError('FIFO open could block')
        return original_open(file, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', require_nonblocking)
    with pytest.raises(RuntimeError, match='not regular'):
        if kind == 'archive':
            archive.read('operation', 'a' * 32, '0' * 64)
        elif kind == 'checkpoint':
            InvestigationCheckpoint(path).read()
        else:
            read_pinned_review(path, '0' * 64)
