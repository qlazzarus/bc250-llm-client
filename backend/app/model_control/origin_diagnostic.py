"""Locked origin diagnostics; not an authorization or enabling preflight.

The lock is advisory: every origin deployer, configuration editor and switch
participant must hold the same lock for the whole operation. Non-participants
can still change the origin during or after inspection. OS runtime provenance,
process lifetime and other preflight requirements remain unverified.
"""
from pathlib import Path

from .origin_policy import inspect_reviewed_static_origin
from .origin_trust_anchor import origin_change_lock, read_origin_manifest_digest

MANIFEST = Path('/etc/bc250-model-control/origin-manifest.json')


def inspect_locked_origin():
    """Inspect a fixed manifest under the cooperative lock; reject unknown routes."""
    with origin_change_lock():
        digest = read_origin_manifest_digest()
        result = inspect_reviewed_static_origin(MANIFEST, expected_manifest_digest=digest)
        if (not isinstance(result, dict) or set(result) != {'static_routes', 'unverified_routes'}
                or not isinstance(result['static_routes'], list)
                or not isinstance(result['unverified_routes'], list)):
            raise RuntimeError('Invalid origin inspection result')
        if result['unverified_routes']:
            raise RuntimeError('Origin has unverified routes')
        if read_origin_manifest_digest() != digest:
            raise RuntimeError('Origin trust anchor changed during inspection')
        return result
