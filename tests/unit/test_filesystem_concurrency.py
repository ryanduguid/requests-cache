"""Keep filesystem metadata compatible with SQLite connection ownership."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import ProgrammingError

import pytest

from requests_cache.backends import filesystem
from requests_cache.backends.filesystem import FileCache, FileDict, LRUDict, LRUFileDict


@pytest.mark.parametrize('wal', [False, True])
def test_clear_preserves_lru_database_file(tmp_path, wal):
    cache = LRUFileDict(tmp_path / 'files', wal=wal)
    try:
        cache['first'] = 'fixture'
        database = Path(cache.lru_index.db_path)
        inode = database.stat().st_ino
        assert cache.size() > 0
        cache.clear()
        assert database.stat().st_ino == inode
        assert len(cache) == cache.size() == len(cache.lru_index) == 0
        cache['second'] = 'later fixture'
        assert cache['second'] == 'later fixture'
        assert cache.size() > 0
    finally:
        cache.lru_index.close()


@pytest.mark.parametrize('wal', [False, True])
@pytest.mark.parametrize('lru', [False, True])
def test_clear_keeps_shared_metadata_usable(tmp_path, wal, lru):
    kwargs = {'max_cache_bytes': 1000} if lru else {}
    caches = [FileCache(tmp_path / 'shared', wal=wal, **kwargs) for _ in range(2)]
    first, second = caches
    try:
        first.responses['first'] = 'fixture'
        first.redirects['alias'] = 'first'
        names = ['redirects.sqlite', 'lru.db'] if lru else ['redirects.sqlite']
        suffixes = ['', '-wal', '-shm'] if wal else ['']
        paths = [first.cache_dir / f'{name}{suffix}' for name in names for suffix in suffixes]
        inodes = {path: path.stat().st_ino for path in paths}
        extra_dir = first.cache_dir / 'extra'
        extra_dir.mkdir()
        (extra_dir / 'payload').write_text('fixture')
        (first.cache_dir / 'unrelated-extension').write_text('fixture')

        first.clear()
        assert {path: path.stat().st_ino for path in paths} == inodes
        assert not extra_dir.exists()
        assert not (first.cache_dir / 'unrelated-extension').exists()
        for cache in caches:
            assert len(cache.responses) == len(cache.redirects) == 0
            if lru:
                assert cache.responses.size() == len(cache.responses.lru_index) == 0
        second.responses['second'] = 'later fixture'
        second.redirects['new alias'] = 'second'
        assert first.responses['second'] == 'later fixture'
        assert first.redirects['new alias'] == 'second'
        second.clear()
        assert len(first.responses) == len(first.redirects) == 0
    finally:
        for cache in caches:
            cache.redirects.close()
            if lru:
                cache.responses.lru_index.close()


@pytest.mark.parametrize('key, extension', [('lru', 'db'), ('redirects', 'sqlite')])
def test_plain_file_dict_clears_metadata_like_payload_names(tmp_path, key, extension):
    cache = FileDict(tmp_path / 'plain', extension=extension)
    cache[key] = 'fixture'
    cache.clear()
    assert list(cache.cache_dir.iterdir()) == []


@pytest.mark.parametrize('metadata', ['redirects', 'lru_index'])
def test_clear_rejects_active_metadata_batch_before_removing_payload(tmp_path, metadata):
    cache = FileCache(tmp_path / 'active', max_cache_bytes=1000)
    cache.responses['first'] = 'fixture'
    cache.redirects['alias'] = 'first'
    storage = cache.redirects if metadata == 'redirects' else cache.responses.lru_index
    try:
        with storage.bulk_commit():
            with pytest.raises(ProgrammingError, match='transaction'):
                cache.clear()
            assert cache.responses['first'] == 'fixture'
            assert cache.redirects['alias'] == 'first'
    finally:
        cache.redirects.close()
        cache.responses.lru_index.close()


@pytest.mark.parametrize('legacy_rows', [False, True])
def test_lru_reopening_keeps_one_correct_size_counter(tmp_path, legacy_rows):
    cache = LRUDict(tmp_path / 'reopen.sqlite')
    try:
        cache.update({'first': 10, 'second': 23})
        if legacy_rows:
            with cache.connection(commit=True) as connection:
                connection.execute('UPDATE http_cache_size SET rowid=10')
                connection.executemany('INSERT INTO http_cache_size VALUES (?)', [(0,), (-10,)])
        for _ in range(3):
            reopened = LRUDict(tmp_path / 'reopen.sqlite')
            try:
                with reopened.connection() as connection:
                    assert connection.execute('SELECT * FROM http_cache_size').fetchall() == [(33,)]
                reopened['second'] = 40
                assert cache.total_size() == 50
                reopened['second'] = 23
            finally:
                reopened.close()
    finally:
        cache.close()


@pytest.mark.parametrize('extension', ['db', 'sqlite'])
def test_metadata_extensions_are_excluded_from_responses_and_lru_sync(tmp_path, extension):
    kwargs = {'max_cache_bytes': 10000, 'extension': extension, 'sync_index': True}
    first = FileCache(tmp_path / 'extensions', **kwargs)
    second = None
    try:
        first.responses['entry'] = 'fixture'
        second = FileCache(tmp_path / 'extensions', **kwargs)
        for cache in [first, second]:
            assert list(cache.responses.keys()) == ['entry']
            assert list(cache.responses.lru_index) == ['entry']
        first.clear()
        for cache in [first, second]:
            assert len(cache.responses) == cache.responses.size() == 0
            assert list(cache.responses.keys()) == []
    finally:
        for cache in [first, second]:
            if cache is not None:
                cache.redirects.close()
                cache.responses.lru_index.close()


def test_metadata_clear_keeps_only_registered_files_and_continues_after_io_error(
    tmp_path, monkeypatch
):
    cache = FileDict(tmp_path / 'files')
    cache._register_metadata(cache.cache_dir / 'index.sqlite')
    retained = ['index.sqlite', 'index.sqlite-wal', 'index.sqlite-shm', 'index.sqlite-journal']
    removed = ['index.sqlite.backup', '.hidden', 'payload.json']
    denied = cache.cache_dir / 'denied'
    for name in ['denied', *retained, *removed]:
        (cache.cache_dir / name).write_text('fabricated file')
    unlink = Path.unlink
    iterdir = Path.iterdir

    def fail_one_unlink(path, *args, **kwargs):
        if path == denied:
            raise PermissionError('Fixture locked payload')
        return unlink(path, *args, **kwargs)

    def denied_first(path):
        return iter(sorted(iterdir(path), key=lambda child: child != denied))

    monkeypatch.setattr(Path, 'unlink', fail_one_unlink)
    monkeypatch.setattr(Path, 'iterdir', denied_first)
    cache.clear()
    assert sorted(path.name for path in cache.cache_dir.iterdir()) == sorted(['denied', *retained])


def test_metadata_clear_recreates_missing_directory(tmp_path):
    cache = FileDict(tmp_path / 'missing')
    cache._register_metadata(cache.cache_dir / 'index.sqlite')
    cache.cache_dir.rmdir()
    cache.clear()
    assert cache.cache_dir.is_dir()


@pytest.mark.parametrize('root_link', [False, True])
def test_metadata_clear_does_not_follow_directory_links(tmp_path, root_link):
    cache = FileDict(tmp_path / 'links')
    cache._register_metadata(cache.cache_dir / 'index.sqlite')
    external = tmp_path / 'external'
    external.mkdir()
    sentinel = external / 'sentinel'
    sentinel.write_text('fixture')
    link = cache.cache_dir if root_link else cache.cache_dir / 'child'
    if root_link:
        cache.cache_dir.rmdir()
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'Directory symlinks unavailable: {error}')
    cache.clear()
    assert sentinel.read_text() == 'fixture'
    assert link.is_symlink() is root_link


@pytest.mark.skipif(sys.platform != 'win32', reason='Directory junctions require Windows')
def test_metadata_clear_does_not_traverse_root_junction(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_text('fixture')
    root = tmp_path / 'cache junction'
    subprocess.run(
        ['cmd.exe', '/d', '/c', 'mklink', '/J', str(root), str(outside)],
        check=True,
        capture_output=True,
    )
    cache = FileCache(root)
    try:
        assert cache.cache_dir == root
        assert not root.is_symlink()
        cache.clear()
        assert sentinel.read_text() == 'fixture'
    finally:
        cache.redirects.close()


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
