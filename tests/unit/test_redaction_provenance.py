"""Cache-only matching distinguishes literal values from removed request data."""

import pytest
from requests import Request

from requests_cache import CachedRequest, CachedResponse, init_serializer
from requests_cache.cache_keys import normalize_request, redact_response
from requests_cache.policy import CacheActions, CacheSettings
from tests.conftest import MOCKED_URL


@pytest.mark.parametrize('header', ['Cookie', 'X-Variant', 'REDACTED'])
@pytest.mark.parametrize('ignored', [[], ['unused']])
def test_literal_redacted_header(mock_session, header, ignored):
    mock_session.trust_env = False
    mock_session.settings.ignored_parameters = ignored
    mock_session.mock_adapter.register_uri('GET', MOCKED_URL, headers={'Vary': header})
    mock_session.get(MOCKED_URL, headers={header: 'REDACTED'})

    response = mock_session.get(MOCKED_URL, headers={header: 'REDACTED'}, only_if_cached=True)

    assert response.status_code == 200
    assert response.from_cache
    assert mock_session.mock_adapter.call_count == 1


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson', 'pickle', 'yaml', 'bson'])
@pytest.mark.parametrize(
    'query, kwargs',
    [
        ('', {'json': ['kept', 1]}),
        ('', {'json': {'items': ['kept']}}),
        ('', {'json': {'value': 'REDACTED'}}),
        ('', {'data': b'REDACTED'}),
        ('?value=REDACTED', {}),
    ],
)
def test_unredacted_redirect_identity(mock_session, serializer_name, query, kwargs):
    if serializer_name not in ('json', 'pickle'):
        pytest.importorskip(serializer_name)
    mock_session.trust_env = False
    mock_session.settings.ignored_parameters = ['unused']
    mock_session.cache.responses.serializer = init_serializer(serializer_name, decode_content=False)
    start = f'{MOCKED_URL}/start'
    final = f'{MOCKED_URL}/final{query}'
    mock_session.mock_adapter.register_uri(
        'POST', start, status_code=307, headers={'Location': final}
    )
    mock_session.mock_adapter.register_uri('POST', final, headers={'Vary': 'Cookie'})
    mock_session.cookies.set('theme', 'light', path='/')
    first = mock_session.post(start, **kwargs)
    mock_session.cache.save_response(first)

    response = mock_session.post(final, only_if_cached=True, **kwargs)

    assert response.status_code == 200
    assert response.from_cache
    assert mock_session.mock_adapter.call_count == 2


@pytest.mark.parametrize('serializer_name', ['json', 'ujson', 'orjson', 'pickle', 'yaml', 'bson'])
@pytest.mark.parametrize('field', ['url', 'body', 'array', 'selected-array', 'header', 'vary'])
def test_removed_identity_survives_copy_and_storage(serializer_name, field):
    if serializer_name not in ('json', 'pickle'):
        pytest.importorskip(serializer_name)
    kwargs = {
        'url': {'params': {'secret': 'fixture'}},
        'body': {'json': {'secret': 'fixture'}},
        'array': {'json': ['secret', 'kept']},
        'selected-array': {'json': {'data': ['secret', 'kept']}},
        'header': {},
        'vary': {},
    }[field]
    prepared = Request(
        'POST', 'https://example.com/final', headers={'Cookie': 'fixture'}, **kwargs
    ).prepare()
    original = CachedResponse(
        url=prepared.url,
        request=CachedRequest.from_request(prepared),
        status_code=200,
        headers={'Vary': 'Cookie'},
        history=[
            CachedResponse(
                status_code=307,
                url='https://example.com/start',
                request=CachedRequest.from_request(
                    Request('POST', 'https://example.com/start').prepare()
                ),
            )
        ],
        redacted_fields=[],
    )
    ignored = ['Cookie'] if field == 'header' else ['Vary'] if field == 'vary' else ['secret']
    redact_response(original, iter(ignored), content_root_key='data')
    serializer = init_serializer(serializer_name, decode_content=False)
    stored = serializer.loads(serializer.dumps(CachedResponse.from_response(original)))
    current = stored.request.prepare()
    if field == 'vary':
        current.headers['REDACTED'] = 'fixture'

    # A later save and changed settings must not make removed values trustworthy again.
    redact_response(stored, ['unused'])
    stored = serializer.loads(serializer.dumps(stored))
    actions = CacheActions.from_request(
        'fixture-key',
        current,
        CacheSettings(only_if_cached=True, allowable_methods=('POST',), ignored_parameters=[]),
    )
    actions.update_from_cached_response(stored, lambda *_args, **_kwargs: 'fixture-key')

    assert actions.error_504
    assert not actions.send_request and not actions.resend_request


@pytest.mark.parametrize('normalise_first', [False, True])
def test_unknown_redaction_history_stays_unknown(normalise_first):
    prepared = Request('POST', 'https://example.com/', json=['kept']).prepare()
    if normalise_first:
        prepared = normalize_request(prepared, ignored_parameters=['secret'])
        cached = CachedRequest.from_request(prepared)
    else:
        # A record written before provenance existed has no evidence of an intact array.
        cached = CachedRequest(
            method=prepared.method, url=prepared.url, body=prepared.body, headers=prepared.headers
        )
    cached = CachedRequest.from_request(cached.copy().prepare())
    response = CachedResponse(
        status_code=200,
        request=cached,
        headers={'Vary': 'Cookie'},
        history=[CachedResponse(status_code=307)],
    )
    actions = CacheActions.from_request(
        'fixture-key',
        prepared,
        CacheSettings(only_if_cached=True, allowable_methods=('POST',), ignored_parameters=[]),
    )

    actions.update_from_cached_response(response, lambda *_args, **_kwargs: 'fixture-key')

    assert actions.error_504
    assert not actions.send_request and not actions.resend_request
