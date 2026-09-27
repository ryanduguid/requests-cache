from logging import getLogger
from typing import List, Optional, Union, cast
from urllib.parse import urlsplit

from attrs import asdict, define, field, fields_dict
from requests import PreparedRequest
from requests.cookies import RequestsCookieJar
from requests.exceptions import InvalidHeader
from requests.structures import CaseInsensitiveDict
from requests.utils import check_header_validity

from ..cache_keys import _record_redaction, encode
from . import RichMixin

logger = getLogger(__name__)


@define(repr=False, getstate_setstate=False)
class CachedRequest(RichMixin):
    """A serialisable dataclass that emulates :py:class:`requests.PreparedRequest`"""

    body: bytes = field(default=None, converter=encode)
    cookies: RequestsCookieJar = field(factory=RequestsCookieJar)
    headers: CaseInsensitiveDict = field(factory=CaseInsensitiveDict)
    method: str = field(default=None)
    url: str = field(default=None)
    # None denotes legacy data whose redaction history is unknown.
    redacted_fields: Optional[List[str]] = field(default=None)

    @classmethod
    def from_request(
        cls, original_request: Union[PreparedRequest, 'CachedRequest']
    ) -> 'CachedRequest':
        """Create a CachedRequest based on an original request object"""
        kwargs = {k: getattr(original_request, k, None) for k in fields_dict(cls).keys()}
        kwargs['cookies'] = getattr(original_request, '_cookies', None)
        redacted = getattr(
            original_request, 'redacted_fields', None if isinstance(original_request, cls) else []
        )
        kwargs['redacted_fields'] = list(redacted) if redacted is not None else None
        obj = cls(**kwargs)  # type: ignore  # False positive in mypy 0.920+?
        body = getattr(original_request, 'body', None)
        if body is not None and not isinstance(body, (str, bytes)):
            _record_redaction(obj, 'body')
        return obj

    @property
    def path_url(self):
        p = urlsplit(self.url)
        url = p.path or '/'
        url += f'?{p.query}' if p.query else ''
        return url

    def copy(self) -> 'CachedRequest':
        """Return a copy of the CachedRequest"""
        return self.__class__(**asdict(self))

    def __getstate__(self):
        return {name: getattr(self, name) for name in fields_dict(type(self))}

    def __setstate__(self, state):
        self.redacted_fields = None
        if isinstance(state, tuple):
            state = dict(zip(fields_dict(type(self)), state, strict=False))
        for name in fields_dict(type(self)):
            if name in state:
                setattr(self, name, state[name])

    def prepare(self) -> PreparedRequest:
        """Convert the CachedRequest back into a PreparedRequest"""
        prepared_request = _PreparedRequest()
        prepared_request.prepare(
            cookies=self.cookies,
            data=self.body,
            headers={
                name: _restore_header(name, value) for name, value in (self.headers or {}).items()
            },
            method=self.method,
            url=self.url,
        )
        prepared_request.redacted_fields = (
            list(self.redacted_fields) if self.redacted_fields is not None else None
        )
        return prepared_request

    @property
    def _cookies(self):
        """For compatibility with PreparedRequest, which has an attribute named '_cookies', and a
        keyword argument named 'cookies'.
        """
        return self.cookies

    def __str__(self):
        return f'{self.method} {self.url}'


class _PreparedRequest(PreparedRequest):
    """Retain cache provenance through Requests' copying of prepared requests."""

    redacted_fields: Optional[List[str]] = None

    def copy(self) -> '_PreparedRequest':
        copied = super().copy()
        copied.__class__ = type(self)
        result = cast(_PreparedRequest, copied)
        result.redacted_fields = (
            list(self.redacted_fields) if self.redacted_fields is not None else None
        )
        return result


def _restore_header(name, value):
    """Restore Latin-1 bytes when normalised text fails Requests' header validation."""
    try:
        check_header_validity((name, value))
    except InvalidHeader as error:
        if not isinstance(value, str) or '\r' in value or '\n' in value:
            raise
        try:
            byte_value = value.encode('latin-1')
        except UnicodeEncodeError:
            raise error from None
        check_header_validity((name, byte_value))
        return byte_value
    return value
