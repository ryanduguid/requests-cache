"""The cache_keys module is mostly covered indirectly via other tests.
This just contains tests for some extra edge cases not covered elsewhere.
"""

from io import BytesIO
import json

import pytest
from requests import Request, Response
from urllib3 import HTTPResponse
from unittest.mock import patch

from requests_cache.cache_keys import (
    MAX_NORM_BODY_SIZE,
    create_key,
    normalize_headers,
    normalize_params,
    normalize_request,
    normalize_url,
    redact_response,
)
from requests_cache.models import CachedRequest, CachedResponse
from requests_cache.serializers import init_serializer

CACHE_KEY = '2d9a656f503c1e39'


@pytest.mark.parametrize('request_type', ['request', 'prepared', 'cached'])
@pytest.mark.parametrize('ignored_header', ['Cookie', 'cOoKiE'])
def test_normalize_request__ignored_cookie_jar(request_type, ignored_header):
    request = Request('GET', 'https://example.com', cookies={'theme': 'light'})
    if request_type != 'request':
        request = request.prepare()
    if request_type == 'cached':
        request = CachedRequest.from_request(request)
    original_jar = request.cookies if request_type == 'request' else request._cookies

    normalised = normalize_request(request, ignored_parameters=[ignored_header])

    assert normalised.headers['Cookie'] == 'REDACTED'
    assert not normalised._cookies
    assert original_jar['theme'] == 'light'
    if request_type != 'request':
        assert request.headers['Cookie'] == 'theme=light'


@pytest.mark.parametrize('has_header', [True, False])
def test_redact_response__ignored_cookie_jar(has_header):
    original_request = Request('GET', 'https://example.com', cookies={'theme': 'light'}).prepare()
    if not has_header:
        del original_request.headers['Cookie']
    response = CachedResponse(
        url=original_request.url, request=CachedRequest.from_request(original_request)
    )

    redact_response(response, ['cOoKiE'])

    assert not response.request.cookies
    assert ('Cookie' in response.request.headers) is has_header
    if has_header:
        assert response.request.headers['Cookie'] == 'REDACTED'
        assert original_request.headers['Cookie'] == 'theme=light'
    assert original_request._cookies['theme'] == 'light'


@pytest.mark.parametrize('location', ['request', 'history', 'next'])
def test_redact_response__detached_request_snapshots(location):
    prepared = Request(
        'GET',
        'https://example.com/page?auth-token=fabricated',
        cookies={'theme': 'light'},
        headers={'X-Private': 'fabricated', 'Content-Type': 'application/x-www-form-urlencoded'},
        data='auth-token=fabricated',
    ).prepare()
    request = CachedRequest.from_request(prepared)
    original = CachedResponse(url=prepared.url, request=request, status_code=200)
    if location == 'history':
        original.status_code = 302
        original.headers['Location'] = 'https://example.com/final'
        original = CachedResponse(
            url='https://example.com/final', history=[original], status_code=200
        )
    elif location == 'next':
        original = CachedResponse(url='https://example.com/start', next=request, status_code=302)

    redacted = CachedResponse.from_response(original)
    redact_response(redacted, ['Cookie', 'auth-token', 'X-Private'])
    if location == 'history':
        stored_request = redacted.history[0].request
        assert 'fabricated' not in redacted.history[0].url
    elif location == 'next':
        stored_request = redacted._next
    else:
        stored_request = redacted.request

    assert stored_request.headers['Cookie'] == 'REDACTED'
    assert stored_request.headers['X-Private'] == 'REDACTED'
    assert not stored_request.cookies
    assert 'fabricated' not in stored_request.url
    assert b'fabricated' not in stored_request.body
    assert request.headers['Cookie'] == 'theme=light'
    assert request.headers['X-Private'] == 'fabricated'
    assert request.cookies['theme'] == 'light'
    assert 'fabricated' in request.url
    assert b'fabricated' in request.body


@pytest.mark.parametrize(
    'value', [bytes([value]) + b'fixture' for value in [28, 29, 30, 31, 133, 160]]
)
@pytest.mark.parametrize('serializer_name', [None, 'json', 'yaml', 'pickle'])
@pytest.mark.parametrize('in_history', [False, True])
def test_redact_response__next_byte_header(value, serializer_name, in_history):
    """Redaction must retain valid byte headers in a prepared next request."""
    request = Request('GET', 'https://example.com/start').prepare()
    following = Request(
        'GET',
        'https://example.com/final',
        headers={'X-Variant': value, 'X-Private': 'fabricated', 'Content-Type': 'text/plain'},
    ).prepare()
    response = CachedResponse(
        url=request.url,
        status_code=302,
        headers={'Location': following.url},
        request=CachedRequest.from_request(request),
        next=CachedRequest.from_request(following),
    )
    if in_history:
        response = CachedResponse(
            url=following.url,
            request=CachedRequest.from_request(following),
            status_code=200,
            history=[response],
        )

    stored = CachedResponse.from_response(response)
    redact_response(stored, ['X-Private'])
    if serializer_name:
        if serializer_name == 'yaml':
            pytest.importorskip('yaml')
        serializer = init_serializer(serializer_name, decode_content=False)
        stored = serializer.loads(serializer.dumps(stored))
    if in_history:
        stored = stored.history[0]

    for _ in range(2):
        assert stored.next.headers['X-Variant'] == value
        assert stored.next.headers['X-Private'] == 'REDACTED'
        assert stored.next.headers['Content-Type'] == 'text/plain'
    assert stored._next.headers['X-Variant'] == value.decode('latin-1')
    assert following.headers['X-Variant'] == value
    assert following.headers['X-Private'] == 'fabricated'


@pytest.mark.parametrize(
    'url, params',
    [
        ('https://example.com?foo=bar&param=1', None),
        ('https://example.com?foo=bar&param=1', {}),
        ('https://example.com/?foo=bar&param=1', {}),
        ('https://example.com?foo=bar&param=1&', {}),
        ('https://example.com?param=1&foo=bar', {}),
        ('https://example.com?param=1', {'foo': 'bar'}),
        ('https://example.com?foo=bar', {'param': '1'}),
        ('https://example.com', {'foo': 'bar', 'param': '1'}),
        ('https://example.com', {'param': '1', 'foo': 'bar'}),
        ('https://example.com', {'foo': 'bar', 'param': 1}),
        ('https://example.com?', {'foo': 'bar', 'param': '1'}),
    ],
)
def test_create_key__normalize_url_params(url, params):
    """All of the above variations should produce the same cache key"""
    request = Request(
        method='GET',
        url=url,
        params=params,
    )
    assert create_key(request) == CACHE_KEY


def test_create_key__normalize_key_only_params():
    request_1 = Request(method='GET', url='https://img.site.com/base/img.jpg?param_1')
    request_2 = Request(method='GET', url='https://img.site.com/base/img.jpg?param_2')
    assert create_key(request_1) != create_key(request_2)

    request_1 = Request(method='GET', url='https://img.site.com/base/img.jpg?k=v&param_1')
    request_2 = Request(method='GET', url='https://img.site.com/base/img.jpg?param_1&k=v')
    assert create_key(request_1) == create_key(request_2)


@pytest.mark.parametrize(
    'first, second, expected_match',
    [
        ('?q=a%2Bb', '?q=a+b', False),
        ('?q=%2B', '?q=+', False),
        ('?x=', '', False),
        ('?x=&x=1', '?x=1', False),
        ('?flag', '?flag=', False),
        ('?flag', '?flag&flag', False),
        ('?flag%2Bname', '?flag+name', False),
        ('?q=%26', '?q=&', False),
        ('?q=%252B', '?q=%2B', False),
        ('?q=a%20b', '?q=a+b', True),
        ('?q=%2b', '?q=%2B', True),
        ('?q=%3D', '?q==', True),
        ('?q=%C3%A9', '?q=calf%C3%A9', False),
        ('?q=%C3%A9', '?q=\xe9', True),
        ('?q=a%2Bb&x=', '?x=&q=a%2bb', True),
        ('?c%61lf%C3%A9', '?calf%C3%A9', True),
        ('?flag%20name', '?flag+name', True),
        ('?=value', '?value', False),
        ('?q=%FF', '?q=%FE', False),
        ('?q=%FF', '?q=%EF%BF%BD', False),
        ('?bad%FF=x', '?bad%FE=x', False),
        ('?bad%FF', '?bad%FE', False),
        ('?q=e%CC%81', '?q=%C3%A9', False),
    ],
)
def test_create_key__query_value_identity(first, second, expected_match):
    requests = [
        Request('GET', f'https://example.com/{query}').prepare() for query in (first, second)
    ]
    assert (create_key(requests[0]) == create_key(requests[1])) is expected_match


@pytest.mark.parametrize(
    'first, second, expected_match',
    [
        ('x=', '', False),
        ('x=&x=1', 'x=1', False),
        ('flag', 'flag=', False),
        ('x=a%2Bb', 'x=a+b', False),
        ('x=a%20b', 'x=a+b', True),
        ('x=&flag', 'flag&x=', True),
    ],
)
def test_create_key__form_value_identity(first, second, expected_match):
    requests = [
        Request(
            'POST',
            'https://example.com/',
            data=body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        ).prepare()
        for body in (first, second)
    ]
    assert (create_key(requests[0]) == create_key(requests[1])) is expected_match


@pytest.mark.parametrize('field', ['params', 'data'])
def test_create_key__ignored_blank_value(field):
    first = Request('GET', 'https://example.com/', **{field: {'secret': ''}}).prepare()
    second = Request('GET', 'https://example.com/', **{field: {'secret': 'fixture'}}).prepare()
    assert create_key(first, ignored_parameters=['secret']) == create_key(
        second, ignored_parameters=['secret']
    )
    redacted = normalize_request(first, ignored_parameters=['secret'])
    content = redacted.url if field == 'params' else redacted.body.decode()
    assert 'secret=REDACTED' in content


@pytest.mark.parametrize(
    'value, expected',
    [
        ('q=a%2bb', 'q=a%2Bb'),
        ('q=a%20b', 'q=a+b'),
        ('x=&x=&flag&flag', 'x=&x=&flag&flag'),
        ('=', '='),
        ('a%3Db&a%26b', 'a%26b&a%3Db'),
        ('a%252Bb&a%2Bb', 'a%252Bb&a%2Bb'),
        ('calf\xe9&flag%20name', 'calf%C3%A9&flag+name'),
        ('q=%FF&bad%FE', 'q=%FF&bad%FE'),
        ('q=a%3Bb&&', 'q=a%3Bb'),
        ('secret=&to%6Ben=fixture&token', 'secret=REDACTED&token=REDACTED&token'),
    ],
)
def test_normalize_params__canonicalisation(value, expected):
    for source in (value, value.encode()):
        result = normalize_params(source, ['secret', 'token'])
        assert result == expected
        assert normalize_params(result, ['secret', 'token']) == result


@pytest.mark.parametrize(
    'url, expected',
    [
        ('HTTP://B\xdcCHER.example:80/p;v?q=%23x#f', 'http://xn--bcher-kva.example/p;v?q=%23x#f'),
        ('https://example.com:443/p?x=', 'https://example.com/p?x='),
        ('https://example.com:8443/p?x=', 'https://example.com:8443/p?x='),
    ],
)
def test_normalize_url__query_isolation(url, expected):
    result = normalize_url(url, ['Authorization'])
    assert result == expected
    assert normalize_url(result, ['Authorization']) == result


def test_create_key__normalize_duplicate_params():
    request_1 = Request(method='GET', url='https://img.site.com/base/img.jpg?param_1=a&param_1=b')
    request_2 = Request(method='GET', url='https://img.site.com/base/img.jpg?param_1=a')
    request_3 = Request(method='GET', url='https://img.site.com/base/img.jpg?param_1=b')
    assert create_key(request_1) != create_key(request_2) != create_key(request_3)

    request_1 = Request(
        method='GET', url='https://img.site.com/base/img.jpg?param_1=a&param_1=b&k=v'
    )
    request_2 = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg?param_1=b&param_1=a',
        params={'k': 'v'},
    )
    assert create_key(request_1) == create_key(request_2)


def test_create_key__fips_hash_fallback():
    """Test that if blake2b fails due to FIPS mode, it creates a valid key using a different
    hash function. Fallback on TypeError and ValueError - both are possible in FIPS mode.
    """
    request = Request(method='GET', url='https://example.com')
    key_1 = create_key(request)

    with patch('requests_cache.cache_keys.blake2b') as mock_blake2b:
        mock_blake2b.side_effect = TypeError
        key_2 = create_key(request)
        mock_blake2b.side_effect = ValueError
        key_3 = create_key(request)

    assert key_1 != key_2
    assert key_2 == key_3  # Fallback to the same algorithm


def test_redact_response__escaped_params():
    """Test that redact_response() handles urlescaped request parameters"""
    url = 'https://img.site.com/base/img.jpg?where=code%3D123'
    request = Request(method='GET', url=url).prepare()
    response = Response()
    response.url = url
    response.request = request
    response.raw = HTTPResponse(request_url=url)
    redacted_response = redact_response(response, [])
    assert redacted_response.url == 'https://img.site.com/base/img.jpg?where=code%3D123'
    assert redacted_response.request.url == 'https://img.site.com/base/img.jpg?where=code%3D123'
    assert redacted_response.request.path_url == '/base/img.jpg?where=code%3D123'
    assert (
        redacted_response.raw._request_url == 'https://img.site.com/base/img.jpg?where=code%3D123'
    )
    if hasattr(redacted_response.raw, 'url'):
        assert redacted_response.raw.url == 'https://img.site.com/base/img.jpg?where=code%3D123'


@pytest.mark.parametrize(
    'content_type',
    [
        'application/json',
        'application/json; charset=utf-8',
        'application/vnd.api+json; charset=utf-8',
        'application/any_string+json',
    ],
)
@pytest.mark.parametrize(
    'data',
    [
        b'{"param_1": "value_1", "param_2": "value_2"}',
        b'["param_3", "param_2", "param_1"',
    ],
)
def test_normalize_request__json_body(data, content_type):
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        data=b'{"param_1": "value_1", "param_2": "value_2"}',
        headers={'Content-Type': content_type},
    )
    norm_request = normalize_request(request, ignored_parameters=['param_2'])
    assert norm_request.body == b'{"param_1": "value_1", "param_2": "REDACTED"}'


def test_normalize_request__json_body_list_filtered():
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        data=b'["param_3", "param_2", "param_1"]',
        headers={'Content-Type': 'application/json'},
    )
    norm_request = normalize_request(request, ignored_parameters=['param_2', 'param_1'])
    assert norm_request.body == b'["param_3"]'


def test_normalize_request__json_body_invalid():
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        data=b'invalid JSON!',
        headers={'Content-Type': 'application/json'},
    )
    assert normalize_request(request, ignored_parameters=['param_2']).body == b'invalid JSON!'


def test_normalize_request__json_body_empty():
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        data=b'{}',
        headers={'Content-Type': 'application/json'},
    )
    assert normalize_request(request, ignored_parameters=['param_2']).body == b'{}'


@pytest.mark.parametrize(
    'content_type',
    ['application/octet-stream', None],
)
def test_normalize_request__binary_body(content_type):
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        data=b'some bytes',
        headers={'Content-Type': content_type},
    )
    assert normalize_request(request, ignored_parameters=['param']).body == request.data


def test_normalize_request__oversized_body():
    body = {'param': '1', 'content': '0' * MAX_NORM_BODY_SIZE}
    encoded_body = json.dumps(body).encode('utf-8')

    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        json=body,
        headers={'Content-Type': 'application/octet-stream'},
    )
    assert normalize_request(request, ignored_parameters=['param']).body == encoded_body


def test_normalize_request__file_like_body():
    original_body = BytesIO(b'some bytes')
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        data=original_body,
        headers={'Content-Type': 'application/json'},
    )
    assert normalize_request(request).body == b'some bytes'
    assert original_body.read() == b'some bytes'


def test_normalize_request__file_like_reset_fails():
    """Test a request body with a file-like class that doesn't support seek()"""

    class CustomBytesIO:
        def __init__(self, content):
            self.content = content

        def __len__(self):
            return len(self.content)

        def read(self):
            return self.content

    original_body = CustomBytesIO(b'some bytes')
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        data=original_body,
        headers={'Content-Type': 'application/json'},
    )
    assert normalize_request(request).body == b'some bytes'
    assert original_body.read() == b'some bytes'


def test_normalize_headers__single_header_value_as_bytes():
    headers = {'Accept': b'gzip'}
    norm_headers = normalize_headers(headers)
    assert norm_headers == {'Accept': 'gzip'}


@pytest.mark.parametrize('value', [b'@@@SKIP_HEADER@@@', '@@@SKIP_HEADER@@@'])
def test_normalize_headers__transport_control(value):
    request = Request('GET', 'https://example.com/', data=b'body', headers={'Content-Type': value})
    normalized = normalize_request(request.prepare())
    assert normalized.headers['Content-Type'] == value
    assert normalized.body == b'body'
    literal = request.prepare()
    literal.headers['Content-Type'] = b'@@@SKIP_HEADER@@@'
    control = request.prepare()
    control.headers['Content-Type'] = '@@@SKIP_HEADER@@@'
    assert create_key(literal, match_headers=True) != create_key(control, match_headers=True)


def test_match_headers__byte_control_and_repr_string():
    literal = Request(
        'GET', 'https://example.com/', headers={'User-Agent': b'@@@SKIP_HEADER@@@'}
    ).prepare()
    quoted = Request(
        'GET', 'https://example.com/', headers={'User-Agent': "b'@@@SKIP_HEADER@@@'"}
    ).prepare()
    assert create_key(literal, match_headers=True) != create_key(quoted, match_headers=True)


def test_normalize_headers__multiple_header_values_as_bytes():
    headers = {'Accept': b'gzip,  deflate,Venmo,  PayPal, '}
    norm_headers = normalize_headers(headers)
    assert norm_headers == {'Accept': 'gzip,  deflate,Venmo,  PayPal, '}


def test_normalize_headers__single_header_value_as_string():
    headers = {'Accept': 'gzip'}
    norm_headers = normalize_headers(headers)
    assert norm_headers == {'Accept': 'gzip'}


def test_normalize_headers__multiple_header_values_as_string():
    headers = {'Accept': 'gzip,  deflate,Venmo,  PayPal, '}
    norm_headers = normalize_headers(headers)
    assert norm_headers == {'Accept': 'gzip,  deflate,Venmo,  PayPal, '}


def test_remove_ignored_headers__empty():
    request = Request(
        method='GET',
        url='https://img.site.com/base/img.jpg',
        headers={'foo': 'bar'},
    )
    assert normalize_request(request.prepare(), ignored_parameters=None).headers == request.headers
