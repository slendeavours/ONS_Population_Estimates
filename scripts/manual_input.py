"""Manual-file pattern: read a source file that a person placed by hand, or a
curated register, with identity checks. A file is never trusted by its name.

    ROOTS             the only places a manual file may sit
    require_under     path must be under a root, exist, be a regular file, and
                      not be (or sit behind) a symlink
    file_identity     sha256, size, mtime and the workbook's document properties
    read_register     a curated CSV register with an as-at line, an exact
                      header and per-row evidence
    announce_manual   print that the input is manual and what was checked

.xls files are read through xlrd 2.0.1 (pinned; the five SafeLives .xls years
need it). xlrd exposes no document properties, so for .xls the property fields
are None unless the xlrd book carries them. Every halt is SystemExit via
editions_core.halt.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from editions_core import halt

REPO = Path(__file__).resolve().parent.parent
ROOTS = (REPO / "data" / "raw", REPO / "data" / "reference")

AS_AT_KEY = "register_as_at"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROP_KEYS = ("title", "creator", "created", "modified")


def require_under(path, roots=ROOTS) -> Path:
    """Return the resolved path, or halt if it is outside every root, missing,
    not a regular file, or a symlink (or reached through one below its root)."""
    p = Path(path)
    resolved_roots = [Path(r).resolve() for r in roots]
    try:
        resolved = p.resolve(strict=True)
    except (FileNotFoundError, OSError):
        halt(f"manual file does not exist: {p}")
    root = next((r for r in resolved_roots
                 if resolved == r or r in resolved.parents), None)
    if root is None:
        halt(f"manual file {resolved} is not under an allowed root: "
             + ", ".join(str(r) for r in resolved_roots))
    # A symlink anywhere between the given path and its root halts.
    q = Path(p.absolute())
    while True:
        if q.is_symlink():
            halt(f"manual file path passes through a symlink: {q}")
        if q.parent == q or q.resolve() == root:
            break
        q = q.parent
    if not resolved.is_file():
        halt(f"manual file is not a regular file: {resolved}")
    return resolved


def _xlsx_props(path: Path) -> dict:
    out = dict.fromkeys(_PROP_KEYS)
    try:
        with zipfile.ZipFile(path) as z:
            if "docProps/core.xml" not in z.namelist():
                return out
            root = ET.fromstring(z.read("docProps/core.xml"))
    except (zipfile.BadZipFile, ET.ParseError, OSError) as e:
        halt(f"{path.name} is not a readable .xlsx workbook: {e}")
    for el in root:
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in out and el.text is not None:
            out[tag] = el.text.strip() or None
    return out


def _xls_props(book) -> dict:
    """Properties from an xlrd book where it carries them, else None."""
    return {
        "title": getattr(book, "title", None),
        "creator": getattr(book, "author", None)
        or getattr(book, "creator", None),
        "created": getattr(book, "created", None),
        "modified": getattr(book, "modified", None),
    }


def _xls_open_props(path: Path) -> dict:
    try:
        import xlrd
    except ImportError:
        halt("xlrd is not installed (pip install xlrd==2.0.1); "
             "the .xls files cannot be proved without it")
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as e:  # xlrd raises XLRDError and others
        halt(f"{path.name} is not a readable .xls workbook: {e}")
    try:
        return _xls_props(book)
    finally:
        book.release_resources()


def file_identity(path) -> dict:
    """sha256, size, mtime (UTC ISO) and title/creator/created/modified."""
    p = Path(path)
    data = p.read_bytes()
    st = p.stat()
    ident = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": st.st_size,
        "mtime": _dt.datetime.fromtimestamp(
            st.st_mtime, _dt.timezone.utc).isoformat(),
    }
    ext = p.suffix.lower()
    if ext == ".xlsx":
        ident.update(_xlsx_props(p))
    elif ext == ".xls":
        ident.update(_xls_open_props(p))
    else:
        ident.update(dict.fromkeys(_PROP_KEYS))
    return ident


class Rows(list):
    """All register rows, as held. `.unevidenced` is the subset (the same row
    dicts) with any evidence column blank; nothing is dropped or invented."""

    unevidenced: list


def read_register(path, header, evidence_cols, key_cols):
    """Read a curated register. Returns (as_at, rows): as_at a date, rows a
    Rows list of dicts with `.unevidenced` the rows missing any evidence
    column. Halts on: first line not `register_as_at,<yyyy-mm-dd>`; header not
    equal to `header` exactly (an extra or unknown column is named); a row with
    the wrong number of fields; a key or evidence column not in `header`; a
    duplicate key (evidenced or not)."""
    header = list(header)
    for label, cols in (("evidence", evidence_cols), ("key", key_cols)):
        missing = [c for c in cols if c not in header]
        if missing:
            halt(f"register {label} columns not in the expected header: "
                 f"{missing}")
    text = Path(path).read_text(encoding="utf-8-sig")
    first, _, rest = text.partition("\n")
    parts = first.strip().split(",", 1)
    if len(parts) != 2 or parts[0].strip() != AS_AT_KEY:
        halt(f"register first line must be '{AS_AT_KEY},<yyyy-mm-dd>', "
             f"got {first.strip()!r}")
    raw_date = parts[1].strip()
    try:
        if not _ISO_DATE.match(raw_date):
            raise ValueError(raw_date)
        as_at = _dt.date.fromisoformat(raw_date)
    except ValueError:
        halt(f"register_as_at is not a yyyy-mm-dd date: {raw_date!r}")
    reader = csv.reader(io.StringIO(rest))
    got = next(reader, None)
    if got is None:
        halt("register has no header row")
    got = [c.strip() for c in got]
    if got != header:
        extra = [c for c in got if c not in header]
        absent = [c for c in header if c not in got]
        detail = []
        if extra:
            detail.append(f"unknown column(s) {extra}")
        if absent:
            detail.append(f"missing column(s) {absent}")
        if not detail:
            detail.append("columns are in a different order")
        halt("register header differs from the expected header: "
             + "; ".join(detail))
    rows = Rows()
    rows.unevidenced = []
    seen = {}
    for n, rec in enumerate(reader, start=3):
        if not rec or all(not c.strip() for c in rec):
            continue
        if len(rec) != len(header):
            halt(f"register line {n} has {len(rec)} fields, "
                 f"expected {len(header)}")
        row = dict(zip(header, (c.strip() for c in rec)))
        key = tuple(row[c] for c in key_cols)
        if key in seen:
            halt(f"register duplicate key {key} on lines {seen[key]} and {n}")
        seen[key] = n
        rows.append(row)
        if any(not row[c] for c in evidence_cols):
            rows.unevidenced.append(row)
    return as_at, rows


def announce_manual(source, path, identity) -> None:
    """Say plainly that this input is manual, where it came from, and what was
    checked."""
    print(f"MANUAL INPUT for {source}: no publisher endpoint was used.")
    print(f"  file:   {path}")
    print(f"  sha256: {identity.get('sha256')}")
    print(f"  size:   {identity.get('size')} bytes; "
          f"modified on disk {identity.get('mtime')}")
    props = {k: identity.get(k) for k in _PROP_KEYS}
    if any(props.values()):
        print("  document properties: "
              + "; ".join(f"{k}={v}" for k, v in props.items()))
    elif Path(str(path)).suffix.lower() == ".xls":
        print("  document properties: not readable (the reader, xlrd, cannot "
              "read .xls document properties; nothing is claimed about them)")
    else:
        print("  document properties: none recorded in the file")
    print("  checked: path is under data/raw or data/reference, exists, is a "
          "regular file and not a symlink; identity is read from the file "
          "itself, never from its name.")
