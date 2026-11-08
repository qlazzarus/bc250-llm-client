"""Narrow policy primitives for origin verification; not an activation verdict.

These checks require independently trusted inputs. They do not enumerate all
routes, establish response provenance, or replace verify_operator_frontend().
"""
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path

from .origin_transport import config_digest, _unique_object, _invalid_constant
from .client_trust import trusted_path


def match_reviewed_config(config, *, expected_digest, admin_socket):
    """Match a reviewed complete effective config and a Unix-only admin endpoint.

    The caller must establish the digest's trusted provenance and bind the
    snapshot to a stable authenticated Caddy process. A match alone is not a
    security verdict on the origin or its backend responses.
    """
    if not isinstance(expected_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
        raise ValueError('Invalid reviewed Caddy digest')
    if (not isinstance(admin_socket, str) or not admin_socket.startswith('/')
            or any(part in ('', '.', '..') for part in admin_socket[1:].split('/'))):
        raise ValueError('Invalid administrative socket path')
    if not isinstance(config, dict) or set(config) - {'admin', 'apps', 'logging', 'storage'}:
        raise ValueError('Unrecognized Caddy configuration')
    admin = config.get('admin')
    if not isinstance(admin, dict) or set(admin) - {'listen', 'enforce_origin', 'origins'}:
        raise ValueError('Unrecognized Caddy admin policy')
    if admin.get('listen') != 'unix/' + admin_socket:
        raise ValueError('Caddy admin must use the reviewed Unix socket')
    if not isinstance(config.get('apps'), dict):
        raise ValueError('Missing Caddy application configuration')
    if not hmac.compare_digest(config_digest(config), expected_digest):
        raise ValueError('Caddy configuration differs from reviewed digest')


def verify_untrusted_download_response(status, headers):
    """Check a narrow, inert download response policy, including duplicates.

    `headers` must contain every wire header as (name, value) pairs. This is not
    sufficient for HTML/API responses, redirects, or service-worker provenance.
    """
    if type(status) is not int or status != 200 or not isinstance(headers, (list, tuple)):
        raise ValueError('Unsupported download response')
    fields = {}
    for item in headers:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError('Invalid header')
        name, value = item
        if not isinstance(name, str) or not isinstance(value, str) or not re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+', name):
            raise ValueError('Invalid header')
        if '\r' in value or '\n' in value:
            raise ValueError('Invalid header')
        key = name.lower()
        if key in fields:
            raise ValueError('Duplicate header')
        fields[key] = value.strip()
    if (fields.get('content-type', '').lower() != 'application/octet-stream'
            or fields.get('content-disposition', '').lower() != 'attachment'
            or fields.get('x-content-type-options', '').lower() != 'nosniff'
            or fields.get('content-security-policy') != "default-src 'none'; sandbox"
            or 'service-worker-allowed' in fields or 'location' in fields
            or 'content-encoding' in fields):
        raise ValueError('Untrusted response is not an inert download')


def verify_stable_caddy_config(*, read_identity, read_config, read_reviewed_digest, admin_socket):
    """Compare authenticated effective snapshots and their independently reviewed digest.

    Readers must themselves authenticate the Caddy process/socket and the source
    of the reviewed digest. This does not prevent a later change, and does not
    certify the policy's dynamic routes or assets.
    """
    identity_before = read_identity()
    digest_before = read_reviewed_digest()
    first = read_config()
    match_reviewed_config(first, expected_digest=digest_before, admin_socket=admin_socket)
    second = read_config()
    match_reviewed_config(second, expected_digest=digest_before, admin_socket=admin_socket)
    if (identity_before != read_identity() or digest_before != read_reviewed_digest()
            or config_digest(first) != config_digest(second)):
        raise ValueError('Caddy identity, reviewed digest, or configuration changed')


def enumerate_supported_routes(config, reviewed_routes):
    """Match a deliberately small route grammar against a reviewed route inventory.

    This rejects unknown server, matcher and handler forms. A matching dynamic
    upstream is merely inventoried: its executable and responses remain
    unverified. The caller must not treat this function as an origin verdict.
    """
    if not isinstance(reviewed_routes, dict) or not reviewed_routes:
        raise ValueError('Reviewed routes are required')
    apps = config.get('apps') if isinstance(config, dict) else None
    if not isinstance(apps, dict) or set(apps) != {'http'}:
        raise ValueError('Unsupported Caddy applications')
    http = apps['http']
    if not isinstance(http, dict) or set(http) != {'servers'} or not isinstance(http['servers'], dict) or not http['servers']:
        raise ValueError('Unsupported Caddy HTTP configuration')
    found = {}
    for server in http['servers'].values():
        if (not isinstance(server, dict) or set(server) != {'listen', 'routes'}
                or not isinstance(server['listen'], list) or not server['listen']
                or any(not isinstance(item, str) or not re.fullmatch(r'(?:127\.0\.0\.1)?:[1-9][0-9]{0,4}', item)
                       or int(item.rsplit(':', 1)[1]) > 65535 for item in server['listen'])
                or not isinstance(server['routes'], list) or not server['routes']):
            raise ValueError('Unsupported Caddy server')
        for route in server['routes']:
            if not isinstance(route, dict) or set(route) - {'@id', 'match', 'handle', 'terminal'}:
                raise ValueError('Unsupported route')
            route_id = route.get('@id')
            if not isinstance(route_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', route_id) or route_id in found:
                raise ValueError('Missing or duplicate route ID')
            if 'terminal' in route and type(route['terminal']) is not bool:
                raise ValueError('Invalid route terminal')
            matches = route.get('match', [])
            if not isinstance(matches, list):
                raise ValueError('Invalid route matcher')
            for match in matches:
                if (not isinstance(match, dict) or not match or set(match) - {'host', 'path', 'method'}
                        or any(not isinstance(values, list) or not values
                               or any(not isinstance(value, str) or not value for value in values)
                               for values in match.values())
                        or any(not re.fullmatch(r'[A-Za-z0-9.-]+', value) for value in match.get('host', []))
                        or any(not re.fullmatch(r'/[A-Za-z0-9_./*~-]*', value)
                               or any(part in ('.', '..') for part in value.split('/')) for value in match.get('path', []))
                        or any(not re.fullmatch(r'[A-Z]+', value) for value in match.get('method', []))):
                    raise ValueError('Unsupported route matcher')
            handlers = route.get('handle')
            if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
                raise ValueError('Unsupported route handlers')
            handler = handlers[0]
            if handler.get('handler') == 'file_server' and set(handler) == {'handler', 'root'}:
                root = handler['root']
                if (not isinstance(root, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', root)
                        or any(part in ('', '.', '..') for part in root[1:].split('/'))):
                    raise ValueError('Invalid static root')
                descriptor = {'kind': 'static', 'target': root}
            elif handler.get('handler') == 'reverse_proxy' and set(handler) == {'handler', 'upstreams'}:
                upstreams = handler['upstreams']
                if (not isinstance(upstreams, list) or len(upstreams) != 1
                        or not isinstance(upstreams[0], dict) or set(upstreams[0]) != {'dial'}
                        or not isinstance(upstreams[0]['dial'], str)
                        or not re.fullmatch(r'127\.0\.0\.1:[1-9][0-9]{0,4}', upstreams[0]['dial'])
                        or int(upstreams[0]['dial'].split(':')[1]) > 65535):
                    raise ValueError('Invalid proxy upstream')
                expected = reviewed_routes.get(route_id)
                if not isinstance(expected, dict) or expected.get('kind') not in {'dynamic', 'download'}:
                    raise ValueError('Unclassified dynamic route')
                descriptor = {'kind': expected['kind'], 'target': upstreams[0]['dial']}
            else:
                raise ValueError('Unknown or unsafe handler')
            if reviewed_routes.get(route_id) != descriptor:
                raise ValueError('Route differs from reviewed inventory')
            found[route_id] = descriptor
    if found != reviewed_routes:
        raise ValueError('Missing or unexpected route')
    return found


def load_reviewed_manifest(path, *, expected_manifest_digest):
    """Read a root-controlled manifest pinned by an independent review digest.

    Root ownership alone is not proof of review. The digest must come from an
    independently controlled deployment trust anchor; the caller must also
    recheck it before using the policy. This function does not activate switching.
    """
    if not isinstance(expected_manifest_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_manifest_digest):
        raise ValueError('Independent manifest digest required')
    path = Path(path)
    trusted_path(path, directory=False)
    before = os.lstat(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_uid != 0
                or opened.st_mode & 0o022 or opened.st_size > 262144
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError('Untrusted reviewed manifest')
        with os.fdopen(fd, 'rb') as stream:
            fd = -1
            raw = stream.read(262145)
            after = os.fstat(stream.fileno())
        if (len(raw) > 262144 or len(raw) != opened.st_size
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_manifest_digest)):
            raise ValueError('Reviewed manifest changed or digest mismatched')
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        valid_keys = ({'version', 'config_digest', 'admin_socket', 'routes'} if isinstance(value, dict) and value.get('version') == 1
                      else {'version', 'config_digest', 'admin_socket', 'routes', 'caddy', 'static_assets'})
        if (not isinstance(value, dict) or set(value) != valid_keys
                or type(value['version']) is not int or value['version'] not in (1, 2)
                or not isinstance(value['config_digest'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['config_digest'])
                or not isinstance(value['admin_socket'], str) or not isinstance(value['routes'], dict)
                or not value['routes']):
            raise ValueError('Invalid reviewed manifest')
        trusted_path(path, directory=False)
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('Reviewed manifest was replaced')
        return value
    finally:
        if fd != -1:
            os.close(fd)


def inspect_reviewed_static_origin(path, *, expected_manifest_digest):
    """Bind reviewed policy to authenticated Caddy and static files only.

    This is deliberately not called by the enabling preflight. In particular,
    even a successful return says nothing about dynamic routes, loaded shared
    libraries or the provenance of the independently supplied digest.
    """
    from .origin_process import verify_caddy_identity, verify_static_route_assets
    from .origin_transport import fetch_caddy_config, verify_admin_group

    manifest = load_reviewed_manifest(path, expected_manifest_digest=expected_manifest_digest)
    if manifest['version'] != 2:
        raise ValueError('Static origin manifest version 2 required')
    caddy = manifest['caddy']
    required = {'unit', 'account', 'group', 'executable', 'argv', 'binary_digest', 'admin_group'}
    if not isinstance(caddy, dict) or set(caddy) != required or any(not isinstance(caddy[k], str) or not caddy[k] for k in required - {'argv'}):
        raise ValueError('Invalid reviewed Caddy identity')
    identity_args = {key: caddy[key] for key in required - {'admin_group'}}
    identity = verify_caddy_identity(**identity_args)
    admin_gid, admin_uid, primary_gid = verify_admin_group(caddy['admin_group'], caddy['account'])
    if identity[2:] != (admin_uid, primary_gid):
        raise ValueError('Caddy admin and service identity mismatch')
    def read_config():
        return fetch_caddy_config(manifest['admin_socket'], expected_pid=identity[0], expected_uid=identity[2],
                                  expected_gid=identity[3], admin_gid=admin_gid)
    first = read_config()
    match_reviewed_config(first, expected_digest=manifest['config_digest'], admin_socket=manifest['admin_socket'])
    routes = enumerate_supported_routes(first, manifest['routes'])
    verify_static_route_assets(routes, manifest['static_assets'], caddy_pid=identity[0])
    second = read_config()
    match_reviewed_config(second, expected_digest=manifest['config_digest'], admin_socket=manifest['admin_socket'])
    if (config_digest(first) != config_digest(second)
            or identity != verify_caddy_identity(**identity_args)
            or (admin_gid, admin_uid, primary_gid) != verify_admin_group(caddy['admin_group'], caddy['account'])
            or manifest != load_reviewed_manifest(path, expected_manifest_digest=expected_manifest_digest)):
        raise ValueError('Caddy or reviewed origin policy changed')
    return {'static_routes': sorted(key for key, route in routes.items() if route['kind'] == 'static'),
            'unverified_routes': sorted(key for key, route in routes.items() if route['kind'] != 'static')}
