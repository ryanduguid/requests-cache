import pytest
from requests.exceptions import InvalidHeader
from requests.utils import default_headers

from requests_cache.models.response import CachedRequest
from tests.conftest import MOCKED_URL


def test_from_request(mock_session):
    response = mock_session.get(MOCKED_URL, data=b'mock request', headers={'foo': 'bar'})
    request = CachedRequest.from_request(response.request)
    expected_headers = {**default_headers(), 'Content-Length': '12', 'foo': 'bar'}

    assert response.request.body == request.body == b'mock request'
    assert response.request.headers == request.headers == expected_headers
    assert response.request.method == request.method == 'GET'
    assert response.request.path_url == request.path_url == '/text'
    assert response.request.url == request.url == MOCKED_URL
    assert response.request._cookies == request._cookies == request.cookies == {}


@pytest.mark.parametrize('value', ['ordinary', 'calf\xe9', b'ordinary', b'calf\xe9'])
def test_prepare__ordinary_header_types(value):
    request = CachedRequest(method='GET', url='https://example.com', headers={'X-Variant': value})
    prepared = request.prepare()
    assert prepared.headers['X-Variant'] == value
    assert type(prepared.headers['X-Variant']) is type(value)
    assert request.headers['X-Variant'] == value


@pytest.mark.parametrize(
    'name, value',
    [
        ('X-Variant', ' fixture'),
        ('X-Variant', '\tfixture'),
        ('X-Variant', '\u2003fixture'),
        ('X-Variant', '\x85fixture\r'),
        ('X-Variant', '\x85fixture\n'),
        ('X-Variant', '\xa0fixture\r'),
        ('X-Variant', '\xa0fixture\n'),
        ('X-Variant', '\x85fixture\r\nInjected: 1'),
        ('X-Variant', 'fixture\r\nInjected: 1'),
        ('X:Invalid', '\x85fixture'),
        (' Invalid', '\xa0fixture'),
    ],
)
def test_prepare__invalid_headers(name, value):
    request = CachedRequest(method='GET', url='https://example.com', headers={name: value})
    with pytest.raises(InvalidHeader):
        request.prepare()
    assert request.headers[name] == value
