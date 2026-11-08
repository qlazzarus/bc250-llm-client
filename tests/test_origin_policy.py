import copy

import pytest

from backend.app.model_control.origin_policy import match_reviewed_config, verify_untrusted_download_response
from backend.app.model_control.origin_transport import config_digest


@pytest.fixture
def reviewed_config():
    return {'admin': {'listen': 'unix//run/caddy-admin/control.sock'}, 'apps': {'http': {'servers': {}}}}


def test_reviewed_config_requires_exact_digest_and_unix_admin(reviewed_config):
    digest = config_digest(reviewed_config)
    match_reviewed_config(reviewed_config, expected_digest=digest, admin_socket='/run/caddy-admin/control.sock')
    changed = copy.deepcopy(reviewed_config)
    changed['apps']['http']['servers']['new'] = {'listen': [':80']}
    with pytest.raises(ValueError):
        match_reviewed_config(changed, expected_digest=digest, admin_socket='/run/caddy-admin/control.sock')


@pytest.mark.parametrize('mutation', [
    {'listen': '127.0.0.1:2019'},
    {'listen': 'unix//run/caddy-admin/control.sock', 'remote': {'listen': ':2021'}},
    {'listen': 'unix//run/caddy-admin/control.sock', 'config': {'load': {'module': 'http'}}},
])
def test_reviewed_config_does_not_allow_other_admin_paths(reviewed_config, mutation):
    reviewed_config['admin'] = mutation
    with pytest.raises(ValueError):
        match_reviewed_config(reviewed_config, expected_digest=config_digest(reviewed_config), admin_socket='/run/caddy-admin/control.sock')


HEADERS = [('Content-Type', 'application/octet-stream'), ('Content-Disposition', 'attachment'),
           ('X-Content-Type-Options', 'nosniff'), ('Content-Security-Policy', "default-src 'none'; sandbox")]


def test_narrow_download_policy_accepts_only_explicit_inert_response():
    verify_untrusted_download_response(200, HEADERS)


@pytest.mark.parametrize('headers', [
    HEADERS + [('content-type', 'text/html')],
    HEADERS + [('Service-Worker-Allowed', '/')],
    HEADERS + [('Location', '/llm')],
    HEADERS + [('Set-Cookie', 'session=attacker; Path=/')],
    HEADERS + [('Clear-Site-Data', '"storage"')],
    HEADERS + [('Refresh', '0; url=/llm')],
    [(key, 'inline' if key == 'Content-Disposition' else value) for key, value in HEADERS],
    [(key, 'text/html' if key == 'Content-Type' else value) for key, value in HEADERS],
    [(key, "default-src *" if key == 'Content-Security-Policy' else value) for key, value in HEADERS],
    HEADERS[:-1],
])
def test_download_policy_rejects_unsafe_or_ambiguous_headers(headers):
    with pytest.raises(ValueError): verify_untrusted_download_response(200, headers)


def test_download_policy_rejects_redirect():
    with pytest.raises(ValueError): verify_untrusted_download_response(302, HEADERS)


def test_config_stability_rejects_change_of_process_or_reviewed_digest(reviewed_config):
    from backend.app.model_control.origin_policy import verify_stable_caddy_config
    digest = config_digest(reviewed_config)
    args = {'read_identity': lambda: (42, 100), 'read_config': lambda: reviewed_config,
            'read_reviewed_digest': lambda: digest, 'admin_socket': '/run/caddy-admin/control.sock'}
    verify_stable_caddy_config(**args)
    for key, values in [('read_identity', [(42, 100), (42, 101)]),
                        ('read_reviewed_digest', [digest, 'a' * 64])]:
        attempts = iter(values)
        with pytest.raises(ValueError):
            verify_stable_caddy_config(**{**args, key: lambda: next(attempts)})


def test_config_stability_rejects_effective_route_change(reviewed_config):
    from backend.app.model_control.origin_policy import verify_stable_caddy_config
    changed = copy.deepcopy(reviewed_config)
    changed['apps']['http']['servers']['new'] = {'listen': [':80']}
    attempts = iter([reviewed_config, changed])
    with pytest.raises(ValueError):
        verify_stable_caddy_config(read_identity=lambda: (42, 100), read_config=lambda: next(attempts),
                                   read_reviewed_digest=lambda: config_digest(reviewed_config),
                                   admin_socket='/run/caddy-admin/control.sock')


def sample_routes():
    return {'admin': {'listen': 'unix//run/caddy-admin/control.sock'}, 'apps': {'http': {'servers': {
        'srv': {'listen': [':80'], 'routes': [
            {'@id': 'ui', 'match': [{'path': ['/llm/*']}], 'handle': [{'handler': 'file_server', 'root': '/opt/client/dist'}]},
            {'@id': 'api', 'match': [{'path': ['/llm/api/*']}], 'handle': [{'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:8010'}]}]},
        ]}}}}}


def test_inventory_covers_static_and_dynamic_routes():
    from backend.app.model_control.origin_policy import enumerate_supported_routes
    expected = {'ui': {'kind': 'static', 'target': '/opt/client/dist'}, 'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}}
    assert enumerate_supported_routes(sample_routes(), expected) == expected


@pytest.mark.parametrize('fault', ['extra_route', 'no_id', 'unknown_handler', 'upstream', 'missing_inventory', 'unknown_matcher', 'extra_server_property'])
def test_inventory_rejects_unreviewed_or_unknown_routes(fault):
    from backend.app.model_control.origin_policy import enumerate_supported_routes
    config = sample_routes(); server = config['apps']['http']['servers']['srv']; routes = server['routes']
    expected = {'ui': {'kind': 'static', 'target': '/opt/client/dist'}, 'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}}
    if fault == 'extra_route': routes.append({'@id': 'other', 'handle': [{'handler': 'file_server', 'root': '/tmp'}]})
    elif fault == 'no_id': del routes[0]['@id']
    elif fault == 'unknown_handler': routes[0]['handle'][0]['handler'] = 'templates'
    elif fault == 'upstream': routes[1]['handle'][0]['upstreams'][0]['dial'] = 'example.com:80'
    elif fault == 'missing_inventory': expected['absent'] = {'kind': 'static', 'target': '/opt/missing'}
    elif fault == 'unknown_matcher': routes[0]['match'] = [{'not': [{'path': ['/llm']}]}]
    else: server['automatic_https'] = {'disable': True}
    with pytest.raises(ValueError): enumerate_supported_routes(config, expected)


def test_reviewed_manifest_requires_independent_digest_and_rejects_tampering(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from backend.app.model_control import origin_policy as policy
    manifest = {'version': 1, 'config_digest': 'a' * 64, 'admin_socket': '/run/caddy-admin/control.sock',
                'routes': {'ui': {'kind': 'static', 'target': '/opt/client/dist'}}}
    path = tmp_path / 'manifest.json'; raw = json.dumps(manifest).encode(); path.write_bytes(raw)
    monkeypatch.setattr(policy, 'trusted_path', lambda *_args, **_kwargs: None)
    real_fstat = policy.os.fstat
    def fstat(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=0, st_size=info.st_size,
                               st_dev=info.st_dev, st_ino=info.st_ino, st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)
    monkeypatch.setattr(policy.os, 'fstat', fstat)
    digest = hashlib.sha256(raw).hexdigest()
    assert policy.load_reviewed_manifest(path, expected_manifest_digest=digest) == manifest
    with pytest.raises(ValueError): policy.load_reviewed_manifest(path, expected_manifest_digest='a' * 64)
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError): policy.load_reviewed_manifest(path, expected_manifest_digest=hashlib.sha256(path.read_bytes()).hexdigest())


def test_v3_manifest_loader_checks_digest_and_required_fields(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from backend.app.model_control import origin_policy as policy
    manifest = {'version': 3, 'config_digest': 'a' * 64, 'admin_socket': '/run/caddy-admin/control.sock',
                'routes': {'ui': {'kind': 'static', 'target': '/opt/client/dist'},
                           'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}},
                'caddy': {'unit': 'caddy.service', 'account': 'caddy', 'group': 'caddy',
                          'executable': '/usr/bin/caddy', 'argv': ['/usr/bin/caddy'],
                          'binary_digest': 'b' * 64, 'admin_group': 'caddy-admin'},
                'static_assets': {'ui': {'index.html': 'c' * 64}},
                'backends': {'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010', 'unit': 'client.service',
                                     'account': 'client', 'group': 'client', 'executable': '/usr/bin/python3',
                                     'argv': ['/usr/bin/python3'], 'binary_digest': 'd' * 64,
                                     'tree': '/opt/client', 'files': {'main.py': 'e' * 64}}}}
    path = tmp_path / 'manifest.json'
    monkeypatch.setattr(policy, 'trusted_path', lambda *_args, **_kwargs: None)
    real_fstat = policy.os.fstat
    def fstat(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=0, st_size=info.st_size,
                               st_dev=info.st_dev, st_ino=info.st_ino, st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)
    monkeypatch.setattr(policy.os, 'fstat', fstat)
    raw = json.dumps(manifest).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    assert policy.load_reviewed_manifest(path, expected_manifest_digest=digest) == manifest
    path.write_bytes(raw + b' ')
    with pytest.raises(ValueError, match='digest'):
        policy.load_reviewed_manifest(path, expected_manifest_digest=digest)
    del manifest['backends']
    raw = json.dumps(manifest).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='Invalid reviewed manifest'):
        policy.load_reviewed_manifest(path, expected_manifest_digest=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize('root', ['/srv/{http.request.host}', '/srv/%2e%2e/secret', '/srv/\\evil', '/srv/../secret'])
def test_static_root_rejects_placeholders_escapes_and_traversal(root):
    from backend.app.model_control.origin_policy import enumerate_supported_routes
    config = sample_routes(); config['apps']['http']['servers']['srv']['routes'][0]['handle'][0]['root'] = root
    routes = {'ui': {'kind': 'static', 'target': root}, 'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}}
    with pytest.raises(ValueError): enumerate_supported_routes(config, routes)


@pytest.mark.parametrize('field,value', [('listen', ['{env.LISTEN}']), ('listen', ['unix//tmp/caddy']),
                                        ('path', ['/llm/%2e%2e/*']), ('path', ['/llm/{host}']),
                                        ('host', ['{http.request.host}']), ('method', ['{method}'])])
def test_listener_and_matcher_reject_ambiguous_expressions(field, value):
    from backend.app.model_control.origin_policy import enumerate_supported_routes
    config = sample_routes(); server = config['apps']['http']['servers']['srv']
    if field == 'listen': server['listen'] = value
    else: server['routes'][0]['match'] = [{field: value}]
    expected = {'ui': {'kind': 'static', 'target': '/opt/client/dist'}, 'api': {'kind': 'dynamic', 'target': '127.0.0.1:8010'}}
    with pytest.raises(ValueError): enumerate_supported_routes(config, expected)
