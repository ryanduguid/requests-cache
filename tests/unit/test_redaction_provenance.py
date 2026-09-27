"""Cache-only matching distinguishes literal values from removed request data."""

import pickle
from io import BytesIO

import pytest
from requests import Request

from requests_cache import CachedRequest, CachedResponse, cache_keys, init_serializer
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
@pytest.mark.parametrize('copy_prepared', [False, True])
def test_removed_identity_survives_copy_and_storage(serializer_name, field, copy_prepared):
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
    if copy_prepared:
        stored.request = CachedRequest.from_request(stored.request.prepare().copy().copy())
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


def test_large_unchanged_json_is_not_marked_as_lost(mock_session, monkeypatch):
    monkeypatch.setattr(cache_keys, 'MAX_NORM_BODY_SIZE', 10)
    test_unredacted_redirect_identity(mock_session, 'json', '', {'json': {'z': ['kept'], 'a': 1}})


def test_legacy_pickle_can_be_copied_and_redacted():
    original = CachedResponse(
        url='https://example.com/',
        request=CachedRequest.from_request(Request('GET', 'https://example.com/').prepare()),
    )
    del original.redacted_fields
    legacy = pickle.loads(pickle.dumps(original))

    copied = CachedResponse.from_response(legacy)
    redact_response(copied, ['unused'])

    assert copied.redacted_fields is None


@pytest.mark.parametrize('tuple_state', [False, True])
def test_legacy_request_state_keeps_new_loss_and_uncertainty(tuple_state):
    request = CachedRequest.from_request(
        Request('POST', 'https://example.com/', json=['secret', 'kept']).prepare()
    )
    state = request.__getstate__()
    state.pop('redacted_fields')
    if tuple_state:
        state = tuple(state.values())
    legacy = CachedRequest.__new__(CachedRequest)
    legacy.__setstate__(state)
    assert legacy.redacted_fields is None
    assert legacy.copy().prepare().copy().redacted_fields is None
    response = CachedResponse(url=request.url, request=legacy)

    redact_response(response, ['secret'])

    assert 'unknown' in response.request.redacted_fields
    assert 'body' in response.request.redacted_fields
    assert response.request.copy().redacted_fields == response.request.redacted_fields


@pytest.mark.parametrize('normalise', [False, True])
def test_stream_identity_does_not_become_trusted_text(normalise):
    request = Request('POST', 'https://example.com/', data=BytesIO(b'fixture')).prepare()
    if normalise:
        request = normalize_request(request)
    cached = CachedRequest.from_request(request)

    assert 'body' in cached.redacted_fields


def test_request_parameters_do_not_imply_redaction():
    cached = CachedRequest.from_request(
        normalize_request(
            Request('GET', 'https://example.com/', params={'kept': 'fixture'}), ['unused']
        )
    )
    assert cached.redacted_fields == []


def test_headerless_cookie_loss_survives_ignored_settings():
    request = Request('GET', 'https://example.com/', cookies={'theme': 'fixture'}).prepare()
    del request.headers['Cookie']
    stored = CachedResponse(
        url=request.url,
        request=CachedRequest.from_request(request),
        status_code=200,
        headers={'Vary': 'Cookie'},
    )
    stored.request.redacted_fields = None
    redact_response(stored, ['Cookie'])
    current = Request('GET', request.url).prepare()
    actions = CacheActions.from_request(
        'fixture-key', current, CacheSettings(only_if_cached=True, ignored_parameters=['Cookie'])
    )

    actions.update_from_cached_response(stored, lambda *_args, **_kwargs: 'fixture-key')

    assert actions.error_504
    assert not actions.send_request and not actions.resend_request


def test_filtered_normalisation_survives_native_copy():
    original = Request('GET', 'https://example.com/?secret=fixture').prepare()
    filtered = normalize_request(original, ['secret'])
    copied = CachedRequest.from_request(filtered.copy().copy())
    assert copied.redacted_fields == ['url']
    assert copied.url.endswith('secret=REDACTED')


@pytest.mark.parametrize('normalise_first', [False, True])
def test_normalisation_distinguishes_legacy_and_fresh_requests(normalise_first):
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

    assert actions.error_504 is not normalise_first
    assert not actions.send_request and not actions.resend_request
