"""Internal utilities for generating cache keys that are used for request matching

.. automodsumm:: requests_cache.cache_keys
   :functions-only:
   :nosignatures:
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from hashlib import blake2b, sha256
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    Union,
)
from urllib.parse import parse_qsl, quote_plus, unquote_to_bytes, urlencode, urlparse, urlunparse

from requests import PreparedRequest, Request, Session
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict
from url_normalize import url_normalize
from urllib3.util import SKIP_HEADER  # type: ignore[attr-defined]

from . import _json
from ._utils import decode, encode, patch_form_boundary, is_json_content_type

__all__ = [
    'create_key',
    'normalize_body',
    'normalize_headers',
    'normalize_request',
    'normalize_params',
    'normalize_url',
]
if TYPE_CHECKING:
    from .models import AnyPreparedRequest, AnyRequest, CachedResponse

# Maximum JSON request body size that will be filtered and normalized
MAX_NORM_BODY_SIZE = 10 * 1024 * 1024

KVList = List[Tuple[str, str]]
ParamList = Optional[Iterable[str]]
RequestContent = Union[Mapping, str, bytes]

logger = getLogger(__name__)


def create_key(
    request: AnyRequest,
    ignored_parameters: ParamList = None,
    match_headers: Union[ParamList, bool] = False,
    serializer: Any = None,
    content_root_key: Optional[str] = None,
    **request_kwargs,
) -> str:
    """Create a normalized cache key based on a request object

    Args:
        request: Request object to generate a cache key from
        ignored_parameters: Request parameters, headers, and/or JSON body params to exclude
        match_headers: Match only the specified headers, or ``True`` to match all headers
        serializer: Serializer name or instance
        content_root_key: root element in the request body to apply ignored_parameters to
        request_kwargs: Additional keyword arguments for :py:func:`~requests.request`
    """
    # Normalize and gather all relevant request info to match against
    request = normalize_request(request, ignored_parameters, content_root_key)
    key_parts = [
        b'requests-cache-key-v2',  # Old normalisation could erase values in stored request metadata.
        request.method or '',
        request.url,
        request.body or '',
        bool(request_kwargs.get('verify', True)),
        *get_matched_headers(request.headers, match_headers),
        str(serializer),
    ]

    # Generate a hash based on this info
    try:
        key = blake2b(digest_size=8)
    except (TypeError, ValueError):
        # OpenSSL 1.1.0 doesn't support the digest_size parameter for blake2b, resulting in a TypeError
        # On FIPS-compliant systems, blake2b is not compliant algorithm, resulting in a ValueError
        # In both cases, fallback to SHA-256
        key = sha256()  # type: ignore
    for part in key_parts:
        encoded = encode(part)
        key.update(len(encoded).to_bytes(8, 'big'))
        key.update(encoded)
    return key.hexdigest()


def get_matched_headers(
    headers: CaseInsensitiveDict, match_headers: Union[ParamList, bool]
) -> List[str]:
    """Get only the headers we should match against as a list of ``k=v`` strings, given an optional
    include list.
    """
    if not match_headers:
        return []
    if match_headers is True:
        match_headers = headers
    # Keep literal byte control values distinct from strings containing their representation.
    return [
        f'{k.lower()}={headers[k]!r}'
        for k in sorted(match_headers, key=lambda x: x.lower())
        if k in headers
    ]


def normalize_request(
    request: AnyRequest,
    ignored_parameters: ParamList = None,
    content_root_key: Optional[str] = None,
) -> AnyPreparedRequest:
    """Normalize and remove ignored parameters from request URL, body, and headers.
    This is used for both:

    * Increasing cache hits by generating more precise cache keys
    * Redacting potentially sensitive info from cached requests

    Args:
        request: Request object to normalize
        ignored_parameters: Request parameters, headers, and/or JSON body params to exclude
        content_root_key: root element in the request body to apply ignored_parameters to
    """
    if isinstance(request, Request):
        # For a multipart POST request that hasn't been prepared, we need to patch the form boundary
        # so the request body will have a consistent hash
        with patch_form_boundary() if request.files else nullcontext():
            norm_request: AnyPreparedRequest = Session().prepare_request(request)
    else:
        norm_request = request.copy()

    norm_request.method = (norm_request.method or '').upper()
    norm_request.url = normalize_url(norm_request.url or '', ignored_parameters)
    norm_request.body = normalize_body(norm_request, ignored_parameters, content_root_key)
    norm_request.headers = normalize_headers(norm_request.headers, ignored_parameters)
    _redact_cookie_jar(norm_request, ignored_parameters)
    return norm_request


def normalize_headers(
    headers: MutableMapping[str, str],
    ignored_parameters: ParamList = None,
) -> CaseInsensitiveDict:
    """Redact ignored values and decode byte headers so they round-trip through Requests unchanged."""
    ignored_headers = {name.lower() for name in ignored_parameters or []}
    return CaseInsensitiveDict(
        (
            name,
            'REDACTED'
            if name.lower() in ignored_headers
            else (value if value == SKIP_HEADER.encode() else decode(value, encoding='latin-1')),
        )
        for name, value in headers.items()
    )


def normalize_url(url: str, ignored_parameters: ParamList) -> str:
    """Normalize and filter a URL. This includes request parameters, IDN domains, scheme, host,
    port, etc.
    """
    url_tokens = urlparse(url)
    query = normalize_params(url_tokens.query, ignored_parameters)
    # The URL normaliser collapses literal plus signs, spaces and empty query values.
    base_url = url_normalize(urlunparse(url_tokens._replace(query=''))) or ''
    return urlunparse(urlparse(base_url)._replace(query=query))


def normalize_body(
    request: AnyPreparedRequest,
    ignored_parameters: ParamList,
    content_root_key: Optional[str] = None,
) -> bytes:
    """Normalize and filter a request body if possible, depending on Content-Type"""
    if not request.body:
        return b''

    norm_body: Union[str, bytes] = request.body

    # Handle the case where the request body is a file-like object
    if hasattr(request.body, 'read'):
        norm_body = request.body.read() or b''
        try:
            request.body.seek(0)  # type: ignore[union-attr]
        except AttributeError as e:
            logger.warning(f'Unable to reset original request body: {e}', exc_info=True)

    try:
        content_type = (
            decode(request.headers['Content-Type'], encoding='latin-1').split(';')[0].lower()
        )
    except (AttributeError, KeyError):
        content_type = ''

    # Filter and sort params if possible
    if is_json_content_type(content_type):
        norm_body = normalize_json_body(norm_body, ignored_parameters, content_root_key)
    elif content_type == 'application/x-www-form-urlencoded':
        norm_body = normalize_params(norm_body, ignored_parameters)

    return encode(norm_body)


def normalize_json_body(
    original_body: Union[str, bytes],
    ignored_parameters: ParamList,
    content_root_key: Optional[str] = None,
) -> Union[str, bytes]:
    """Normalize and filter a request body with serialized JSON data"""
    if len(original_body) <= 2 or (
        len(original_body) > MAX_NORM_BODY_SIZE and not ignored_parameters
    ):
        return original_body

    try:
        body = _json.loads(decode(original_body))
    # If it's invalid JSON, then don't mess with it
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug('Invalid JSON body')
        return original_body

    if content_root_key and isinstance(body, dict) and content_root_key in body:
        body[content_root_key] = filter_sort_json(body[content_root_key], ignored_parameters)
    else:
        body = filter_sort_json(body, ignored_parameters)
    return _json.dumps(body)


def normalize_params(value: Union[str, bytes], ignored_parameters: ParamList = None) -> str:
    """Normalize and filter urlencoded params from either a URL or request body with form data"""
    components = decode(value).split('&')
    params = parse_qsl(
        '&'.join(component for component in components if '=' in component),
        keep_blank_values=True,
        errors='surrogateescape',
    )
    params = filter_sort_multidict(params, ignored_parameters)
    query_str = urlencode(params, errors='surrogateescape')

    # Preserve bare names separately from empty-valued fields, decoding each octet once.
    key_only_params = [
        quote_plus(unquote_to_bytes(component.replace('+', ' ')), safe='')
        for component in components
        if component and '=' not in component
    ]
    if key_only_params:
        key_only_param_str = '&'.join(sorted(key_only_params))
        query_str = f'{query_str}&{key_only_param_str}' if query_str else key_only_param_str

    return query_str


def redact_response(
    response: CachedResponse, ignored_parameters: ParamList, content_root_key: Optional[str] = None
) -> CachedResponse:
    """Redact any ignored parameters (potentially containing sensitive info) from a cached request"""
    if ignored_parameters:
        for cached_response in [response, *response.history]:
            cached_response.url = filter_url(cached_response.url, ignored_parameters)
            cached_response.headers = normalize_headers(cached_response.headers, ignored_parameters)
            for request in (cached_response.request, cached_response._next):
                if request is None:
                    continue
                request.url = filter_url(request.url, ignored_parameters)
                request.body = normalize_body(request, ignored_parameters, content_root_key)
                request.headers = normalize_headers(request.headers, ignored_parameters)
                _redact_cookie_jar(request, ignored_parameters)
    return response


def _redact_cookie_jar(request: AnyPreparedRequest, ignored_parameters: ParamList):
    if 'cookie' in {name.lower() for name in ignored_parameters or []}:
        # CachedRequest may share this jar with the live request, so replace it instead of clearing it.
        if isinstance(request, PreparedRequest):
            request._cookies = RequestsCookieJar()  # type: ignore[attr-defined]
        else:
            request.cookies = RequestsCookieJar()


def filter_sort_json(data, ignored_parameters: ParamList):
    if isinstance(data, Mapping):
        return filter_sort_dict(data, ignored_parameters)
    elif isinstance(data, list):
        return filter_sort_list(data, ignored_parameters)
    return data


def filter_sort_dict(
    data: Mapping[str, str],
    ignored_parameters: ParamList = None,
) -> Dict[str, str]:
    # Note: Any ignored_parameters present will have their values replaced instead of removing the
    # parameter. Different ignored values match, but an absent parameter remains distinct.
    ignored_parameters = set(ignored_parameters or [])
    return {k: ('REDACTED' if k in ignored_parameters else v) for k, v in sorted(data.items())}


def filter_sort_multidict(
    data: KVList,
    ignored_parameters: ParamList = None,
) -> KVList:
    ignored_parameters = set(ignored_parameters or [])
    return [(k, 'REDACTED' if k in ignored_parameters else v) for k, v in sorted(data)]


def filter_sort_list(data: List, ignored_parameters: ParamList = None) -> List:
    ignored = set(ignored_parameters or [])
    return [value for value in data if not isinstance(value, str) or value not in ignored]


def filter_url(url: str, ignored_parameters: ParamList) -> str:
    """Filter ignored parameters out of a URL"""
    # Strip query params from URL, sort and filter, and reassemble into a complete URL
    url_tokens = urlparse(url)
    return urlunparse(
        (
            url_tokens.scheme,
            url_tokens.netloc,
            url_tokens.path,
            url_tokens.params,
            normalize_params(url_tokens.query, ignored_parameters),
            url_tokens.fragment,
        )
    )
