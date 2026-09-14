"""Regression controls for cache matching, redaction and local cache recovery."""

import sqlite3
from time import monotonic
from unittest.mock import patch

import pytest
from requests import Request
from requests_mock import ANY, Adapter

from requests_cache import CachedSession
from requests_cache.backends import BaseCache
from requests_cache.backends.filesystem import FileCache
from requests_cache.backends.sqlite import SQLiteCache, SQLiteDict
from requests_cache.cache_keys import MAX_NORM_BODY_SIZE, create_key


@pytest.mark.parametrize(
    'first, second',
    [
        (
            {'url': 'https://example.invalid/a', 'data': 'bc'},
            {'url': 'https://example.invalid/ab', 'data': 'c'},
        ),
        (
            {'url': 'https://example.invalid/a', 'json': ['x', 'y']},
            {'url': 'https://example.invalid/a', 'json': ['y', 'x']},
        ),
        (
            {'url': 'https://example.invalid/a', 'headers': {'X-Variant': 'A,B'}},
            {'url': 'https://example.invalid/a', 'headers': {'X-Variant': 'a,b'}},
        ),
        (
            {'url': 'https://example.invalid/a', 'headers': {'X-Variant': 'A,B'}},
            {'url': 'https://example.invalid/a', 'headers': {'X-Variant': 'B,A'}},
        ),
    ],
)
def test_distinct_requests_do_not_share_a_response(first, second):
    adapter = Adapter()
    adapter.register_uri(
        'POST',
        ANY,
        text=lambda request, context: repr(
            (request.url, request.body, request.headers.get('X-Variant'))
        ),
    )
    with CachedSession(
        backend='memory', allowable_methods=['POST'], match_headers=['X-Variant']
    ) as session:
        session.mount('https://', adapter)
        initial = session.post(**first)
        different = session.post(**second)
        repeated = session.post(**second)

    assert not initial.from_cache and not different.from_cache and repeated.from_cache
    assert initial.text != different.text == repeated.text
    assert adapter.call_count == 2


@pytest.mark.parametrize(
    'header', ['Authorization', 'authorization', 'AUTHORIZATION', 'X-API-Key', 'X-API-KEY']
)
@pytest.mark.parametrize('response_header', [False, True])
def test_ignored_header_is_redacted_from_persisted_blob(tmp_path, header, response_header):
    marker = 'fabricated-header-marker'
    adapter = Adapter()
    adapter.register_uri(
        'GET', ANY, text='example', headers={header: marker} if response_header else {}
    )
    with CachedSession(str(tmp_path / 'headers'), backend='sqlite') as session:
        session.mount('https://', adapter)
        response = session.get('https://example.invalid/a', headers={header: marker})
        cached = session.cache.responses[response.cache_key]
        assert cached.request.headers[header] == 'REDACTED'
        if response_header:
            assert cached.headers[header] == 'REDACTED'
        with session.cache.responses.connection() as connection:
            blob = connection.execute('SELECT value FROM responses').fetchone()[0]
        assert marker.encode() not in blob


@pytest.mark.parametrize('size', [20, MAX_NORM_BODY_SIZE + 1])
def test_ignored_json_is_redacted_above_matching_size_limit(tmp_path, size):
    marker = 'fabricated-json-marker'
    adapter = Adapter()
    adapter.register_uri('POST', ANY, text='example')
    with CachedSession(
        str(tmp_path / 'json'), allowable_methods=['POST'], ignored_parameters=['private']
    ) as session:
        session.mount('https://', adapter)
        response = session.post(
            'https://example.invalid/a', json={'private': marker, 'padding': 'x' * size}
        )
        cached = session.cache.responses[response.cache_key]
        assert marker.encode() not in cached.request.body
        assert b'REDACTED' in cached.request.body
        with session.cache.responses.connection() as connection:
            blob = connection.execute('SELECT value FROM responses').fetchone()[0]
        assert marker.encode() not in blob


def test_ignored_header_names_do_not_change_body_or_query_case_rules():
    first = Request('POST', 'https://example.invalid/?PRIVATE=first', json={'PRIVATE': 'first'})
    second = Request('POST', 'https://example.invalid/?PRIVATE=second', json={'PRIVATE': 'second'})
    assert create_key(first, ignored_parameters=['private']) != create_key(
        second, ignored_parameters=['private']
    )


def test_sqlite_recovery_closes_both_connections(tmp_path):
    cache = SQLiteCache(tmp_path / 'recovery', serializer=None)
    try:
        cache.responses['response'] = 'value'
        cache.redirects['redirect'] = 'response'
        with patch.object(
            BaseCache, 'clear', side_effect=sqlite3.DatabaseError('Fabricated failure')
        ):
            cache.clear()
        assert len(cache.responses) == len(cache.redirects) == 0
        cache.responses['new'] = 'value'
        assert cache.responses['new'] == 'value'
    finally:
        cache.close()


def test_file_cache_clear_removes_redirects_and_remains_usable(tmp_path):
    cache = FileCache(tmp_path / 'files')
    try:
        cache.redirects['redirect'] = 'response'
        cache.clear()
        assert len(cache.redirects) == 0
        cache.redirects['new'] = 'response'
        assert cache.redirects['new'] == 'response'
    finally:
        cache.close()


@pytest.mark.parametrize('options', [{'timeout': 0.05}, {'timeout': 2, 'busy_timeout': 50}])
def test_sqlite_lock_contention_obeys_connection_timeout(tmp_path, options):
    cache = SQLiteDict(tmp_path / 'locked', serializer=None, **options)
    competing = sqlite3.connect(cache.db_path, isolation_level=None)
    competing.execute('BEGIN IMMEDIATE')
    try:
        # The independent lock is released even if a faulty retry loop outlives the bound.
        with patch.object(cache, '_connection', wraps=cache._connection) as connection:
            execute = connection.execute
            started = monotonic()

            def bounded_execute(statement):
                if monotonic() - started > 0.5:
                    raise AssertionError('The configured SQLite timeout was exceeded')
                return execute._mock_wraps(statement)

            execute.side_effect = bounded_execute
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                with cache._acquire_sqlite_lock():
                    pytest.fail('The competing write lock must prevent entry')
            assert execute.call_count == 1
            assert not cache._active_transaction
    finally:
        competing.rollback()
        competing.close()
        cache.close()


def test_sqlite_lock_does_not_retry_unrelated_operational_errors(tmp_path):
    cache = SQLiteDict(tmp_path / 'other-error', serializer=None)
    try:
        with patch.object(cache, '_connection') as connection:
            connection.execute.side_effect = [
                sqlite3.OperationalError('Malformed statement'),
                AssertionError('Retried'),
            ]
            with pytest.raises(sqlite3.OperationalError, match='Malformed statement'):
                with cache._acquire_sqlite_lock():
                    pytest.fail('The failed transaction must not start')
            assert connection.execute.call_count == 1
    finally:
        cache.close()
