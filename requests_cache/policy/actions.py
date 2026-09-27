import json
from collections import Counter
from datetime import datetime, timedelta
from logging import DEBUG, getLogger
from typing import TYPE_CHECKING, Dict, List, Mapping, MutableMapping, Optional, Union

from attrs import define, field
from requests import PreparedRequest, Response
from requests.structures import CaseInsensitiveDict

from .._utils import coalesce, decode, encode, is_json_content_type
from ..cache_keys import _normalize_json_body, normalize_headers, normalize_request
from ..models import RichMixin
from . import (
    DO_NOT_CACHE,
    EXPIRE_IMMEDIATELY,
    NEVER_EXPIRE,
    CacheDirectives,
    ExpirationTime,
    KeyCallback,
    get_expiration_datetime,
    get_expiration_seconds,
    get_url_expiration,
    utcnow,
)
from .settings import CacheSettings

if TYPE_CHECKING:
    from ..models import AnyPreparedRequest, CachedRequest, CachedResponse

# Nonstandard headers that can be used to override the request method
METHOD_OVERRIDE_HEADERS = [
    'X-HTTP-Method-Override',
    'X-HTTP-Method',
    'X-Method-Override',
]

logger = getLogger(__name__)


@define(repr=False)
class CacheActions(RichMixin):
    """Translates cache settings and headers into specific actions to take for a given cache item.
     The resulting actions are then handled in :py:meth:`CachedSession.send`.

    .. rubric:: Notes

    * See :ref:`precedence` for behavior if multiple sources provide an expiration
    * See :ref:`headers` for more details about header behavior
    * The following arguments/properties are the outputs of this class:

    Args:
        cache_key: The cache key created based on the initial request
        error_504: Indicates the request cannot be fulfilled based on cache settings
        expire_after: User or header-provided expiration value
        send_request: Send a new request
        resend_request: Send a new request to refresh a stale cache item
        resend_async: Return a stale cache item, and send a non-blocking request to refresh it
        skip_read: Skip reading from the cache
        skip_write: Skip writing to the cache
        vary_mismatch: The cached response belongs to a different request variant
    """

    # Outputs
    cache_key: str = field(default=None, repr=False)
    error_504: bool = field(default=False)
    expire_after: ExpirationTime = field(default=None)
    send_request: bool = field(default=False)
    resend_request: bool = field(default=False)
    resend_async: bool = field(default=False)
    skip_read: bool = field(default=False)
    skip_write: bool = field(default=False)
    vary_cache_key: Optional[str] = field(default=None)
    vary_mismatch: bool = field(default=False)

    # Inputs
    _directives: CacheDirectives = field(default=None, repr=False)
    _settings: CacheSettings = field(default=None, repr=False)

    # Temporary attributes
    _only_if_cached: bool = field(default=False, repr=False)
    _refresh: bool = field(default=False, repr=False)
    _request: PreparedRequest = field(default=None, repr=False)
    _stale_if_error: Union[bool, ExpirationTime] = field(default=None, repr=False)
    _stale_while_revalidate: Union[bool, ExpirationTime] = field(default=None, repr=False)
    _validation_headers: Dict[str, str] = field(factory=dict, repr=False)

    @classmethod
    def from_request(
        cls,
        cache_key: str,
        request: PreparedRequest,
        settings: Optional[CacheSettings] = None,
    ):
        """Initialize from request info and cache settings.

        Note on refreshing: `must-revalidate` isn't a standard request header, but is used here to
        indicate a user-requested refresh. Typically that's only used in response headers, and
        `max-age=0` would be used by a client to request a refresh. However, this would conflict
        with the `expire_after` option provided in :py:meth:`.CachedSession.request`.

        Args:
            cache_key: The cache key created based on the initial request
            request: The outgoing request
            settings: Session-level cache settings
        """
        settings = settings or CacheSettings()
        directives = CacheDirectives.from_headers(request.headers)
        logger.debug(f'Cache directives from request headers: {directives}')

        # Check expiration values in order of precedence
        expire_after = coalesce(
            directives.max_age,
            get_url_expiration(request.url, settings.urls_expire_after),
            settings.expire_after,
        )

        # Merge values that may come from either settings (requests-cache-specific) or headers (standard)
        actual_no_cache = directives.actual_no_cache or expire_after == DO_NOT_CACHE
        kinda_no_cache = directives.no_cache or directives.no_store
        only_if_cached = settings.only_if_cached or directives.only_if_cached
        refresh = directives.max_age == EXPIRE_IMMEDIATELY or directives.must_revalidate
        stale_if_error = settings.stale_if_error or directives.stale_if_error
        stale_while_revalidate = (
            settings.stale_while_revalidate or directives.stale_while_revalidate
        )

        # Check and log conditions for reading from the cache
        read_criteria = {
            'disabled cache': settings.disabled,
            'disabled method': not _is_method_allowed(request, settings),
            'disabled by headers or refresh': kinda_no_cache,
            'disabled by expiration': actual_no_cache,
        }
        _log_cache_criteria('read', read_criteria)

        actions = cls(
            cache_key=cache_key,
            directives=directives,
            expire_after=expire_after,
            only_if_cached=only_if_cached,
            refresh=refresh,
            request=request,
            settings=settings,
            skip_read=any(read_criteria.values()),
            skip_write=directives.no_store,
            stale_if_error=stale_if_error,
            stale_while_revalidate=stale_while_revalidate,
        )
        return actions

    @property
    def expires(self) -> Optional[datetime]:
        """Convert the user/header-provided expiration value to a datetime. Applies to new cached
        responses, and previously cached responses that are being revalidated.
        """
        return get_expiration_datetime(self.expire_after)

    def is_usable(self, cached_response: Optional['CachedResponse'], error: bool = False):
        """Determine whether a given cached response is "fresh enough" to satisfy the request,
        based on:

        * min-fresh
        * max-stale
        * stale-if-error (if an error has occurred)
        * stale-while-revalidate
        """
        if cached_response is None:
            return False
        elif (
            cached_response.expires is None
            or (cached_response.is_expired and self._stale_while_revalidate is True)
            or (error and self._stale_if_error is True)
        ):
            return True
        # Handle stale_if_error as a time value
        elif error and self._stale_if_error:
            offset = timedelta(seconds=get_expiration_seconds(self._stale_if_error))
        # Handle stale_while_revalidate as a time value
        elif cached_response.is_expired and self._stale_while_revalidate:
            offset = timedelta(seconds=get_expiration_seconds(self._stale_while_revalidate))
        # Handle min-fresh and max-stale
        else:
            offset = self._directives.get_expire_offset()

        return utcnow() < cached_response.expires + offset

    def update_from_cached_response(
        self,
        cached_response: Optional['CachedResponse'],
        create_key: Optional[KeyCallback] = None,
        **key_kwargs,
    ):
        """Determine if we can reuse a cached response, or set headers for a conditional request
        if possible.

        Used after fetching a cached response, but before potentially sending a new request.

        Args:
            cached_response: Cached response to examine
            create_key: Cache key function, used for validating ``Vary`` headers
            key_kwargs: Additional keyword arguments for ``create_key``.
        """
        # A Vary mismatch is a cache miss, regardless of freshness.
        self.vary_mismatch = bool(
            cached_response is not None
            and create_key
            and not self._validate_vary(cached_response, create_key, **key_kwargs)
        )
        if self.vary_mismatch:
            cached_response = None

        usable_response = self.is_usable(cached_response)
        usable_if_error = self.is_usable(cached_response, error=True)

        # Can't satisfy the request
        if not usable_response and self._only_if_cached and not usable_if_error:
            self.error_504 = True
        # Send the request for the first time
        elif cached_response is None:
            self.send_request = True
        # Resend the request, unless settings permit a stale response
        elif not usable_response and not (self._only_if_cached and usable_if_error):
            self.resend_request = True
        # Resend the request in the background; meanwhile return stale response
        elif cached_response.is_expired and usable_response and self._stale_while_revalidate:
            self.resend_async = True

        if cached_response is not None and not self._only_if_cached:
            self._update_validation_headers(cached_response)
        logger.debug(f'Post-read cache actions: {self}')

    def update_from_response(self, response: Response):
        """Update expiration + actions based on headers and other details from a new response.

        Used after receiving a new response, but before saving it to the cache.
        """
        directives = CacheDirectives.from_headers(response.headers)
        if self._settings.cache_control:
            self._update_from_response_headers(directives)

        # If "expired" but there's a validator, save it to the cache and revalidate on use
        skip_stale = self.expire_after == EXPIRE_IMMEDIATELY and not directives.has_validator
        do_not_cache = self.expire_after == DO_NOT_CACHE

        # Apply filter callback, if any
        callback = self._settings.filter_fn
        filtered_out = callback is not None and not callback(response)

        # Check and log conditions for writing to the cache
        write_criteria = {
            'disabled cache': self._settings.disabled,
            'disabled method': not _is_method_allowed(response.request, self._settings),
            'disabled status': response.status_code not in self._settings.allowable_codes,
            'disabled by filter': filtered_out,
            'disabled by headers': self.skip_write,
            'disabled by expiration': do_not_cache or skip_stale,
            'disabled by read-only': self._settings.read_only,
        }
        self.skip_write = any(write_criteria.values())
        _log_cache_criteria('write', write_criteria)

    def update_request(self, request: PreparedRequest) -> PreparedRequest:
        """Apply validation headers (if any) before sending a request"""
        request.headers.update(self._validation_headers)
        return request

    def update_revalidated_response(
        self, response: Response, cached_response: 'CachedResponse'
    ) -> 'CachedResponse':
        """After revalidation, update the cached response's expiration and headers"""
        logger.debug(f'Response for URL {response.request.url} has not been modified')

        # Skip updating the cached response if expiration and headers are unchanged
        # OR if session/request settings skip it.
        # Ignore validators missing from new response, since they may be omitted
        headers_changed = any(
            cached_response.headers.get(k) != v for k, v in response.headers.items()
        )
        expiration_changed = self.expires != cached_response.expires
        permanent_skip = (
            self._settings.read_only or self._settings.disabled or self._directives.no_store
        )
        self.skip_write = permanent_skip or not (expiration_changed or headers_changed)

        cached_response.expires = self.expires

        # Update headers;
        # Account for Transfer-Encoding in original response and Content-Length in 304 response
        cached_response.headers.update(response.headers)
        if (
            'Content-Length' in cached_response.headers
            and 'Transfer-Encoding' in cached_response.headers
        ):
            del cached_response.headers['Content-Length']

        cached_response.revalidated = True
        return cached_response

    def _update_from_response_headers(self, directives: CacheDirectives):
        """Check response headers for expiration and other cache directives"""
        logger.debug(f'Cache directives from response headers: {directives}')

        self._stale_if_error = self._stale_if_error or directives.stale_if_error
        if directives.immutable:
            self.expire_after = NEVER_EXPIRE
        else:
            self.expire_after = coalesce(
                directives.max_age,
                directives.expires,
                self.expire_after,
            )
        self.skip_write = self.skip_write or directives.no_store

    def _update_validation_headers(self, cached_response: 'CachedResponse'):
        """If needed, get validation headers based on a cached response. Revalidation may be
        triggered by a stale response, request headers, or cached response headers.
        """
        directives = CacheDirectives.from_headers(cached_response.headers)
        # These conditions always apply
        revalidate = directives.has_validator and (
            cached_response.is_expired or self._refresh or self._settings.always_revalidate
        )
        # These conditions only apply if cache_control=True
        cc_revalidate = self._settings.cache_control and (
            directives.no_cache or directives.must_revalidate
        )

        # Add the appropriate validation headers, if needed
        if revalidate or cc_revalidate:
            if directives.etag:
                self._validation_headers['If-None-Match'] = directives.etag
            if directives.last_modified:
                self._validation_headers['If-Modified-Since'] = directives.last_modified
            self.send_request = False
            self.resend_request = True

    def _validate_vary(
        self, cached_response: 'CachedResponse', create_key: KeyCallback, **key_kwargs
    ) -> bool:
        """If the cached response contains Vary, check that the specified request headers match"""
        vary = cached_response.headers.get('Vary')
        redacted = getattr(cached_response, 'redacted_fields', None)
        if ((redacted is None or 'unknown' in redacted) and vary == 'REDACTED') or (
            redacted is not None and 'header:vary' in redacted
        ):
            return False
        if not vary:
            return True

        # Generate a secondary cache key for the headers nominated by Vary.
        # Cookie checks use the final request, whose header includes redirect updates.
        match_headers = [k.strip().lower() for k in vary.split(',')]
        if '*' in match_headers:
            return False
        vary_request = (
            cached_response.history[-1].request
            if cached_response.history and 'cookie' not in match_headers
            else cached_response.request
        )

        if not self._vary_headers_available(vary_request, match_headers):
            return False

        # The prepared Cookie header records what was sent; the jar may contain unsent cookies.
        if 'cookie' in match_headers:
            if cached_response.history and not self._matches_final_request(vary_request):
                return False
            if not self._cookies_match(vary_request):
                logger.debug('Failed Vary check: cookies do not match')
                return False
            match_headers = [h for h in match_headers if h != 'cookie']

        if not match_headers:
            return True

        key_kwargs['match_headers'] = match_headers
        vary_key_cached = create_key(vary_request, **key_kwargs)
        vary_key_current = create_key(self._request, **key_kwargs)
        headers_match = vary_key_current == vary_key_cached
        if not headers_match:
            _log_vary_diff(
                self._request.headers,
                vary_request.headers,
                key_kwargs['match_headers'],
            )
            # Compute secondary cache key with merged Vary + user match_headers
            merged = self._merge_match_headers(match_headers)
            key_kwargs['match_headers'] = merged
            self.vary_cache_key = create_key(self._request, **key_kwargs)
        return headers_match

    def _vary_headers_available(
        self, cached_request: 'CachedRequest', match_headers: List[str]
    ) -> bool:
        """Reject comparisons whose nominated header values were or will be redacted."""
        cached_headers = normalize_headers(cached_request.headers or {})
        current_headers = normalize_headers(self._request.headers or {})
        ignored = {h.lower() for h in (self._settings.ignored_parameters or [])}
        ignore_overlap = ignored & set(match_headers)
        if any(h in cached_headers or h in current_headers for h in ignore_overlap):
            logger.debug(
                f'Failed Vary check: Vary header(s) are in ignored_parameters: {ignore_overlap}'
            )
            return False
        for request, headers in (
            (cached_request, cached_headers),
            (self._request, current_headers),
        ):
            redacted = getattr(request, 'redacted_fields', [])
            if any(f'header:{h}' in (redacted or []) for h in match_headers):
                return False
            if (redacted is None or 'unknown' in redacted) and any(
                headers.get(h) == 'REDACTED' for h in match_headers
            ):
                return False
        return True

    def _matches_final_request(self, cached_request: 'CachedRequest') -> bool:
        """Only reuse a redirected response when the available final identity matches."""
        for request in (self._request, cached_request):
            if not request.method or not request.url:
                return False
            body: object = request.body
            if body is not None and not isinstance(body, (str, bytes)):
                return False  # Normalising a shared stream would read and rewind the live body.
        if bool(self._request.body) != bool(cached_request.body):
            return False
        try:
            # Form normalisation may have erased a nonempty body such as 'x='.
            if not cached_request.body and int(cached_request.headers.get('Content-Length', '0')):
                return False
            cached = normalize_request(
                cached_request,
                ignored_parameters=None,
                content_root_key=self._settings.content_root_key,
            )
            current = normalize_request(
                self._request,
                ignored_parameters=None,
                content_root_key=self._settings.content_root_key,
            )
        except (TypeError, ValueError, RecursionError):
            return False
        if not all(
            _has_complete_identity(original, normalised)
            for original, normalised in ((cached_request, cached), (self._request, current))
        ):
            return False

        return (current.method, current.url) == (cached.method, cached.url) and (
            encode(current.body) == encode(cached.body)
            or _identity_body(current, self._settings.content_root_key)
            == _identity_body(cached, self._settings.content_root_key)
        )

    def _cookies_match(self, cached_request: 'CachedRequest') -> bool:
        """Compare sent Cookie headers, retaining jar checks only for two headerless requests."""
        cached_headers = normalize_headers(cached_request.headers or {})
        current_headers = normalize_headers(self._request.headers or {})
        cached_field = (
            'Cookie' in cached_headers,
            cached_headers.get('Cookie'),
        )
        current_field = (
            'Cookie' in current_headers,
            current_headers.get('Cookie'),
        )
        if cached_field[0] or current_field[0]:
            return cached_field == current_field
        if any(
            'cookies' in (getattr(request, 'redacted_fields', None) or [])
            for request in (cached_request, self._request)
        ):
            return False
        if 'cookie' in {h.lower() for h in (self._settings.ignored_parameters or [])}:
            return True

        cached_cookies: Mapping[str, Optional[str]] = cached_request.cookies or {}
        request_cookies = getattr(self._request, '_cookies', None) or {}
        if not isinstance(cached_cookies, Mapping) or not isinstance(request_cookies, Mapping):
            return False
        return Counter(request_cookies.items()) == Counter(cached_cookies.items())

    def _merge_match_headers(self, vary_headers: List[str]) -> Union[List[str], bool]:
        """Merge Vary headers with user-configured match_headers to build a complete
        set of headers for the secondary cache key."""
        user_headers = self._settings.match_headers
        if user_headers is True:
            return True
        if not user_headers:
            return vary_headers
        return sorted(set(vary_headers + [h.lower() for h in user_headers]))


def _is_method_allowed(request: PreparedRequest, settings: CacheSettings) -> bool:
    """Check request method as well as method override headers"""
    headers = request.headers or CaseInsensitiveDict()
    methods = [headers.get(k) for k in METHOD_OVERRIDE_HEADERS]
    methods += [request.method]
    return any(m is not None and m in settings.allowable_methods for m in methods)


def _log_vary_diff(
    headers_1: MutableMapping[str, str],
    headers_2: MutableMapping[str, str],
    vary: List[str],
):
    """Log which specific headers specified by Vary did not match"""
    if logger.level > DEBUG:
        return
    headers_1 = normalize_headers(headers_1)
    headers_2 = normalize_headers(headers_2)
    nonmatching = [k for k in vary if headers_1.get(k) != headers_2.get(k)]
    logger.debug(f'Failed Vary check. Non-matching headers: {", ".join(nonmatching)}')


def _identity_body(request: 'AnyPreparedRequest', content_root_key: Optional[str]) -> bytes:
    """Compare JSON consistently when one large body was normalised before storage."""
    body = encode(request.body)
    content_type = decode(request.headers.get('Content-Type', ''), encoding='latin-1')
    if is_json_content_type(content_type.split(';', 1)[0].lower()):
        try:
            body = encode(_normalize_json_body(body, None, content_root_key, force=True)[0])
        except RecursionError:
            pass  # Keep byte comparison when a large body exceeds the JSON parser's depth limit.
    return body


def _has_complete_identity(
    original: 'AnyPreparedRequest', normalised: 'AnyPreparedRequest'
) -> bool:
    """Use recorded data loss where available, and conservative checks for legacy entries."""
    if isinstance(original, PreparedRequest) and not hasattr(original, 'redacted_fields'):
        return True
    redacted = getattr(original, 'redacted_fields', None)
    if redacted is not None:
        if {'url', 'body'} & set(redacted) or (original.body and 'header:content-type' in redacted):
            return False
        if 'unknown' not in redacted:
            return True
    body = encode(normalised.body)
    if (
        'REDACTED' in (normalised.url or '')
        or b'REDACTED' in encode(original.body)
        or b'REDACTED' in body
    ):
        return False
    content_type = decode(normalised.headers.get('Content-Type', ''), encoding='latin-1')
    # Legacy arrays may have lost values under a different content_root_key setting.
    return not (
        (body and content_type == 'REDACTED')
        or (
            is_json_content_type(content_type.split(';', 1)[0].lower())
            and _has_ambiguous_json_array(body)
        )
    )


def _has_ambiguous_json_array(body: bytes) -> bool:
    try:
        data = json.loads(body, parse_int=str, parse_float=str)
    except RecursionError:
        return True
    except ValueError:
        return False
    # A previous content_root_key can name any immediate member, even if settings later change.
    return isinstance(data, list) or (
        isinstance(data, dict) and any(isinstance(value, list) for value in data.values())
    )


def _log_cache_criteria(operation: str, criteria: Dict):
    """Log details on any failed checks for cache read or write"""
    if logger.level > DEBUG:
        return
    if any(criteria.values()):
        status = ', '.join([k for k, v in criteria.items() if v])
    else:
        status = 'Passed'
    logger.debug(f'Pre-{operation} cache checks: {status}')
