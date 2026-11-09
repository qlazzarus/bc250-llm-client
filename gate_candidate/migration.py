"""Offline conversion of a drained legacy gate snapshot to protocol 3 state.

The caller must stop the legacy gate and exclude other controllers after taking
an authenticated snapshot. This module cannot prove those operational facts.
"""
from .gate import provision


def provision_from_legacy_snapshot(path, snapshot):
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {'paused', 'active_requests', 'waiting_requests'}
            or type(snapshot['paused']) is not bool
            or type(snapshot['active_requests']) is not int
            or type(snapshot['waiting_requests']) is not int
            or snapshot['active_requests'] != 0 or snapshot['waiting_requests'] != 0):
        raise ValueError('A drained legacy gate snapshot is required')
    provision(path, legacy_paused=snapshot['paused'])
