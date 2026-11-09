import hashlib
import os
import stat
from types import SimpleNamespace

import pytest

from backend.app.model_control.files import FileLock
from backend.app.model_control.investigation_checkpoint import InvestigationCheckpoint
from backend.app.model_control.investigation_log import _encode
from backend.app.model_control.investigation_review_anchor import read_pinned_review, replay_with_pinned_review
from test_investigation_records import records


@pytest.fixture
def review_file(tmp_path, monkeypatch):
    monkeypatch.setattr(FileLock, '_check_trusted_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_file', staticmethod(lambda _: None))
    path = tmp_path / 'review'
    data = _encode(records()[1]) + b'\n'
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def test_read_exact_canonical_review(review_file):
    path, anchor = review_file
    assert read_pinned_review(path, anchor) == records()[1]


def test_mismatch_and_invalid_anchor_fail_closed(review_file):
    path, anchor = review_file
    path.write_bytes(path.read_bytes().replace(b'"review"', b'"revieW"'))
    with pytest.raises(RuntimeError, match='anchor mismatch'):
        read_pinned_review(path, anchor)
    with pytest.raises(ValueError):
        read_pinned_review(path, '0' * 63)


@pytest.mark.parametrize('data', [b'{}\n', b'{}', b' ' * 4097, b''])
def test_even_pinned_malformed_or_oversize_record_fails(review_file, data):
    path, _ = review_file
    path.write_bytes(data)
    with pytest.raises(RuntimeError):
        read_pinned_review(path, hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize('change', ['bool_version', 'duplicate_key', 'extra_field'])
def test_even_pinned_invalid_review_schema_is_rejected(review_file, change):
    path, _ = review_file
    review = records()[1]
    if change == 'bool_version':
        review['version'] = True
    elif change == 'extra_field':
        review['extra'] = 'value'
    data = _encode(review) + b'\n'
    if change == 'duplicate_key':
        data = data.replace(b'"version":1', b'"version":1,"version":1')
    path.write_bytes(data)
    with pytest.raises(RuntimeError, match='Invalid review record'):
        read_pinned_review(path, hashlib.sha256(data).hexdigest())


def test_symlink_rejected(review_file, tmp_path):
    path, anchor = review_file
    target = tmp_path / 'target'
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(OSError):
        read_pinned_review(path, anchor)


def test_root_file_protection_contract():
    good = {'st_mode': stat.S_IFREG | 0o600, 'st_uid': 0, 'st_nlink': 1}
    InvestigationCheckpoint._check_file(SimpleNamespace(**good))
    for change in ({'st_uid': 1}, {'st_nlink': 2}, {'st_mode': stat.S_IFREG | 0o644}):
        with pytest.raises(RuntimeError):
            InvestigationCheckpoint._check_file(SimpleNamespace(**(good | change)))


def test_replacement_during_read_rejected(review_file, monkeypatch, tmp_path):
    path, anchor = review_file
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(path.read_bytes())
    original_read = os.read
    changed = False
    def replace_after_read(fd, count):
        nonlocal changed
        data = original_read(fd, count)
        if data and not changed:
            changed = True
            os.replace(replacement, path)
        return data
    monkeypatch.setattr(os, 'read', replace_after_read)
    with pytest.raises(RuntimeError, match='changed'):
        read_pinned_review(path, anchor)


@pytest.mark.parametrize(('length', 'status'), [(2, 'no_attempt'), (3, 'outcome_unknown'),
                                               (4, 'reconciliation_required')])
def test_pinned_review_only_reports_unresolved_progress(review_file, length, status):
    path, anchor = review_file
    assert replay_with_pinned_review(records()[:length], path, anchor).status == status


def test_missing_or_mismatched_review_is_unverified(review_file, tmp_path):
    path, anchor = review_file
    assert replay_with_pinned_review(records()[:1], path, anchor).status == 'unverified'
    assert replay_with_pinned_review(records(), tmp_path / 'missing', anchor).status == 'unverified'
    assert replay_with_pinned_review(records(), path, '0' * 64).status == 'unverified'
    changed = dict(records()[1], review_digest='f' * 64)
    data = _encode(changed) + b'\n'
    path.write_bytes(data)
    assert replay_with_pinned_review(records(), path, hashlib.sha256(data).hexdigest()).status == 'unverified'


def test_pinned_review_does_not_rescue_invalid_investigation(review_file):
    path, anchor = review_file
    bad = records()
    bad[3]['result'] = 'invalid'
    assert replay_with_pinned_review(bad, path, anchor).status == 'unverified'


def test_review_read_error_stays_unverified(review_file, monkeypatch):
    path, anchor = review_file
    def fail(*args, **kwargs):
        raise OSError('read failed')
    monkeypatch.setattr(os, 'read', fail)
    assert replay_with_pinned_review(records(), path, anchor).status == 'unverified'
