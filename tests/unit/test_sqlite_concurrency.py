"""Concurrent cache operations must preserve rows and transaction ownership."""

import pickle
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from requests_cache.backends.sqlite import SQLiteCache, SQLiteDict
from requests_cache.models import CachedResponse


@pytest.mark.parametrize('serializer', [None, 'pickle'])
def test_shared_connection_reads_preserve_values(tmp_path, serializer):
    cache = SQLiteDict(tmp_path / 'concurrent.sqlite', serializer=serializer)
    try:
        values = {f'key-{i}': f'fixture-{i}'.encode() * 20 for i in range(8)}
        cache.update(values)
        barrier = Barrier(12)

        def read(worker):
            barrier.wait(timeout=10)
            for index in range(200):
                key = f'key-{(index + worker) % len(values)}'
                assert cache[key] == values[key]

        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(read, range(12)))
    finally:
        cache.close()


@pytest.mark.parametrize('key', ["quoted'key", "x' OR 1=1 --", 'plain'])
def test_sqlite_keys_are_bound_values(tmp_path, key):
    cache = SQLiteDict(tmp_path / 'keys.sqlite', serializer=None)
    try:
        cache['first'] = b'first fixture'
        cache[key] = b'key fixture'
        assert cache[key] == b'key fixture'
        with pytest.raises(KeyError):
            cache["missing' OR 1=1 --"]
    finally:
        cache.close()


def test_close_waits_for_active_cursor(tmp_path):
    cache = SQLiteDict(tmp_path / 'close.sqlite', serializer=None)
    cache['key'] = b'fixture'
    started, closed = Event(), Event()

    def close():
        started.set()
        cache.close()
        closed.set()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with cache.connection() as connection:
                cursor = connection.execute('SELECT value FROM http_cache WHERE key=?', ('key',))
                future = executor.submit(close)
                assert started.wait(timeout=2)
                assert not closed.wait(timeout=0.1)
                assert cursor.fetchone() == (b'fixture',)
                cursor.close()
            future.result(timeout=2)
        assert closed.is_set()
    finally:
        cache.close()


@pytest.mark.parametrize(
    'error_type',
    [RuntimeError, sqlite3.OperationalError, sqlite3.IntegrityError, KeyboardInterrupt],
)
def test_failed_transaction_rolls_back_and_reports_failure(tmp_path, error_type):
    cache = SQLiteDict(tmp_path / 'rollback.sqlite', serializer=None)
    try:
        with pytest.raises(error_type, match='Fixture failure'):
            with cache.bulk_commit():
                cache['partial'] = b'fixture'
                raise error_type('Fixture failure')
        assert 'partial' not in cache
        cache['later'] = b'complete fixture'
        assert cache['later'] == b'complete fixture'
    finally:
        cache.close()


@pytest.mark.parametrize('value', [b'', b'\x80\x05'])
def test_truncated_pickle_is_an_invalid_cache_entry(tmp_path, value):
    cache = SQLiteDict(tmp_path / 'invalid.sqlite')
    try:
        with cache.connection(commit=True) as connection:
            connection.execute('INSERT INTO http_cache (key,value) VALUES (?,?)', ('key', value))
        assert cache['key'] is None
    finally:
        cache.close()


def test_connection_context_excludes_other_threads(tmp_path):
    cache = SQLiteDict(tmp_path / 'owner.sqlite')

    def can_acquire():
        acquired = cache._lock.acquire(blocking=False)
        if acquired:
            cache._lock.release()
        return acquired

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with cache.connection():
                assert executor.submit(can_acquire).result(timeout=2) is False
            assert executor.submit(can_acquire).result(timeout=2) is True
    finally:
        cache.close()


@pytest.mark.parametrize('method', ['keys', 'sorted'])
@pytest.mark.parametrize('operation', ['read', 'write', 'close'])
def test_paused_iteration_allows_other_operations(tmp_path, method, operation):
    cache = SQLiteDict(tmp_path / 'iterator.sqlite')
    for key in ['a', 'b']:
        cache[key] = CachedResponse(status_code=200, content=key.encode())
    iterator = iter(cache) if method == 'keys' else cache.sorted(key='key')
    started, completed = Event(), Event()

    def operate():
        started.set()
        try:
            if operation == 'read':
                assert cache['a'].content == b'a'
            elif operation == 'write':
                cache['c'] = CachedResponse(status_code=200, content=b'c')
            else:
                cache.close()
        finally:
            completed.set()

    try:
        next(iterator)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(operate)
            try:
                assert started.wait(timeout=2)
                finished_while_paused = completed.wait(timeout=0.2)
            finally:
                # A failed implementation must release its generator lock before executor shutdown.
                iterator.close()
            future.result(timeout=2)
            assert finished_while_paused
    finally:
        iterator.close()
        cache.close()


def test_sorted_iteration_keeps_selected_keys_and_reads_current_values(tmp_path):
    cache = SQLiteDict(tmp_path / 'changes.sqlite')
    try:
        for key in ['a', 'b', 'c', 'd']:
            cache[key] = key
        iterator = cache.sorted(key='key', limit=3)
        assert next(iterator) == 'a'
        del cache['b']
        cache['c'] = 'updated c'
        cache['aa'] = 'later insertion'
        assert list(iterator) == ['updated c']
    finally:
        cache.close()


@pytest.mark.parametrize('method', ['keys', 'sorted'])
def test_paused_iteration_can_resume_after_close(tmp_path, method):
    cache = SQLiteDict(tmp_path / 'reopen.sqlite')
    try:
        cache.update({'a': 'first', 'b': 'second'})
        iterator = iter(cache) if method == 'keys' else cache.sorted(key='key')
        assert next(iterator) == ('a' if method == 'keys' else 'first')
        cache.close()
        assert list(iterator) == ['b' if method == 'keys' else 'second']
    finally:
        cache.close()


@pytest.mark.parametrize('method', ['get', 'sorted'])
def test_deserialisation_does_not_hold_connection_lock(tmp_path, method):
    cache = SQLiteDict(tmp_path / 'callbacks.sqlite')

    def can_acquire():
        acquired = cache._lock.acquire(blocking=False)
        if acquired:
            cache._lock.release()
        return acquired

    class Serializer:
        def loads(self, value):
            assert executor.submit(can_acquire).result(timeout=2)
            return pickle.loads(value)

        def dumps(self, value):
            return pickle.dumps(value)

    try:
        cache.serializer = Serializer()
        cache['key'] = 'fixture'
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = cache['key'] if method == 'get' else next(cache.sorted())
        assert result == 'fixture'
    finally:
        cache.close()


def test_bulk_transaction_owns_reads_until_rollback(tmp_path):
    cache = SQLiteDict(tmp_path / 'bulk.sqlite')
    started, completed = Event(), Event()

    def read():
        started.set()
        result = cache.get('partial')
        completed.set()
        return result

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(RuntimeError, match='Fixture rollback'):
                with cache.bulk_commit():
                    cache['partial'] = 'fixture'
                    future = executor.submit(read)
                    assert started.wait(timeout=2)
                    assert not completed.wait(timeout=0.1)
                    raise RuntimeError('Fixture rollback')
            assert future.result(timeout=2) is None
        cache['complete'] = 'later fixture'
        assert cache['complete'] == 'later fixture'
    finally:
        cache.close()


@pytest.mark.parametrize('value', [False, 0, b'', CachedResponse(status_code=404)])
def test_sorted_keeps_false_valued_entries(tmp_path, value):
    cache = SQLiteDict(tmp_path / 'false-values.sqlite')
    try:
        cache['key'] = value
        results = list(cache.sorted())
        assert len(results) == 1
        if isinstance(value, CachedResponse):
            assert results[0].status_code == 404
        else:
            assert results[0] == value
    finally:
        cache.close()


@pytest.mark.parametrize('limit, expected', [(None, 3), (0, 0), (2, 2), (-1, 3)])
def test_sorted_limit_is_a_maximum(tmp_path, limit, expected):
    cache = SQLiteDict(tmp_path / 'limits.sqlite')
    try:
        cache.update({'a': 'one', 'b': 'two', 'c': 'three'})
        assert len(list(cache.sorted(limit=limit))) == expected
    finally:
        cache.close()


def test_sorted_preserves_text_and_blob_keys_with_byte_text_factory(tmp_path):
    class ByteTextConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.text_factory = bytes

    cache = SQLiteDict(tmp_path / 'converted-keys.sqlite', factory=ByteTextConnection)
    try:
        cache['key'] = CachedResponse(status_code=200, content=b'text key')
        cache[b'key'] = CachedResponse(status_code=200, content=b'blob key')
        results = list(cache.sorted(key='key'))
        assert [response.content for response in results] == [b'text key', b'blob key']
        assert [response.cache_key for response in results] == [b'key', b'key']
    finally:
        cache.close()


def test_bulk_commit_reopens_file_connection(tmp_path):
    cache = SQLiteDict(tmp_path / 'bulk-reopen.sqlite')
    try:
        cache.close()
        with cache.bulk_commit():
            cache['key'] = 'fixture'
        assert cache['key'] == 'fixture'
    finally:
        cache.close()


@pytest.mark.parametrize('operation', ['close', 'clear', 'vacuum'])
def test_bulk_commit_rejects_transaction_breaking_operations(tmp_path, operation):
    cache = SQLiteDict(tmp_path / 'bulk-lifecycle.sqlite')
    try:
        with pytest.raises(sqlite3.ProgrammingError, match='transaction'):
            with cache.bulk_commit():
                cache['partial'] = 'fixture'
                getattr(cache, operation)()
        assert cache.get('partial') is None
        cache['complete'] = 'later fixture'
        assert cache['complete'] == 'later fixture'
    finally:
        cache.close()


def test_failed_nested_bulk_entry_does_not_rollback_owner(tmp_path):
    cache = SQLiteDict(tmp_path / 'nested-bulk.sqlite')
    try:
        with cache.bulk_commit():
            cache['first'] = 'fixture'
            with pytest.raises(sqlite3.OperationalError, match='transaction'):
                with cache.bulk_commit():
                    pytest.fail('Nested bulk transactions are unsupported')
            assert cache['first'] == 'fixture'
            cache['second'] = 'complete fixture'
        assert set(cache) == {'first', 'second'}
    finally:
        cache.close()


@pytest.mark.parametrize('failure', ['commit', 'rollback'])
def test_transaction_cleanup_failure_discards_unusable_connection(tmp_path, failure):
    class FailingConnection(sqlite3.Connection):
        fail_commit = False
        fail_rollback = False

        def commit(self):
            if self.fail_commit:
                raise sqlite3.OperationalError('Fixture commit failure')
            return super().commit()

        def rollback(self):
            if self.fail_rollback:
                raise sqlite3.OperationalError('Fixture rollback failure')
            return super().rollback()

    cache = SQLiteDict(tmp_path / 'cleanup.sqlite', factory=FailingConnection)
    try:
        error_type = sqlite3.OperationalError if failure == 'commit' else RuntimeError
        with pytest.raises(error_type) as caught:
            with cache.bulk_commit():
                cache['partial'] = 'fixture'
                if failure == 'commit':
                    cache._connection.fail_commit = True
                else:
                    cache._connection.fail_rollback = True
                    raise RuntimeError('Fixture body failure')
        assert cache._active_transaction is False
        if failure == 'rollback':
            assert cache._connection is None
            assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        else:
            cache._connection.fail_commit = False
        assert cache.get('partial') is None
        cache['complete'] = 'later fixture'
        assert cache['complete'] == 'later fixture'
    finally:
        cache.close()


def test_sorted_preserves_declared_type_key_conversion(tmp_path, monkeypatch):
    monkeypatch.setitem(sqlite3.converters, 'TEXT', lambda value: value.upper())
    cache = SQLiteDict(tmp_path / 'typed-keys.sqlite', detect_types=sqlite3.PARSE_DECLTYPES)
    try:
        cache['key'] = CachedResponse(status_code=200, content=b'fixture')
        (response,) = cache.sorted()
        assert response.content == b'fixture'
        assert response.cache_key == b'KEY'
    finally:
        cache.close()


def test_repeated_popitem_does_not_rescan_remaining_keys(tmp_path):
    rows_read = 0

    class CountingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.row_factory = self.count_row

        @staticmethod
        def count_row(cursor, row):
            nonlocal rows_read
            rows_read += 1
            return row

    cache = SQLiteDict(tmp_path / 'popitem.sqlite', factory=CountingConnection)
    try:
        values = {str(i): f'fixture {i}' for i in range(50)}
        cache.update(values)
        rows_read = 0
        assert dict(cache.popitem() for _ in values) == values
        assert rows_read <= 4 * len(values)
        with pytest.raises(KeyError):
            cache.popitem()
    finally:
        cache.close()


@pytest.mark.parametrize('encoding', ['UTF-8', 'UTF-16le', 'UTF-16be'])
@pytest.mark.parametrize('conversion', ['default', 'bytes', 'declared', 'transforming', 'rows'])
def test_sorted_key_identity_uses_database_encoding(tmp_path, monkeypatch, encoding, conversion):
    path = tmp_path / 'encoded.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA encoding='{encoding}'")
        connection.execute('CREATE TABLE bootstrap (value TEXT)')
    connection.close()

    class ByteTextConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.text_factory = bytes

    kwargs = {}
    if conversion == 'bytes':
        kwargs['factory'] = ByteTextConnection
    elif conversion == 'declared':
        monkeypatch.setitem(sqlite3.converters, 'TEXT', lambda value: b'converted:' + value)
        kwargs['detect_types'] = sqlite3.PARSE_DECLTYPES
    cache = SQLiteDict(path, **kwargs)
    try:
        with cache.connection() as connection:
            if conversion == 'transforming':
                connection.text_factory = lambda raw: raw.decode('utf-8') + ' suffix'
            elif conversion == 'rows':
                connection.row_factory = lambda cursor, row: tuple(
                    value + ' suffix' if isinstance(value, str) else value for value in row
                )
            text_factory = connection.text_factory
            row_factory = connection.row_factory
        for key, content in [
            ('aa', b'first'),
            ('\u6161', b'second'),
            ('\u00e9', b'third'),
            (b'aa', b'blob'),
        ]:
            cache[key] = CachedResponse(status_code=200, content=content)
        with cache.connection() as connection:
            rows = connection.execute('SELECT key,value FROM http_cache ORDER BY key').fetchall()
        expected = [(key, cache.serializer.loads(value).content) for key, value in rows]
        assert [
            (response.cache_key, response.content) for response in cache.sorted(key='key')
        ] == expected
        with cache.connection() as connection:
            assert connection.text_factory is text_factory
            assert connection.row_factory is row_factory
    finally:
        cache.close()


@pytest.mark.parametrize('encoding', ['UTF-16le', 'UTF-16be'])
def test_reset_expiration_preserves_utf16_response_bodies(tmp_path, encoding):
    path = tmp_path / 'expiration.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA encoding='{encoding}'")
        connection.execute('CREATE TABLE bootstrap (value TEXT)')
    connection.close()
    cache = SQLiteCache(path)
    expected = {'aa': b'first', '\u6161': b'second'}
    try:
        for key, content in expected.items():
            cache.responses[key] = CachedResponse(status_code=200, content=content)
        assert {response.cache_key: response.content for response in cache.filter()} == expected
        cache.reset_expiration(60)
        assert {key: cache.responses[key].content for key in expected} == expected
    finally:
        cache.close()


def test_encoding_failure_restores_text_factory(tmp_path):
    class FailingCursor(sqlite3.Cursor):
        def execute(self, sql, *args):
            if sql == 'PRAGMA encoding':
                raise RuntimeError('Fixture metadata failure')
            return super().execute(sql, *args)

    class FailingConnection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            return super().cursor(factory=FailingCursor)

    cache = SQLiteDict(tmp_path / 'metadata-failure.sqlite', factory=FailingConnection)
    try:
        cache['key'] = 'fixture'
        with cache.connection() as connection:

            def factory(raw):
                return raw.decode('utf-8') + ' suffix'

            connection.text_factory = factory
        with pytest.raises(RuntimeError, match='Fixture metadata failure'):
            list(cache.sorted())
        with cache.connection() as connection:
            assert connection.text_factory is factory
        assert cache['key'] == 'fixture'
    finally:
        cache.close()
