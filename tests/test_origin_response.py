import pytest

from backend.app.model_control.origin_response import verify_dynamic_response


def headers(content_type):
    return [('Content-Type', content_type), ('X-Content-Type-Options', 'nosniff'),
            ('Content-Security-Policy', "default-src 'none'; sandbox"),
            ('Content-Length', '123'), ('Cache-Control', 'no-store'),
            ('Date', 'Sun, 06 Nov 1994 08:49:37 GMT')]


@pytest.mark.parametrize(('kind', 'content_type', 'extra'), [
    ('json', 'application/json; charset=utf-8', []),
    ('image', 'image/png', []), ('image', 'image/jpeg', []),
    ('download', 'application/octet-stream', [('Content-Disposition', 'attachment')]),
])
def test_supported_wire_responses(kind, content_type, extra):
    verify_dynamic_response(kind, 200, headers(content_type) + extra)


@pytest.mark.parametrize('extra', [
    [('Service-Worker-Allowed', '/')], [('Set-Cookie', 'x=y')],
    [('Clear-Site-Data', '"storage"')], [('Location', '/')],
    [('Content-Encoding', 'gzip')], [('Transfer-Encoding', 'chunked')],
    [('content-type', 'text/javascript')], [('X-Extra', 'a\r\nb')],
])
def test_unreviewed_or_ambiguous_headers_fail(extra):
    with pytest.raises(ValueError):
        verify_dynamic_response('json', 200, headers('application/json') + extra)


@pytest.mark.parametrize(('kind', 'content_type', 'status'), [
    ('image', 'image/svg+xml', 200), ('image', 'text/html', 200),
    ('json', 'text/javascript', 200), ('upload', 'application/json', 200),
    ('json', 'application/json', 302), ('json', 'application/json', True),
])
def test_unsupported_types_and_statuses_fail(kind, content_type, status):
    with pytest.raises(ValueError):
        verify_dynamic_response(kind, status, headers(content_type))


def test_download_requires_disposition_and_policy():
    with pytest.raises(ValueError):
        verify_dynamic_response('download', 200, headers('application/octet-stream'))
    with pytest.raises(ValueError):
        verify_dynamic_response('download', 200, headers('application/octet-stream') + [('Content-Disposition', 'inline')])
    with pytest.raises(ValueError):
        verify_dynamic_response('json', 200, headers('application/json')[:-4])
