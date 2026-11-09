"""Bind a reviewed response inventory to a supported Caddy route tree.

Diagnostic only: tree comparison does not establish Caddy matching semantics,
policy enforcement, complete response coverage, or the provenance of the
reviewed inputs and samples. No successful result enables model switching.
"""
from .origin_response import verify_dynamic_response
from .origin_route_tree import verify_reviewed_route_tree


def inspect_response_inventory(routes, reviewed_routes, reviewed_responses, samples):
    """Require an exact policy for every response-producing handler location.

    Locations are route/handler index tuples as in verify_reviewed_route_tree.
    Every proxy declares its exact upstream and one or more distinct supported
    wire profiles; each profile needs a sample checked by the v1 response policy.
    Samples are (status, complete uncombined headers), keyed by (location,
    profile). They do not prove every response complies. Static and generated
    responses are inventoried but their content and safety remain unverified.
    Callers must independently authenticate the inputs and prevent changes.
    """
    proxies = dict(verify_reviewed_route_tree(routes, reviewed_routes))
    if not isinstance(reviewed_responses, dict) or not isinstance(samples, dict):
        raise ValueError('Response inventory and samples are required')
    producers = {}

    def walk(items, location):
        for i, route in enumerate(items):
            for j, handler in enumerate(route['handle']):
                pos = location + (i, j)
                kind = handler['handler']
                if kind == 'subroute':
                    walk(handler['routes'], pos)
                elif kind in ('reverse_proxy', 'file_server', 'static_response'):
                    producers[pos] = kind

    walk(routes, ())
    def valid_location(value):
        return (type(value) is tuple and bool(value) and len(value) % 2 == 0
                and all(type(part) is int and part >= 0 for part in value))

    if (any(not valid_location(pos) for pos in reviewed_responses)
            or any(type(key) is not tuple or len(key) != 2
                   or not valid_location(key[0]) or type(key[1]) is not str for key in samples)):
        raise ValueError('Invalid response inventory location')
    if set(reviewed_responses) != set(producers):
        raise ValueError('Response inventory differs from route tree')
    required_samples = set()
    for pos, handler in producers.items():
        policy = reviewed_responses[pos]
        if not isinstance(policy, dict):
            raise ValueError('Invalid response policy')
        if handler == 'reverse_proxy':
            if (set(policy) != {'handler', 'target', 'policy', 'profiles'}
                    or policy['handler'] != handler or policy['target'] != proxies[pos]
                    or policy['policy'] != 'dynamic-wire-v1'
                    or not isinstance(policy['profiles'], list) or not policy['profiles']
                    or any(type(profile) is not str or profile not in ('json', 'image', 'download')
                           for profile in policy['profiles'])
                    or len(set(policy['profiles'])) != len(policy['profiles'])):
                raise ValueError('Invalid dynamic response policy')
            required_samples.update((pos, profile) for profile in policy['profiles'])
        elif policy != {'handler': handler, 'policy': 'unverified'}:
            raise ValueError('Invalid static response policy')
    if set(samples) != required_samples:
        raise ValueError('Dynamic response samples differ from reviewed profiles')
    for key in required_samples:
        sample = samples[key]
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            raise ValueError('Invalid dynamic response sample')
        verify_dynamic_response(key[1], sample[0], sample[1])
    return {'inventoried': sorted(producers), 'unverified': sorted(producers)}
