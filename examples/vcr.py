#!/usr/bin/env python
"""
Example utilities to export responses to a format compatible with VCR-based libraries, including:
* [vcrpy](https://github.com/kevin1024/vcrpy)
* [betamax](https://github.com/betamaxpy/betamax)
"""
import json
from base64 import b64encode
from datetime import timezone
from importlib.metadata import version as pkg_version
from os import makedirs
from os.path import abspath, dirname, expanduser, join
from typing import Any, Dict, Iterable
from urllib.parse import urlparse

import yaml

from requests_cache import BaseCache, CachedResponse, CachedSession


def to_vcr_cassette(cache: BaseCache, path: str, cassette_format: str = 'vcrpy'):
    """Export responses to VCRpy YAML or Betamax JSON.

    Args:
        cache: Cache instance containing response data to export
        path: Path for new cassette file
        cassette_format: 'vcrpy' (default) or 'betamax'
    """

    responses = cache.responses.values()
    write_cassette(to_vcr_cassette_dict(responses, cassette_format), path)


def to_vcr_cassettes_by_host(
    cache: BaseCache, cassette_dir: str = '.', cassette_format: str = 'vcrpy'
):
    """Export VCRpy YAML or Betamax JSON cassettes, split into separate files
    based on request host

    Args:
        cache: Cache instance containing response data to export
        cassette_dir: Base directory for cassette library
        cassette_format: 'vcrpy' (default) or 'betamax'
    """
    responses = cache.responses.values()
    extension = 'json' if cassette_format == 'betamax' else 'yml'
    for host, cassette in to_vcr_cassette_dicts_by_host(responses, cassette_format).items():
        write_cassette(cassette, join(cassette_dir, f'{host}.{extension}'))


def to_vcr_cassette_dict(
    responses: Iterable[CachedResponse], cassette_format: str = 'vcrpy'
) -> Dict:
    """Convert responses to a VCR cassette dict"""
    if cassette_format not in ('vcrpy', 'betamax'):
        raise ValueError('cassette_format must be vcrpy or betamax')
    episodes = [to_vcr_episode(r, cassette_format) for r in responses]
    if cassette_format == 'betamax':
        return {
            'http_interactions': episodes,
            'recorded_with': f'requests-cache {pkg_version("requests_cache")}',
        }
    return {'interactions': episodes, 'version': 1}


def to_vcr_episode(response: CachedResponse, cassette_format: str = 'vcrpy') -> Dict:
    """Convert a single response to a VCR-compatible response ("episode") dict"""
    def _to_multidict(d):
        return {k: [v] for k, v in d.items()}

    request_body = response.request.body
    response_body = {'string': response.content, 'encoding': response.encoding}
    if cassette_format == 'betamax':
        body_bytes = request_body.encode('utf-8') if isinstance(request_body, str) else request_body
        request_body = {'base64_string': b64encode(body_bytes or b'').decode('ascii')}
        response_body = {
            'base64_string': b64encode(response.content).decode('ascii'),
            'encoding': response.encoding,
        }

    # Translate requests.Response structure into VCR format
    return {
        'request': {
            'body': request_body,
            'headers': _to_multidict(response.request.headers),
            'method': response.request.method,
            'uri': response.request.url,
        },
        'response': {
            'body': response_body,
            'headers': _to_multidict(response.headers),
            'status': {'code': response.status_code, 'message': response.reason},
            'url': response.url,
        },
        'recorded_at': response.created_at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
    }


def to_vcr_cassette_dicts_by_host(
    responses: Iterable[CachedResponse], cassette_format: str = 'vcrpy'
) -> Dict[str, Dict]:
    responses_by_host: Dict[str, Any] = {}
    for response in responses:
        host = urlparse(response.request.url).netloc
        responses_by_host.setdefault(host, [])
        responses_by_host[host].append(response)
    return {
        host: to_vcr_cassette_dict(responses, cassette_format)
        for host, responses in responses_by_host.items()
    }


def write_cassette(cassette, path):
    path = abspath(expanduser(path))
    makedirs(dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        if 'http_interactions' in cassette:
            json.dump(cassette, f)
        else:
            f.write(yaml.safe_dump(cassette))


# Create an example cache and export it to a cassette
if __name__ == '__main__':
    cache_dir = 'example_cache'
    session = CachedSession(join(cache_dir, 'http_cache.sqlite'))
    session.get('https://httpbin.org/get')
    session.get('https://httpbin.org/json')
    to_vcr_cassette(session.cache, join(cache_dir, 'http_cache.yaml'))
