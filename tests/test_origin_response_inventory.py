import copy

import pytest

from backend.app.model_control.origin_response_inventory import inspect_response_inventory


def fixture():
    routes = [{'handle': [{'handler': 'subroute', 'routes': [
        {'handle': [{'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:8010'}]}]},
        {'handle': [{'handler': 'file_server'}]},
        {'handle': [{'handler': 'static_response', 'status_code': 404}]},
    ]}]}]
    inventory = {
        (0, 0, 0, 0): {'handler': 'reverse_proxy', 'target': '127.0.0.1:8010',
                       'policy': 'dynamic-wire-v1', 'profiles': ['json', 'image']},
        (0, 0, 1, 0): {'handler': 'file_server', 'policy': 'unverified'},
        (0, 0, 2, 0): {'handler': 'static_response', 'policy': 'unverified'},
    }
    def sample(content_type):
        return (200, [('Content-Type', content_type), ('X-Content-Type-Options', 'nosniff'),
                      ('Content-Security-Policy', "default-src 'none'; sandbox")])
    samples = {((0, 0, 0, 0), 'json'): sample('application/json'),
               ((0, 0, 0, 0), 'image'): sample('image/png')}
    return routes, inventory, samples


def test_exact_inventory_and_samples_are_only_diagnostic():
    routes, inventory, samples = fixture()
    report = inspect_response_inventory(routes, copy.deepcopy(routes), inventory, samples)
    assert report == {'inventoried': sorted(inventory), 'unverified': sorted(inventory)}


@pytest.mark.parametrize('fault', ['missing', 'extra', 'target', 'profile', 'duplicate', 'sample_missing',
                                  'sample_extra', 'unsafe', 'unsupported', 'reorder', 'boolean_location'])
def test_inventory_rejects_missing_changed_or_unverified_inputs(fault):
    routes, inventory, samples = fixture()
    reviewed = copy.deepcopy(routes)
    proxy = (0, 0, 0, 0)
    if fault == 'missing': del inventory[(0, 0, 1, 0)]
    elif fault == 'extra': inventory[(9, 0)] = {'handler': 'file_server', 'policy': 'unverified'}
    elif fault == 'target': inventory[proxy]['target'] = '127.0.0.1:8020'
    elif fault == 'profile': inventory[proxy]['profiles'] = ['upload']
    elif fault == 'duplicate': inventory[proxy]['profiles'].append('json')
    elif fault == 'sample_missing': del samples[(proxy, 'json')]
    elif fault == 'sample_extra': samples[(proxy, 'download')] = samples[(proxy, 'json')]
    elif fault == 'unsafe': samples[(proxy, 'json')][1].append(('Set-Cookie', 'x=y'))
    elif fault == 'boolean_location':
        inventory[(False, 0, 0, 0)] = inventory.pop(proxy)
    elif fault == 'unsupported':
        routes[0]['handle'][0]['routes'][1]['handle'][0] = {'handler': 'custom'}
        reviewed = copy.deepcopy(routes)
    else: routes[0]['handle'][0]['routes'].reverse()
    with pytest.raises(ValueError):
        inspect_response_inventory(routes, reviewed, inventory, samples)
