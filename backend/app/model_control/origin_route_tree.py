"""Narrow Caddy route-tree comparison, not an origin security verdict.

The caller must authenticate both the effective configuration and the reviewed
policy. This compares the entire supported tree, including order, matcher OR
sets, route groups and terminal flags; it does not simulate Caddy matching or
prove that handlers, responses, files, or runtime are trustworthy.
"""
import copy
import re

_PATH = re.compile(r'/[A-Za-z0-9_./*~-]*\Z')
_TARGET = re.compile(r'127\.0\.0\.1:([1-9][0-9]{0,4})\Z')


def verify_reviewed_route_tree(routes, reviewed_routes):
    """Validate and compare a restricted route tree, returning proxy locations.

    Unknown fields and handlers fail closed. Locations are index tuples into
    route/handler lists, not route authorization decisions. All proxy responses
    remain unverified. This is not connected to the enabling preflight.
    """
    proxies = []
    count = 0

    def path(value, *, wildcard=True):
        if (not isinstance(value, str) or not _PATH.fullmatch(value)
                or any(p in ('.', '..') for p in value.split('/'))
                or (not wildcard and '*' in value)):
            raise ValueError('Unsupported route path')

    def walk(items, location, depth):
        nonlocal count
        if depth > 16 or not isinstance(items, list) or not items:
            raise ValueError('Unsupported route tree')
        for index, route in enumerate(items):
            count += 1
            if count > 1024 or not isinstance(route, dict) or set(route) - {'@id', 'match', 'handle', 'terminal', 'group'}:
                raise ValueError('Unsupported route')
            for name in ('@id', 'group'):
                if name in route and (not isinstance(route[name], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', route[name])):
                    raise ValueError('Unsupported route identity')
            if 'terminal' in route and type(route['terminal']) is not bool:
                raise ValueError('Unsupported terminal flag')
            matches = route.get('match', [])
            if not isinstance(matches, list):
                raise ValueError('Unsupported matcher')
            for matcher in matches:
                if not isinstance(matcher, dict) or not matcher or set(matcher) - {'path', 'host', 'method', 'file'}:
                    raise ValueError('Unsupported matcher')
                for name, values in matcher.items():
                    if name == 'file':
                        if values != {'try_files': ['{http.request.uri.path}', '/index.html']}:
                            raise ValueError('Unsupported file matcher')
                        continue
                    if not isinstance(values, list) or not values:
                        raise ValueError('Unsupported matcher values')
                    for value in values:
                        if name == 'path':
                            path(value)
                        elif (not isinstance(value, str) or not re.fullmatch(
                                r'[A-Za-z0-9.-]+' if name == 'host' else r'[A-Z]+', value)):
                            raise ValueError('Unsupported matcher value')
            handlers = route.get('handle')
            if not isinstance(handlers, list) or not handlers:
                raise ValueError('Unsupported handlers')
            for hindex, handler in enumerate(handlers):
                if not isinstance(handler, dict):
                    raise ValueError('Unsupported handler')
                kind = handler.get('handler')
                pos = location + (index, hindex)
                if kind == 'subroute' and set(handler) == {'handler', 'routes'}:
                    walk(handler['routes'], pos, depth + 1)
                elif kind == 'rewrite' and set(handler) in ({'handler', 'strip_path_prefix'}, {'handler', 'uri'}):
                    value = handler.get('strip_path_prefix', handler.get('uri'))
                    if 'uri' in handler and value == '{http.matchers.file.relative}':
                        if not matches or any('file' not in matcher for matcher in matches):
                            raise ValueError('File rewrite without file matcher')
                    else:
                        path(value, wildcard=False)
                elif kind == 'vars' and set(handler) == {'handler', 'root'}:
                    path(handler['root'], wildcard=False)
                elif kind == 'file_server' and set(handler) == {'handler'}:
                    pass
                elif (kind == 'encode' and set(handler) == {'handler', 'encodings', 'prefer'}
                      and handler['encodings'] == {'gzip': {}, 'zstd': {}}
                      and handler['prefer'] == ['zstd', 'gzip']):
                    pass
                elif kind == 'reverse_proxy' and set(handler) == {'handler', 'upstreams'}:
                    upstreams = handler['upstreams']
                    if (not isinstance(upstreams, list) or len(upstreams) != 1
                            or not isinstance(upstreams[0], dict) or set(upstreams[0]) != {'dial'}
                            or not isinstance(upstreams[0]['dial'], str)):
                        raise ValueError('Unsupported proxy')
                    match = _TARGET.fullmatch(upstreams[0]['dial'])
                    if not match or int(match.group(1)) > 65535:
                        raise ValueError('Unsupported proxy')
                    proxies.append((pos, upstreams[0]['dial']))
                elif (kind == 'static_response' and set(handler) == {'handler', 'status_code'}
                      and type(handler['status_code']) is int and 100 <= handler['status_code'] <= 599):
                    pass
                elif (kind == 'static_response' and set(handler) == {'handler', 'status_code', 'headers'}
                      and type(handler['status_code']) is int and handler['status_code'] == 302
                      and handler['headers'] == {'Location': ['308']}):
                    pass
                else:
                    raise ValueError('Unsupported handler')

    walk(routes, (), 0)
    # Compare objects as well as their types: Python considers True equal to 1.
    def same(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b
    if not same(routes, reviewed_routes):
        raise ValueError('Route tree differs from review')
    return copy.deepcopy(proxies)
