"""Conservative wire-response checks for reviewed dynamic routes.

A caller must supply the complete, uncombined wire headers and a separately
reviewed route classification. Checking samples cannot prove that every
response is constrained, or that the upstream, route, or OS runtime is trusted.
This module is not an origin safety or activation verdict.
"""
import re
from email.utils import parsedate_to_datetime

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_LENGTH = re.compile(r'(?:0|[1-9][0-9]{0,18})\Z')
_TYPES = {
    'json': {'application/json', 'application/json; charset=utf-8'},
    'image': {'image/png', 'image/jpeg', 'image/gif', 'image/webp'},
    'download': {'application/octet-stream'},
}
_REQUIRED = {'content-type', 'x-content-type-options', 'content-security-policy'}
_OPTIONAL = {'content-length', 'date', 'cache-control'}


def verify_dynamic_response(kind, status, headers):
    """Reject unsupported responses, duplicate fields, and origin state headers.

    The narrow profiles accept only 200 responses; redirects, ranges, cookies,
    service-worker scope headers, compression and arbitrary extensions fail.
    Attachments require an exact filename-free disposition. JSON is intended
    only for a trusted backend; MIME and headers cannot establish body safety.
    """
    if not isinstance(kind, str) or kind not in _TYPES or type(status) is not int or status != 200:
        raise ValueError('Unsupported dynamic response')
    if not isinstance(headers, (list, tuple)) or len(headers) > 16:
        raise ValueError('Invalid response headers')
    fields = {}
    for item in headers:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError('Invalid response header')
        name, value = item
        if (not isinstance(name, str) or not _TOKEN.fullmatch(name)
                or not isinstance(value, str) or len(value) > 1024
                or any(ord(c) < 32 or ord(c) >= 127 for c in value)):
            raise ValueError('Invalid response header')
        name = name.lower()
        if name in fields:
            raise ValueError('Duplicate response header')
        fields[name] = value.strip(' ')
    allowed = _REQUIRED | _OPTIONAL | ({'content-disposition'} if kind == 'download' else set())
    if not _REQUIRED <= fields.keys() or set(fields) - allowed:
        raise ValueError('Unsupported response headers')
    if (fields['content-type'].lower() not in _TYPES[kind]
            or fields['x-content-type-options'].lower() != 'nosniff'
            or fields['content-security-policy'] != "default-src 'none'; sandbox"):
        raise ValueError('Unsafe response policy')
    if kind == 'download' and fields.get('content-disposition', '').lower() != 'attachment':
        raise ValueError('Download must be an attachment')
    if 'content-length' in fields and not _LENGTH.fullmatch(fields['content-length']):
        raise ValueError('Invalid content length')
    if 'cache-control' in fields and fields['cache-control'].lower() != 'no-store':
        raise ValueError('Unsupported cache policy')
    if 'date' in fields:
        try:
            date = parsedate_to_datetime(fields['date'])
            if date is None or date.utcoffset() is None or date.utcoffset().total_seconds() != 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Invalid response date') from None
