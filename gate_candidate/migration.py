"""Offline conversion of a drained legacy gate snapshot to protocol 3 state.

The caller must stop the legacy gate and exclude other controllers after taking
an authenticated snapshot. This module cannot prove those operational facts.
"""
from .gate import HEX, provision


def provision_from_legacy_snapshot(path, snapshot):
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {'paused', 'active_requests', 'waiting_requests'}
            or type(snapshot['paused']) is not bool
            or type(snapshot['active_requests']) is not int
            or type(snapshot['waiting_requests']) is not int
            or snapshot['active_requests'] != 0 or snapshot['waiting_requests'] != 0):
        raise ValueError('A drained legacy gate snapshot is required')
    provision(path, legacy_paused=snapshot['paused'])


def legacy_pause_for_rollback(state):
    """Determine legacy pause only when protocol 3 state can be represented.

    The caller must separately verify worker records and stop all controllers.
    This does not change either gate or authorize a rollback.
    """
    if (not isinstance(state, dict) or type(state.get('protocol')) is not int
            or state['protocol'] != 3 or state.get('durable') is not True
            or not isinstance(state.get('epoch'), str) or not HEX.fullmatch(state['epoch'])
            or 'recovery_exclusive_hold' not in state or state['recovery_exclusive_hold'] is not None
            or 'deployment_generation' not in state or state['deployment_generation'] is not None
            or type(state.get('paused')) is not bool
            or type(state.get('active_requests')) is not int or state['active_requests'] != 0
            or type(state.get('waiting_requests')) is not int or state['waiting_requests'] != 0
            or not isinstance(state.get('holds'), list)
            or any(not isinstance(hold, str) or hold not in {'startup', 'legacy'} for hold in state['holds'])
            or len(set(state['holds'])) != len(state['holds'])
            or state['paused'] != bool(state['holds'])):
        raise ValueError('Gate state cannot be safely represented by the legacy gate')
    return 'legacy' in state['holds']
