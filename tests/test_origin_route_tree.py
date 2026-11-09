import copy

import pytest

from backend.app.model_control.origin_route_tree import verify_reviewed_route_tree


def tree():
    return [{'match': [{'path': ['/llm/*']}], 'terminal': True, 'handle': [
        {'handler': 'subroute', 'routes': [
            {'match': [{'path': ['/llm/api/*']}], 'handle': [
                {'handler': 'rewrite', 'strip_path_prefix': '/llm'},
                {'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:8010'}]}]},
            {'handle': [{'handler': 'vars', 'root': '/opt/client/dist'}, {'handler': 'file_server'}]},
        ]}]}]


def test_nested_tree_records_proxy_and_requires_exact_order_and_fallthrough():
    routes = tree()
    assert verify_reviewed_route_tree(routes, copy.deepcopy(routes)) == [((0, 0, 0, 1), '127.0.0.1:8010')]
    for mutation in ('order', 'matcher', 'terminal', 'rewrite', 'handler_order'):
        changed = copy.deepcopy(routes)
        route = changed[0]
        nested = route['handle'][0]['routes']
        if mutation == 'order': nested.reverse()
        elif mutation == 'matcher': nested[0]['match'][0]['path'] = ['/api/*']
        elif mutation == 'terminal': route['terminal'] = False
        elif mutation == 'rewrite': nested[0]['handle'][0]['strip_path_prefix'] = '/api'
        else: nested[0]['handle'].reverse()
        with pytest.raises(ValueError, match='differs'):
            verify_reviewed_route_tree(changed, routes)


def test_reviewed_file_fallback_encode_and_redirect_forms():
    routes = [{'handle': [{'handler': 'encode', 'encodings': {'gzip': {}, 'zstd': {}},
                            'prefer': ['zstd', 'gzip']}]},
              {'match': [{'file': {'try_files': ['{http.request.uri.path}', '/index.html']}}],
               'handle': [{'handler': 'rewrite', 'uri': '{http.matchers.file.relative}'},
                          {'handler': 'file_server'}]},
              {'handle': [{'handler': 'static_response', 'status_code': 302,
                           'headers': {'Location': ['308']}}]}]
    assert verify_reviewed_route_tree(routes, copy.deepcopy(routes)) == []
    for mutation in ('file', 'file_missing', 'encoding', 'location'):
        changed = copy.deepcopy(routes)
        if mutation == 'file': changed[1]['match'][0]['file']['try_files'].append('/other')
        elif mutation == 'file_missing': del changed[1]['match']
        elif mutation == 'encoding': changed[0]['handle'][0]['prefer'].reverse()
        else: changed[2]['handle'][0]['headers']['Location'] = ['https://evil.example/']
        with pytest.raises(ValueError):
            verify_reviewed_route_tree(changed, copy.deepcopy(changed))


@pytest.mark.parametrize('handler', [
    {'handler': 'encode', 'encodings': {'gzip': {}}},
    {'handler': 'rewrite', 'uri': '/{http.request.uri}'},
    {'handler': 'reverse_proxy', 'upstreams': [{'dial': 'localhost:8010'}]},
    {'handler': 'static_response', 'status_code': True},
])
def test_unsupported_forms_fail_even_if_reviewed(handler):
    routes = [{'handle': [handler]}]
    with pytest.raises(ValueError):
        verify_reviewed_route_tree(routes, copy.deepcopy(routes))


def test_authenticated_server_tree_inventory_and_fail_closed_changes():
    from backend.app.model_control.origin_policy import inspect_reviewed_route_trees
    from backend.app.model_control.origin_transport import config_digest

    routes = tree()
    servers = {'srv0': {'listen': [':80'], 'routes': routes}}
    config = {'admin': {'listen': 'unix//run/caddy/admin.sock'},
              'apps': {'http': {'servers': servers}}}
    reviewed = copy.deepcopy(servers)
    digest = config_digest(config)
    def inspect(snapshot=config, policy=reviewed, identity=lambda: (123, 456),
                read_digest=lambda: digest):
        return inspect_reviewed_route_trees(
            read_identity=identity, read_config=lambda: copy.deepcopy(snapshot),
            read_reviewed_digest=read_digest, admin_socket='/run/caddy/admin.sock',
            reviewed_servers=policy)
    assert inspect() == {'srv0': [((0, 0, 0, 1), '127.0.0.1:8010')]}
    for mutation in ('listener', 'route', 'extra_server'):
        changed = copy.deepcopy(reviewed)
        if mutation == 'listener': changed['srv0']['listen'] = [':81']
        elif mutation == 'route': changed['srv0']['routes'][0]['terminal'] = False
        else: changed['other'] = copy.deepcopy(changed['srv0'])
        with pytest.raises(ValueError):
            inspect(policy=changed)
    identities = iter([(123, 456), (124, 456)])
    with pytest.raises(ValueError, match='changed'):
        inspect(identity=lambda: next(identities))
    digests = iter([digest, '0' * 64])
    with pytest.raises(ValueError, match='changed'):
        inspect(read_digest=lambda: next(digests))
