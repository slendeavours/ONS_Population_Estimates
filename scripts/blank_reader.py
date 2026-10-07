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

_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


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

    if raw is None:
        key = ""
    elif isinstance(raw, str):
        key = raw.strip()
    else:
        key = None

    if key is not None:
        if key in markers:
            return CellResult(None, markers[key])
        text = key.replace(",", "")
        if _NUMBER.fullmatch(text):
            return CellResult(Decimal(text), None)
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
