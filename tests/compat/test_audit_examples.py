"""Exercise changed examples with fabricated responses and local cache files."""

import runpy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from requests_mock import ANY, Adapter

from requests_cache import CachedSession

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples'
URL = 'https://example.invalid/replay'


@pytest.mark.parametrize('cassette_format', ['vcrpy', 'betamax'])
@pytest.mark.parametrize('method, request_body', [('GET', None), ('POST', b'fabricated request')])
@pytest.mark.parametrize('body', [b'fabricated response', b'\x00\xff\x81'])
def test_exported_cassette_replays_without_network(
    tmp_path, cassette_format, method, request_body, body
):
    vcr = pytest.importorskip('vcr')
    betamax = pytest.importorskip('betamax')
    export = runpy.run_path(str(EXAMPLES / 'vcr.py'))
    adapter = Adapter()
    adapter.register_uri(method, URL, content=body, reason='OK')
    with CachedSession(backend='memory', allowable_methods=['GET', 'POST']) as session:
        session.mount('https://', adapter)
        original = session.request(method, URL, data=request_body)
        extension = 'json' if cassette_format == 'betamax' else 'yaml'
        path = tmp_path / f'export.{extension}'
        export['to_vcr_cassette'](session.cache, str(path), cassette_format)

    with requests.Session() as playback:
        playback.trust_env = False
        if cassette_format == 'betamax':
            recorder = betamax.Betamax(playback, cassette_library_dir=str(tmp_path))
            context = recorder.use_cassette('export', record='none')
        else:
            context = vcr.use_cassette(str(path), record_mode='none')
        with context:
            response = playback.request(method, URL, data=request_body)
    assert response.content == original.content == body
    assert response.status_code == 200


def test_old_response_conversion_preserves_content_and_metadata():
    convert = runpy.run_path(str(EXAMPLES / 'convert_cache.py'))['convert_old_response']
    adapter = Adapter()
    adapter.register_uri('GET', URL, content=b'fabricated old content', reason='OK')
    with requests.Session() as session:
        session.mount('https://', adapter)
        original = session.get(URL)
    created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    old = SimpleNamespace(
        **{
            field: getattr(original, field)
            for field in requests.Response.__attrs__
            if field != 'request'
        },
        request=original.request,
    )
    converted = convert(old, created_at)
    assert converted.content == original.content
    assert converted.status_code == original.status_code
    assert converted.request.url == original.request.url
    assert converted.created_at == created_at


def test_log_example_counts_only_uncached_requests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    example = runpy.run_path(str(EXAMPLES / 'log_requests.py'))
    adapter = Adapter()
    adapter.register_uri('GET', ANY, text='fabricated response')
    with requests.Session() as plain:
        plain.mount('https://', adapter)
        original = plain.get(URL)
    with patch.object(requests, 'get', return_value=original):
        with example['log_requests']() as session:
            first, second = session.get(URL), session.get(URL)
            assert not first.from_cache and second.from_cache
        session.close()


def test_backtesting_bypasses_future_cache_entry():
    time_machine = pytest.importorskip('time_machine')
    example = runpy.run_path(str(EXAMPLES / 'time_machine_backtesting.py'))
    adapter = Adapter()
    adapter.register_uri('GET', ANY, text='fabricated response')
    with example['BacktestCachedSession'](backend='memory') as session:
        session.mount('https://', adapter)
        session.get(URL)
        assert session.get(URL).from_cache
        with time_machine.travel(datetime(2020, 1, 1, tzinfo=timezone.utc)):
            response = session.get(URL)
            assert not response.from_cache
            assert response.created_at.year == 2020
        assert session.get(URL).from_cache
    assert adapter.call_count == 2
