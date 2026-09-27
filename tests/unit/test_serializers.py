# Note: Almost all serializer logic is covered by parametrized integration tests.
# Any additional serializer-specific tests can go here.
import gzip
import pickle
import sys
from importlib import reload
from unittest.mock import MagicMock, patch
from uuid import uuid4
from base64 import b64encode

import pytest
from cattrs import BaseConverter, GenConverter
from requests import Request
from urllib3.connection import HTTPConnection
from urllib3.util import SKIP_HEADER

from requests_cache import (
    CachedRequest,
    CachedResponse,
    CachedSession,
    CattrStage,
    SerializerPipeline,
    Stage,
    json_serializer,
    safe_pickle_serializer,
    utf8_encoder,
    init_serializer,
)
from tests.conftest import skip_missing_deps
from requests_cache.cache_keys import normalize_request, normalize_headers


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson', 'pickle', 'yaml', 'bson'])
@pytest.mark.parametrize(
    'value',
    [
        b'',
        b'ordinary',
        b'calf\xe9',
        b'\xc3\xa9',
        b'\xff\x80',
        b'\x85fixture',
        b'Mixed,  values\tinside ',
        SKIP_HEADER.encode(),
    ],
)
@pytest.mark.parametrize('location', ['request', 'next', 'history'])
def test_raw_byte_headers(serializer_name, value, location):
    """Serialisers preserve valid header octets when request normalisation is disabled."""
    if serializer_name not in ('json', 'pickle'):
        pytest.importorskip(serializer_name)
    prepared = Request(
        'GET',
        'https://example.com/final',
        data=b'fixture body',
        headers={
            'X-Fixture': value,
            'Content-Type': b'application/octet-stream',
            'X-Text': 'ordinary',
        },
    ).prepare()
    request = CachedRequest.from_request(prepared)
    response = CachedResponse(status_code=200, content=b'\x00\xff', request=request)
    if location == 'next':
        response = CachedResponse(status_code=302, next=request)
    elif location == 'history':
        response = CachedResponse(status_code=200, history=[response])
    serializer = init_serializer(serializer_name, decode_content=False)

    restored = serializer.loads(serializer.dumps(response))

    if location == 'history':
        restored = restored.history[0]
    stored_request = restored._next if location == 'next' else restored.request
    outgoing = stored_request.prepare()
    header = outgoing.headers['X-Fixture']
    assert (header.encode('latin-1') if isinstance(header, str) else header) == value
    assert outgoing.body == prepared.body
    assert outgoing.headers['X-Text'] == 'ordinary'
    assert prepared.headers['X-Fixture'] == value
    if serializer_name in ('pickle', 'yaml', 'bson'):
        assert stored_request.headers['X-Fixture'] == value


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson'])
@pytest.mark.parametrize('name', ['X-Fixture', 'User-Agent'])
@pytest.mark.parametrize('value', [SKIP_HEADER.encode(), SKIP_HEADER])
@pytest.mark.parametrize('normalise', [False, True])
def test_transport_control_header(serializer_name, name, value, normalise):
    """Literal bytes and the urllib3 string control must retain different transport behaviour."""
    if serializer_name != 'json':
        pytest.importorskip(serializer_name)
    prepared = Request('GET', 'https://example.com/', headers={name: value}).prepare()
    request = normalize_request(prepared) if normalise else prepared
    response = CachedResponse(status_code=200, next=CachedRequest.from_request(request))
    serializer = init_serializer(serializer_name, decode_content=False).copy()

    restored = serializer.loads(serializer.dumps(response)).next

    assert restored.headers[name] == value
    with patch('http.client.HTTPConnection.putheader') as emit:
        connection = HTTPConnection('example.com')
        if isinstance(value, bytes):
            connection.putheader(name, restored.headers[name])
            emit.assert_called_once_with(name, value)
        elif name == 'User-Agent':
            connection.putheader(name, restored.headers[name])
            emit.assert_not_called()
        else:
            with pytest.raises(ValueError):
                connection.putheader(name, restored.headers[name])
            emit.assert_not_called()


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson'])
@pytest.mark.parametrize('decode_content', [False, True])
@pytest.mark.parametrize(
    'content_type, body',
    [
        ('application/json', b'{"fixture": 1}'),
        ('text/plain', b'calf\xc3\xa9'),
        ('application/octet-stream', b'\x00\xff'),
    ],
)
def test_json_header_conversion_preserves_body(serializer_name, decode_content, content_type, body):
    if serializer_name != 'json':
        pytest.importorskip(serializer_name)
    response = CachedResponse(
        status_code=200,
        content=body,
        encoding='utf-8',
        headers={
            'Content-Type': content_type,
            'Content-Length': str(len(body)),
            'X-Fixture': b'calf\xe9',
        },
    )
    serializer = init_serializer(serializer_name, decode_content=decode_content).copy()

    stored = serializer.stages[0].dumps(response)
    restored = serializer.loads(serializer.dumps(response))

    if decode_content and content_type == 'application/json':
        assert restored.json() == {'fixture': 1}
    else:
        assert restored.content == body
    assert restored.headers['Content-Length'] == str(len(restored.content))
    assert restored.headers['X-Fixture'] == 'calf\xe9'
    assert response.headers['X-Fixture'] == b'calf\xe9'
    if decode_content and content_type != 'application/octet-stream':
        assert '_decoded_content' in stored
        assert '_content' not in stored
    else:
        assert stored['_content'] == b64encode(body).decode()
        assert '_decoded_content' not in stored


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson', 'pickle', 'yaml', 'bson'])
@pytest.mark.parametrize('normalise', [False, True])
@pytest.mark.parametrize(
    'content_type, body',
    [
        (b'application/json', b'{"fixture":1}'),
        (b'text/plain', b'calf\xc3\xa9'),
        (b'application/octet-stream', b'\x00\xff'),
        (SKIP_HEADER.encode(), b'\x00\xff'),
    ],
)
def test_decode_content__byte_content_type(serializer_name, normalise, content_type, body):
    if serializer_name not in ('json', 'pickle'):
        pytest.importorskip(serializer_name)
    headers = {'Content-Type': content_type}
    if normalise:
        headers = normalize_headers(headers, ignored_parameters=['unused'])
    response = CachedResponse(content=body, encoding='utf-8', headers=headers)
    serializer = init_serializer(serializer_name, decode_content=True)

    restored = serializer.loads(serializer.dumps(response))

    assert restored.content == body
    assert response.headers == headers
    if serializer_name in ('pickle', 'yaml', 'bson') or content_type == SKIP_HEADER.encode():
        assert restored.headers == headers
    else:
        assert restored.headers['Content-Type'] == content_type.decode('latin-1')


@skip_missing_deps('orjson')
@skip_missing_deps('ujson')
def test_json_aliases():
    assert init_serializer('json', decode_content=True).name == 'json'
    assert init_serializer('orjson', decode_content=True).name == 'orjson'
    assert init_serializer('ujson', decode_content=True).name == 'ujson'


@skip_missing_deps('ujson')
@skip_missing_deps('orjson')
def test_json_explicit_lib():
    from requests_cache.serializers.preconf import (
        json_serializer,
        orjson_serializer,
        ujson_serializer,
    )

    response = CachedResponse(status_code=200)
    for obj in [json_serializer, ujson_serializer, orjson_serializer]:
        assert obj.loads(obj.dumps(response)) == response


def test_optional_dependencies():
    import requests_cache.serializers.preconf

    with patch.dict(
        sys.modules,
        {'bson': None, 'itsdangerous': None, 'yaml': None, 'orjson': None, 'ujson': None},
    ):
        reload(requests_cache.serializers.preconf)

        from requests_cache.serializers.preconf import (
            bson_serializer,
            orjson_serializer,
            safe_pickle_serializer,
            ujson_serializer,
            yaml_serializer,
        )

        for obj in [bson_serializer, yaml_serializer, orjson_serializer, ujson_serializer]:
            print(f'Testing serializer {obj.name}')
            with pytest.raises(ImportError):
                obj.dumps('')
            with pytest.raises(ImportError):
                obj.loads('')

        with pytest.raises(ImportError):
            safe_pickle_serializer('')

    reload(requests_cache.serializers.preconf)


@skip_missing_deps('itsdangerous')
def test_cache_signing(tempfile_path):
    from itsdangerous import Signer
    from itsdangerous.exc import BadSignature

    serializer = safe_pickle_serializer(secret_key=str(uuid4()))
    session = CachedSession(tempfile_path, serializer=serializer)
    assert isinstance(session.cache.responses.serializer.stages[-1].obj, Signer)

    # Simple serialize/deserialize round trip
    response = CachedResponse()
    session.cache.responses['key'] = response
    assert session.cache.responses['key'] == response

    # Without the same signing key, the item shouldn't be considered safe to deserialize
    serializer = safe_pickle_serializer(secret_key='a different key')
    session = CachedSession(tempfile_path, serializer=serializer)
    with pytest.raises(BadSignature):
        session.cache.responses['key']


def test_custom_serializer(tempfile_path):
    serializer = SerializerPipeline(
        [
            json_serializer,  # Serialize to a JSON string
            utf8_encoder,  # Encode to bytes
            Stage(dumps=gzip.compress, loads=gzip.decompress),  # Compress
        ]
    )
    session = CachedSession(tempfile_path, serializer=serializer)
    response = CachedResponse()
    session.cache.responses['key'] = response
    assert session.cache.responses['key'] == response


def test_plain_pickle(tempfile_path):
    """`requests.Response` modifies pickling behavior. If plain `pickle` is used as a serializer,
    serializing `CachedResponse` should still work as expected.
    """
    session = CachedSession(tempfile_path, serializer=pickle)

    response = CachedResponse()
    session.cache.responses['key'] = response
    assert session.cache.responses['key'] == response
    assert session.cache.responses['key'].expires is None


def test_cattrs_compat():
    """CattrStage should be compatible with BaseConverter, which doesn't support the omit_if_default
    keyword arg.
    """
    stage_1 = CattrStage()
    assert isinstance(stage_1.converter, GenConverter)

    stage_2 = CattrStage(factory=BaseConverter)
    assert isinstance(stage_2.converter, BaseConverter)


def test_copy():
    stage_1 = CattrStage()
    stage_2 = Stage(MagicMock())
    serializer_1 = SerializerPipeline([stage_1, stage_2], name='test_serializer', is_binary=True)
    serializer_2 = serializer_1.copy()
    serializer_1.set_decode_content(True)

    assert serializer_1.name == serializer_2.name
    assert serializer_1.is_binary == serializer_2.is_binary
    for stage in serializer_1.stages:
        print(stage)
    for stage in serializer_2.stages:
        print(stage)
    assert serializer_1.stages[0].decode_content is True
    assert serializer_2.stages[0].decode_content is False
