"""Preserve JSON numbers through request redaction and response decoding."""

import json
from decimal import Decimal, DecimalException, localcontext
from typing import Callable


class _RawNumber:
    """A numeric token supplied by the JSON parser, never arbitrary application text."""

    __slots__ = ('value',)

    def __init__(self, value: str):
        self.value = value


def decode_float(value: str, dumps: Callable = repr) -> float:
    """Return a float only when its encoded decimal value and zero sign are unchanged."""
    number = float(value)
    encoded = dumps(number)
    if value == encoded:
        return number
    try:
        with localcontext():
            original, restored = Decimal(value), Decimal(encoded)
            if original.is_finite() and restored.is_finite() and original == restored:
                if original or original.is_signed() == restored.is_signed():
                    return number
    except DecimalException:
        pass
    raise ValueError('JSON number cannot be represented without loss')


def _decode_float(value: str):
    try:
        return decode_float(value)
    except ValueError:
        return _RawNumber(value)


def _decode_int(value: str):
    try:
        return int(value) if value != '-0' else _RawNumber(value)
    except ValueError:
        # A valid integer can exceed Python's digit limit; it still needs redaction around it.
        return _RawNumber(value)


def loads(value):
    return json.loads(value, parse_float=_decode_float, parse_int=_decode_int)


def dumps(value):
    try:
        return json.dumps(value)
    except TypeError:
        # Only parser-produced trees enter here. Keep the normal encoder's fast path when possible.
        return ''.join(_encode(value))


def _encode(value):
    if isinstance(value, _RawNumber):
        yield value.value
    elif isinstance(value, dict):
        yield '{'
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield ', '
            yield json.dumps(key) + ': '
            yield from _encode(item)
        yield '}'
    elif isinstance(value, list):
        yield '['
        for index, item in enumerate(value):
            if index:
                yield ', '
            yield from _encode(item)
        yield ']'
    else:
        yield json.dumps(value)
