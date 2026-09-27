"""Keep filesystem metadata compatible with SQLite connection ownership."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from requests_cache.backends import filesystem
from requests_cache.backends.filesystem import LRUDict, LRUFileDict


def test_clear_closes_lru_database_before_removing_directory(tmp_path, monkeypatch):
    cache = LRUFileDict(tmp_path / 'files')
    remove_directory = filesystem.rmtree

    def remove(path, *args, **kwargs):
        assert cache.lru_index._connection is None
        return remove_directory(path, *args, **kwargs)

    monkeypatch.setattr(filesystem, 'rmtree', remove)
    try:
        cache['first'] = 'fixture'
        assert cache.size() > 0
        cache.clear()
        assert len(cache) == cache.size() == len(cache.lru_index) == 0
        cache['second'] = 'later fixture'
        assert cache['second'] == 'later fixture'
        assert cache.size() > 0
    finally:
        cache.lru_index.close()


def test_paused_lru_iteration_releases_connection_lock(tmp_path):
    cache = LRUDict(tmp_path / 'index.sqlite')
    cache.update({'a': 10, 'b': 20})
    iterator = cache.sorted(key='key')

    def can_acquire():
        acquired = cache._lock.acquire(blocking=False)
        if acquired:
            cache._lock.release()
        return acquired

    try:
        assert next(iterator) == 'a'
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(can_acquire).result(timeout=2)
        cache.close()
        assert list(iterator) == ['b']
    finally:
        iterator.close()
        cache.close()


@pytest.mark.parametrize('key', ["quoted'key", "x' OR 1=1 --", 'plain'])
def test_lru_keys_are_bound_values(tmp_path, key):
    cache = LRUDict(tmp_path / 'keys.sqlite')
    try:
        cache['first'] = 1
        cache[key] = 2
        assert cache[key] == 2
        with pytest.raises(KeyError):
            cache["missing' OR 1=1 --"]
    finally:
        cache.close()


def test_lru_zero_limit_returns_no_keys(tmp_path):
    cache = LRUDict(tmp_path / 'limits.sqlite')
    try:
        cache['key'] = 10
        assert list(cache.sorted(limit=0)) == []
    finally:
        cache.close()


@pytest.mark.parametrize(
    'total_size, expected', [(1, ['key0', 'key1']), (201, ['key0', 'key1', 'key2'])]
)
def test_lru_equal_timestamps_select_enough_bytes(tmp_path, monkeypatch, total_size, expected):
    monkeypatch.setattr(filesystem, 'time_ns', lambda: 42)
    cache = LRUDict(tmp_path / 'tied-times.sqlite')
    try:
        cache.update({f'key{i}': i * 100 for i in range(4)})
        assert cache.get_lru(total_size) == expected
    finally:
        cache.close()
