"""Single place where blank versus zero is decided (RULES.md rule 1).

A published 0 stays 0. Withheld figures are NULL with a flag. A blank is never
coerced to 0, and a marker that has not been declared stops the load.
Pure Python, no database.
"""
import math
import re
from decimal import Decimal, InvalidOperation
from typing import NamedTuple, Optional

FLAGS = ("suppressed", "missing", "not_applicable")

_NUMBER = re.compile(
    r"[+-]?((\d{1,3}(,\d{3})+|\d+)(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


def _parse(text: str) -> Optional[Decimal]:
    """Parse stripped text as a number (commas only as 3-digit groups)."""
    if _NUMBER.fullmatch(text):
        return Decimal(text.replace(",", ""))
    return None


class UnknownCellError(ValueError):
    """A cell is neither a number nor a declared marker."""


class CellResult(NamedTuple):
    value: Optional[Decimal]
    flag: Optional[str]


def read_cell(raw, markers: dict) -> CellResult:
    for marker, flag in markers.items():
        if flag not in FLAGS:
            raise ValueError(f"marker {marker!r} maps to invalid flag {flag!r}; "
                             f"expected one of {FLAGS}")
        if _parse(marker.strip()) is not None:
            raise ValueError(f"marker key {marker!r} parses as a number; "
                             "a numeric value can never be a marker")

    if raw is None:
        key = ""
    elif isinstance(raw, str):
        key = raw.strip()
    else:
        key = None

    if key is not None:
        if key in markers:
            return CellResult(None, markers[key])
        number = _parse(key)
        if number is not None:
            return CellResult(number, None)
        raise UnknownCellError(f"unknown cell value {raw!r}")

    if isinstance(raw, bool):
        raise UnknownCellError(f"unknown cell value {raw!r} (bool is not a number)")
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            raise UnknownCellError(f"unknown cell value {raw!r}")
        return CellResult(raw, None)
    if isinstance(raw, int):
        return CellResult(Decimal(raw), None)
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise UnknownCellError(f"unknown cell value {raw!r}")
        try:
            return CellResult(Decimal(repr(raw)), None)
        except InvalidOperation:
            raise UnknownCellError(f"unknown cell value {raw!r}")
    raise UnknownCellError(f"unknown cell value {raw!r}")
