#!/usr/bin/env python3
"""Release only startup after read-only checks; never retry an unknown outcome."""
import json
import sys
import urllib.request

from coordination_status import KEYS, get, read_status, valid_gate


def release():
    read_status()
    key = (KEYS / 'gate-admin-key').read_text().rstrip('\r\n')
    url = 'http://127.0.0.1:8081/control'
    before = get(url, key)
    if (not valid_gate(before) or 'startup' not in before['holds']
            or any(hold != 'startup' and hold != 'legacy' and not hold.startswith('admin-')
                   for hold in before['holds'])):
        raise RuntimeError('Gate is not ready for startup release')
    request = urllib.request.Request(url + '/holds/startup', method='DELETE',
                                     headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get('held') is not False:
        raise RuntimeError('Release not confirmed')
    after = get(url, key)
    expected = sorted(hold for hold in before['holds'] if hold != 'startup')
    if (not valid_gate(after) or after['epoch'] != before['epoch']
            or sorted(after['holds']) != expected):
        raise RuntimeError('Gate changed during release')
    read_status()
    return after['holds']


def main():
    try:
        print(json.dumps({'startup_released': True, 'remaining_holds': release()}))
    except Exception:
        print('Release outcome unavailable; inspect state before any retry.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
