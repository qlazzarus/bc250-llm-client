import pytest

from backend.app.model_control.investigation_evidence import evidence_digest, unassociated_evidence_digest, verify_evidence

OP = 'a' * 32
EVIDENCE = {'journal': b'journal snapshot', 'gate': b'gate snapshot'}


def test_binding_is_order_independent_and_detects_changes():
    digest = evidence_digest(OP, EVIDENCE)
    assert evidence_digest(OP, dict(reversed(list(EVIDENCE.items())))) == digest
    assert verify_evidence(OP, EVIDENCE, digest)
    for op, items in [(OP, {'journal': b'journal snapshot'}),
                      (OP, {**EVIDENCE, 'gate': b'changed'}),
                      (OP, {'journal': EVIDENCE['gate'], 'gate': EVIDENCE['journal']}),
                      ('b' * 32, EVIDENCE)]:
        assert not verify_evidence(op, items, digest)


@pytest.mark.parametrize('op,items', [('', EVIDENCE), (OP, {}), (OP, {'../secret': b'x'}),
                                      (OP, {'gate': 'not bytes'}), (OP, {'Gate': b'x'}),
                                      (OP, {'gate': b'x' * (16 * 1024 * 1024 + 1)}),
                                      (OP, {f'x{i}': b'' for i in range(33)})])
def test_invalid_bundles_rejected(op, items):
    with pytest.raises(ValueError):
        evidence_digest(op, items)


def test_invalid_expected_digest_is_rejected():
    with pytest.raises(ValueError):
        verify_evidence(OP, EVIDENCE, 'invalid')


def test_unassociated_evidence_binds_investigation_and_separates_domains():
    digest = unassociated_evidence_digest(OP, EVIDENCE)
    assert digest != evidence_digest(OP, EVIDENCE)
    assert digest != unassociated_evidence_digest('b' * 32, EVIDENCE)
    assert digest != unassociated_evidence_digest(OP, {'journal': b'changed'})
