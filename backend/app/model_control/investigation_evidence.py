"""Deterministic, read-only evidence binding; never authorizes recovery.

The caller supplies independently acquired bytes and retains them in protected
storage. Only the manifest digest belongs in the investigation log. A digest
is not proof of source, ownership, completeness, freshness, or authorization;
low-entropy secrets must not be used as evidence because hashes can disclose
information by guessing. This module provides no backup or secure collection.
"""
import hashlib
import hmac
import re

from .investigation_log import _encode

_NAME = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_MAX_ITEMS = 32
_MAX_ITEM_BYTES = 16 * 1024 * 1024


def _bundle_digest(identifier, association, evidence):
    """Bind exact named byte snapshots to an operation, without storing them.

    Names are fixed identifiers selected by the caller, not file paths or raw
    metadata. The caller must enforce its own complete required set and collect
    and protect snapshots consistently. Empty evidence sets are rejected.
    """
    if (not isinstance(identifier, str) or not _HEX32.fullmatch(identifier)
            or not isinstance(evidence, dict) or not 1 <= len(evidence) <= _MAX_ITEMS):
        raise ValueError('Invalid evidence bundle')
    entries = []
    for name, content in evidence.items():
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(content, bytes) or len(content) > _MAX_ITEM_BYTES):
            raise ValueError('Invalid evidence item')
        entries.append({'name': name, 'size': len(content),
                        'sha256': hashlib.sha256(content).hexdigest()})
    entries.sort(key=lambda entry: entry['name'])
    manifest = {'domain': 'model-control-investigation-evidence-v1',
                'association': association, 'identifier': identifier, 'items': entries}
    return hashlib.sha256(_encode(manifest)).hexdigest()


def evidence_digest(operation_id, evidence):
    return _bundle_digest(operation_id, 'operation', evidence)


def unassociated_evidence_digest(investigation_id, evidence):
    """Bind orphan evidence to its investigation without inventing an operation."""
    return _bundle_digest(investigation_id, 'unassociated', evidence)


def verify_evidence(operation_id, evidence, expected_digest):
    """Check supplied snapshots against a previously trusted bundle digest."""
    if not isinstance(expected_digest, str) or not _HEX64.fullmatch(expected_digest):
        raise ValueError('Invalid evidence digest')
    return hmac.compare_digest(evidence_digest(operation_id, evidence), expected_digest)
