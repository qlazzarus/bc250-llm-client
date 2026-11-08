from contextlib import contextmanager

import pytest

from backend.app.model_control import origin_diagnostic as diagnostic


def test_inspection_and_both_anchor_reads_run_under_lock(monkeypatch):
    events = []
    @contextmanager
    def lock():
        events.append('lock')
        try:
            yield
        finally:
            events.append('unlock')
    def anchor():
        assert events[-1] in ('lock', 'inspect')
        events.append('anchor')
        return 'a' * 64
    def inspect(path, *, expected_manifest_digest):
        assert events[-1] == 'anchor'
        assert path == diagnostic.MANIFEST and expected_manifest_digest == 'a' * 64
        events.append('inspect')
        return {'static_routes': ['ui'], 'unverified_routes': []}
    monkeypatch.setattr(diagnostic, 'origin_change_lock', lock)
    monkeypatch.setattr(diagnostic, 'read_origin_manifest_digest', anchor)
    monkeypatch.setattr(diagnostic, 'inspect_reviewed_static_origin', inspect)
    assert diagnostic.inspect_locked_origin()['static_routes'] == ['ui']
    assert events == ['lock', 'anchor', 'inspect', 'anchor', 'unlock']


@pytest.mark.parametrize('result', [
    {'static_routes': ['ui'], 'unverified_routes': ['api']},
    {'static_routes': ['ui']},
    {'static_routes': ['ui'], 'unverified_routes': None},
    {'static_routes': ['ui'], 'unverified_routes': [], 'unexpected': True},
])
def test_inspection_rejects_unverified_or_malformed_results(monkeypatch, result):
    @contextmanager
    def lock():
        yield
    monkeypatch.setattr(diagnostic, 'origin_change_lock', lock)
    monkeypatch.setattr(diagnostic, 'read_origin_manifest_digest', lambda: 'a' * 64)
    monkeypatch.setattr(diagnostic, 'inspect_reviewed_static_origin', lambda *_a, **_k: result)
    with pytest.raises(RuntimeError):
        diagnostic.inspect_locked_origin()


def test_inspection_rejects_changed_anchor_and_lock_failure(monkeypatch):
    @contextmanager
    def lock():
        yield
    monkeypatch.setattr(diagnostic, 'origin_change_lock', lock)
    anchors = iter(['a' * 64, 'b' * 64])
    monkeypatch.setattr(diagnostic, 'read_origin_manifest_digest', lambda: next(anchors))
    monkeypatch.setattr(diagnostic, 'inspect_reviewed_static_origin', lambda *_a, **_k: {'static_routes': ['ui'], 'unverified_routes': []})
    with pytest.raises(RuntimeError, match='anchor changed'):
        diagnostic.inspect_locked_origin()
    @contextmanager
    def busy():
        raise RuntimeError('lock busy')
        yield
    monkeypatch.setattr(diagnostic, 'origin_change_lock', busy)
    with pytest.raises(RuntimeError, match='lock busy'):
        diagnostic.inspect_locked_origin()
