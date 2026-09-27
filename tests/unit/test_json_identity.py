"""JSON matching and decoded storage must preserve retained values and types."""

import json
from decimal import Decimal, localcontext
from unittest.mock import patch

import pytest
from requests import Request

from requests_cache import CachedRequest, CachedResponse, CachedSession, init_serializer
from requests_cache.cache_keys import (
    create_key,
    normalize_json_body,
    normalize_request,
    redact_response,
)
from requests_cache.policy.actions import CacheActions
from requests_cache.policy.settings import CacheSettings


def prepared(body, content_type='application/json'):
    return Request(
        'POST', 'https://example.com/item', data=body, headers={'Content-Type': content_type}
    ).prepare()


@pytest.mark.parametrize(
    'first, second',
    [
        ('{"x":1.00000000000000001}', '{"x":1.00000000000000002}'),
        ('{"x":1e400}', '{"x":1e401}'),
        ('{"x":1e-400}', '{"x":0.0}'),
        ('"abc"', '["a","b","c"]'),
        ('"123"', '123'),
        ('"true"', 'true'),
        ('"null"', 'null'),
        ('[1.00000000000000001]', '["1.00000000000000001"]'),
    ],
)
def test_json_request_identity(first, second):
    assert create_key(prepared(first)) != create_key(prepared(second))


@pytest.mark.parametrize(
    'body', ['"abc"', '123', '1.234', 'true', 'false', 'null', '""', '[]', '{}']
)
def test_json_scalar_type(body):
    actual = json.loads(normalize_json_body(body, []), parse_float=Decimal)
    expected = json.loads(body, parse_float=Decimal)
    assert type(actual) is type(expected)
    assert actual == expected


@pytest.mark.parametrize(
    'number',
    [
        '1.00000000000000001',
        '1e400',
        '1e-400',
        '1e9999999999999999999',
        '7' * 5000,
    ],
)
def test_precise_numbers_do_not_bypass_redaction(number):
    original = '{"secret":"fixture-private","value":' + number + '}'
    result = normalize_json_body(original, ['secret'])
    assert json.loads(result, parse_float=str, parse_int=str) == {
        'secret': 'REDACTED',
        'value': number,
    }
    assert 'fixture-private' not in result
    assert create_key(prepared(original), ignored_parameters=['secret']) == create_key(
        prepared(original.replace('fixture-private', 'different-fixture')),
        ignored_parameters=['secret'],
    )


@pytest.mark.parametrize(
    'number, expected',
    [
        ('0.1', '0.1'),
        ('1.2300', '1.23'),
        ('1e0', '1.0'),
        ('1e-1', '0.1'),
        ('-0.0', '-0.0'),
        ('0.0', '0.0'),
    ],
)
def test_exact_ordinary_number_normalisation(number, expected):
    with localcontext() as context:
        context.prec = 2
        context.capitals = 0
        assert normalize_json_body('[{}]'.format(number), []) == '[{}]'.format(expected)


@pytest.mark.parametrize('location', ['request', 'history', 'next'])
@pytest.mark.parametrize('content_type', ['application/json', b'application/json'])
@pytest.mark.parametrize('ignore_content_type', [False, True])
def test_json_redaction_saved_root(location, content_type, ignore_content_type):
    body = b'{"data":{"secret":"fixture-private","x":1.00000000000000001},"sibling":1e400}'
    request = prepared(body, content_type)
    cached_request = CachedRequest.from_request(request)
    response = CachedResponse(url=request.url, request=cached_request, status_code=200)
    if location == 'history':
        response.status_code = 302
        response = CachedResponse(
            url=request.url, request=cached_request, history=[response], status_code=200
        )
    elif location == 'next':
        response = CachedResponse(
            url=request.url, request=cached_request, next=cached_request, status_code=302
        )
    ignored = ['secret', 'Content-Type'] if ignore_content_type else ['secret']
    with CachedSession(
        backend='memory', ignored_parameters=ignored, content_root_key='data'
    ) as session:
        session.trust_env = False
        key = session.cache.create_key(request)
        normalised = normalize_request(request, ignored, 'data')
        assert b'fixture-private' not in normalised.body
        session.cache.save_response(response, key)
        stored = session.cache.responses[key]
    if location == 'history':
        stored = stored.history[0]
    stored_request = stored._next if location == 'next' else stored.request
    assert b'fixture-private' not in stored_request.body
    assert json.loads(stored_request.body, parse_float=str) == {
        'data': {'secret': 'REDACTED', 'x': '1.00000000000000001'},
        'sibling': '1e400',
    }
    assert stored_request.body == normalised.body
    assert request.body == body
    assert cached_request.body == body
    if ignore_content_type:
        assert stored_request.headers['Content-Type'] == 'REDACTED'


@pytest.mark.parametrize(
    'first, second',
    [
        ('{"x":1.00000000000000001}', '{"x":1.00000000000000002}'),
        ('"abc"', '["a","b","c"]'),
    ],
)
def test_json_cache_only_identity(first, second):
    with CachedSession(backend='memory', allowable_methods=('GET', 'POST')) as session:
        session.trust_env = False
        request = prepared(first)
        response = CachedResponse(
            url=request.url,
            request=CachedRequest.from_request(request),
            content=b'first response',
            status_code=200,
        )
        session.cache.save_response(response)
        with patch(
            'requests.Session.send', side_effect=AssertionError('Unexpected network request')
        ):
            actual = session.send(prepared(second), only_if_cached=True)
        assert actual.status_code == 504


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson', 'pickle', 'yaml', 'bson'])
@pytest.mark.parametrize(
    'body',
    [
        b'{"x":1.00000000000000001}',
        b'{"x":1e400}',
        b'{"x":1e-400}',
        b'null',
        b'{"x":1,"x":2}',
        b'{"x":18446744073709551616}',
        b'{"x":NaN}',
        b'{"x":-0}',
    ],
)
def test_decoded_json_falls_back_without_loss(serializer_name, body):
    if serializer_name not in ('json', 'pickle'):
        pytest.importorskip(serializer_name)
    response = CachedResponse(
        content=body,
        encoding='ascii',
        headers={
            'Content-Type': 'application/json',
            'Content-Length': str(len(body)),
        },
    )
    serializer = init_serializer(serializer_name, decode_content=True)
    stored = serializer.stages[0].dumps(response)
    restored = serializer.loads(serializer.dumps(response))
    assert restored.content == body
    assert restored.encoding == 'ascii'
    assert restored.headers['Content-Length'] == str(len(body))
    assert response.content == body
    assert '_decoded_content' not in stored
    assert '_content' in stored


@pytest.mark.parametrize('body', [b'false', b'0', b'""', b'[]', b'{}', b'{"x":0.1}', b'1.25'])
def test_decoded_json_keeps_safe_values_readable(body):
    response = CachedResponse(content=body, headers={'Content-Type': 'application/json'})
    serializer = init_serializer('json', decode_content=True)
    stored = serializer.stages[0].dumps(response)
    restored = serializer.loads(serializer.dumps(response))
    assert '_decoded_content' in stored
    assert '_content' not in stored
    assert json.loads(restored.content) == json.loads(body)


@pytest.mark.parametrize('body', [b'{"x":1.00000000000000001}', b'null', b'{"x":1}'])
def test_text_json_round_trip(body):
    response = CachedResponse(content=body, headers={'Content-Type': 'text/json'})
    serializer = init_serializer('json', decode_content=True)
    restored = serializer.loads(serializer.dumps(response))
    assert json.loads(restored.content, parse_float=Decimal) == json.loads(
        body, parse_float=Decimal
    )


def test_extreme_number_preserves_decimal_context():
    with localcontext() as context:
        context.prec = 2
        context.clear_flags()
        original_flags = context.flags.copy()
        for signal in context.traps:
            context.traps[signal] = False
        result = normalize_json_body('{"x":1e9999999999999999999}', [])
        assert result == '{"x": 1e9999999999999999999}'
        assert context.flags == original_flags


@pytest.mark.parametrize(
    'selected, expected',
    [
        ('["secret","keep",1.00000000000000001]', ['keep', Decimal('1.00000000000000001')]),
        ('"secret"', 'secret'),
        ('false', False),
        ('null', None),
        ('1.00000000000000001', Decimal('1.00000000000000001')),
    ],
)
def test_selected_json_root_retains_type(selected, expected):
    body = '{"data":' + selected + ',"secret":"outer fixture"}'
    result = normalize_json_body(body, ['secret'], 'data')
    assert json.loads(result, parse_float=Decimal) == {'data': expected, 'secret': 'outer fixture'}


def test_missing_root_uses_existing_top_level_policy():
    result = normalize_json_body('{"secret":"fixture-private","x":1e400}', ['secret'], 'missing')
    assert json.loads(result, parse_float=Decimal) == {'secret': 'REDACTED', 'x': Decimal('1e400')}


def test_duplicate_request_members_keep_last_member_policy():
    result = normalize_json_body(
        '{"secret":"first fixture","secret":"second fixture","x":1,"x":2}', ['secret']
    )
    assert json.loads(result) == {'secret': 'REDACTED', 'x': 2}


def test_numeric_fallback_keeps_string_escaping():
    body = '{"quote\\"":"line\\n\\\\tail","x":1.00000000000000001}'
    result = normalize_json_body(body, [])
    assert json.loads(result, parse_float=Decimal) == json.loads(body, parse_float=Decimal)


def test_json_redaction_failure_does_not_restore_original_body():
    with patch('requests_cache.cache_keys._json.dumps', side_effect=ValueError('Fixture failure')):
        with pytest.raises(ValueError, match='Fixture failure'):
            normalize_json_body('{"secret":"fixture-private"}', ['secret'])


def test_oversized_json_keeps_redaction_requirement():
    body = '{"padding":"' + ('a' * 1000) + '","secret":"fixture-private","x":1.00000000000000001}'
    with patch('requests_cache.cache_keys.MAX_NORM_BODY_SIZE', 100):
        assert normalize_json_body(body, []) == body
        result = normalize_json_body(body, ['secret'])
    assert 'fixture-private' not in result
    assert json.loads(result, parse_float=Decimal)['x'] == Decimal('1.00000000000000001')


def test_decoded_json_fallback_clears_stale_decoded_content():
    response = CachedResponse(content=b'{"x":1e400}', headers={'Content-Type': 'application/json'})
    response._decoded_content = {'stale': 'fixture'}
    serializer = init_serializer('json', decode_content=True)
    restored = serializer.loads(serializer.dumps(response))
    assert restored.content == response.content


def test_response_json_without_optional_encoder(monkeypatch):
    from requests_cache.serializers import cattrs

    monkeypatch.setattr(cattrs, 'json', json)
    response = CachedResponse(
        content=b'{"x":1.00000000000000001}', headers={'Content-Type': 'application/json'}
    )
    serializer = init_serializer('json', decode_content=True)
    assert serializer.loads(serializer.dumps(response)).content == response.content


@pytest.mark.parametrize(
    'body',
    [
        b'{"n":9007199254740993}',
        b'{"n":1e200}',
        b'{"n":1e-200}',
        b'{"n":1}',
        b'{"n":1.0}',
        b'{"n":-0.0}',
        b'[1,2,3]',
        b'1.25',
    ],
)
@pytest.mark.parametrize('stdlib_encoder', [False, True])
def test_dynamodb_numbers_keep_original_body(body, stdlib_encoder, monkeypatch):
    pytest.importorskip('boto3')
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
    from requests_cache.serializers import cattrs, dynamodb_document_serializer

    if stdlib_encoder:
        monkeypatch.setattr(cattrs, 'json', json)
    serializer = init_serializer(dynamodb_document_serializer, decode_content=True).copy()
    response = CachedResponse(content=body, headers={'Content-Type': 'application/json'})
    stored = serializer.dumps(response)
    assert '_content' in stored and '_decoded_content' not in stored
    sdk_value = TypeDeserializer().deserialize(TypeSerializer().serialize(stored))
    assert serializer.loads(sdk_value).content == body


@pytest.mark.parametrize('body', [b'{"s":"value"}', b'["a",false]', b'"text"'])
def test_dynamodb_number_free_json_stays_readable(body):
    pytest.importorskip('boto3')
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
    from requests_cache.serializers import dynamodb_document_serializer

    serializer = init_serializer(dynamodb_document_serializer, decode_content=True)
    response = CachedResponse(content=body, headers={'Content-Type': 'application/json'})
    stored = serializer.dumps(response)
    assert '_decoded_content' in stored and '_content' not in stored
    restored = serializer.loads(TypeDeserializer().deserialize(TypeSerializer().serialize(stored)))
    assert json.loads(restored.content) == json.loads(body)


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson', 'bson', 'yaml', 'pickle'])
@pytest.mark.parametrize('body', [b'{"s":"\\ud800"}', b'{"\\u0000":"value"}', b'["\\udfff"]'])
def test_decoded_json_unsupported_strings_keep_body(serializer_name, body):
    if serializer_name not in ('json', 'pickle'):
        pytest.importorskip(serializer_name)
    serializer = init_serializer(serializer_name, decode_content=True)
    response = CachedResponse(content=body, headers={'Content-Type': 'application/json'})
    assert serializer.loads(serializer.dumps(response)).content == body


@pytest.mark.parametrize(
    'current_body, final_body, current_root, expected',
    [
        (b'{"data":{"z":1,"a":2}}', b'{"data":{"z":1,"a":2}}', 'data', True),
        (b'{"data":["keep"]}', b'{"data":["secret","keep"]}', 'data', False),
        (b'{"data":["keep"]}', b'{"data":["secret","keep"]}', None, False),
        (b'{"data":[]}', b'{"data":["secret"]}', 'data', False),
        (b'["keep"]', b'["secret","keep"]', 'missing', False),
    ],
)
def test_selected_root_redirect_identity(current_body, final_body, current_root, expected):
    current, final = prepared(current_body), prepared(final_body)
    current.headers['Cookie'] = final.headers['Cookie'] = 'sid=fixture'
    response = CachedResponse(
        request=CachedRequest.from_request(final),
        status_code=200,
        headers={'Vary': 'Cookie'},
        history=[
            CachedResponse(status_code=307, request=CachedRequest(url='https://example.com/start'))
        ],
    )
    redact_response(response, ['secret'], 'data')
    settings = CacheSettings(
        ignored_parameters=['secret'],
        content_root_key=current_root,
        allowable_methods=('GET', 'POST'),
        only_if_cached=True,
    )
    actions = CacheActions.from_request('fixture-key', current, settings)
    actions.update_from_cached_response(response, lambda *_args, **_kwargs: 'constant-key')
    assert actions.error_504 is not expected
    assert not actions.send_request and not actions.resend_request


def test_one_shot_ignored_parameters_cover_all_request_parts():
    request = Request(
        'POST',
        'https://example.com/?secret=fixture-query',
        data=b'{"secret":"fixture-body"}',
        headers={'Content-Type': 'application/json', 'secret': 'fixture-header'},
    ).prepare()
    result = normalize_request(request, iter(['secret']))
    assert result.url.endswith('secret=REDACTED')
    assert result.headers['secret'] == 'REDACTED'
    assert json.loads(result.body) == {'secret': 'REDACTED'}
    response = CachedResponse(request=CachedRequest.from_request(request), url=request.url)
    redact_response(response, iter(['secret']))
    assert response.request.headers['secret'] == 'REDACTED'
    assert json.loads(response.request.body) == {'secret': 'REDACTED'}


def test_one_shot_settings_remain_available_for_later_requests():
    with CachedSession(backend='memory', ignored_parameters=iter(['secret'])) as session:
        session.trust_env = False
        for i in range(2):
            request = prepared('{"secret":"fixture-' + str(i) + '"}')
            key = session.cache.create_key(request)
            response = CachedResponse(request=CachedRequest.from_request(request), status_code=200)
            session.cache.save_response(response, key)
            assert json.loads(session.cache.responses[key].request.body) == {'secret': 'REDACTED'}
