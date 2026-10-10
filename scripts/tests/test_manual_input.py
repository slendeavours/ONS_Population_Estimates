"""Tests for manual_input (the manual-file pattern). Fixture files only, all
written inside a temporary directory that is removed afterwards. No database,
no network, nothing outside the temp dir."""
import datetime
import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import manual_input as mi  # noqa: E402

HEADER = ["name", "code", "when", "evidence_url", "evidence_title",
          "checked_on"]
EVID = ["evidence_url", "evidence_title", "checked_on"]
KEY = ["code", "when"]
CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/'
    '2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:dcterms="http://purl.org/dc/terms/"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dc:title>SafeLives Marac data</dc:title><dc:creator>Test Author'
    '</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">2025-07-01T09:00:00Z'
    '</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">'
    '2025-08-02T10:30:00Z</dcterms:modified></cp:coreProperties>')


def _expect_halt(tc, fn, *a, **k):
    with tc.assertRaises(SystemExit) as cm:
        fn(*a, **k)
    tc.assertTrue(str(cm.exception.code).startswith("HALT:"))
    return str(cm.exception.code)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def write(self, d, name, text):
        p = d / name
        p.write_text(text, encoding="utf-8", newline="")
        return p


class RequireUnder(Base):
    def test_inside_returns_resolved(self):
        f = self.write(self.root, "a.csv", "x")
        self.assertEqual(mi.require_under(f, roots=(self.root,)), f)

    def test_outside_halts(self):
        f = self.write(self.outside, "a.csv", "x")
        _expect_halt(self, mi.require_under, f, roots=(self.root,))

    def test_dotdot_escape_halts(self):
        self.write(self.outside, "a.csv", "x")
        p = self.root / ".." / "outside" / "a.csv"
        _expect_halt(self, mi.require_under, p, roots=(self.root,))

    def test_missing_halts(self):
        _expect_halt(self, mi.require_under, self.root / "nope.csv",
                     roots=(self.root,))

    def test_directory_halts(self):
        d = self.root / "sub"
        d.mkdir()
        _expect_halt(self, mi.require_under, d, roots=(self.root,))

    def test_symlink_halts(self):
        target = self.write(self.root, "real.csv", "x")
        link = self.root / "link.csv"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted here")
        _expect_halt(self, mi.require_under, link, roots=(self.root,))

    def test_symlink_detected_by_check_even_without_privilege(self):
        from unittest import mock
        f = self.write(self.root, "x.csv", "x")
        real = Path.is_symlink
        with mock.patch.object(
                Path, "is_symlink",
                lambda self_: self_.name == "x.csv" or real(self_)):
            _expect_halt(self, mi.require_under, f, roots=(self.root,))

    def test_default_roots_are_raw_and_reference(self):
        names = {(r.parent.name, r.name) for r in mi.ROOTS}
        self.assertEqual(names, {("data", "raw"), ("data", "reference")})
        self.assertEqual(mi.ROOTS[0].parent.parent.name,
                         "ONS_Population_Estimates")


class Identity(Base):
    def _xlsx(self, name="f.xlsx", core=CORE):
        p = self.root / name
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            if core is not None:
                z.writestr("docProps/core.xml", core)
        return p

    def test_xlsx_docprops(self):
        p = self._xlsx()
        ident = mi.file_identity(p)
        self.assertEqual(len(ident["sha256"]), 64)
        self.assertEqual(ident["size"], p.stat().st_size)
        self.assertTrue(ident["mtime"])
        self.assertEqual(ident["title"], "SafeLives Marac data")
        self.assertEqual(ident["creator"], "Test Author")
        self.assertEqual(ident["created"], "2025-07-01T09:00:00Z")
        self.assertEqual(ident["modified"], "2025-08-02T10:30:00Z")

    def test_xlsx_without_docprops_is_none(self):
        ident = mi.file_identity(self._xlsx("g.xlsx", core=None))
        for k in ("title", "creator", "created", "modified"):
            self.assertIsNone(ident[k])

    def test_sha_is_of_the_bytes(self):
        p = self._xlsx()
        self.assertEqual(mi.file_identity(p)["sha256"],
                         hashlib.sha256(p.read_bytes()).hexdigest())

    def test_other_extension_has_none_props(self):
        p = self.write(self.root, "r.csv", "a,b\n")
        ident = mi.file_identity(p)
        self.assertIsNone(ident["title"])
        self.assertEqual(ident["size"], 4)

    def test_bad_xlsx_halts(self):
        p = self.write(self.root, "bad.xlsx", "not a zip")
        _expect_halt(self, mi.file_identity, p)

    def test_bad_xls_halts(self):
        p = self.write(self.root, "bad.xls", "not a workbook")
        _expect_halt(self, mi.file_identity, p)

    def test_xls_props_through_xlrd_book(self):
        class Book:
            title = "T"
            author = None
        got = mi._xls_props(Book())
        self.assertEqual(got["title"], "T")
        self.assertIsNone(got["creator"])
        self.assertIsNone(got["created"])
        self.assertIsNone(got["modified"])

    def test_xlrd_is_pinned_version(self):
        import xlrd
        self.assertEqual(xlrd.__version__, "2.0.1")


def _reg(as_at="register_as_at,2026-10-10", header=None, rows=()):
    lines = [as_at, ",".join(header or HEADER)]
    lines += list(rows)
    return "\n".join(lines) + "\n"


GOOD = "A,E1,2020-01-01,http://x,Title,2026-10-01"
NOEV = "B,E2,2021-01-01,,,"
PART = "C,E3,2022-01-01,http://y,,2026-10-01"


class Register(Base):
    def read(self, text, **kw):
        p = self.write(self.root, "reg.csv", text)
        return mi.read_register(p, kw.get("header", HEADER),
                                kw.get("evidence_cols", EVID),
                                kw.get("key_cols", KEY))

    def test_good_register(self):
        as_at, rows = self.read(_reg(rows=[GOOD]))
        self.assertEqual(as_at, datetime.date(2026, 10, 10))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "E1")
        self.assertEqual(rows.unevidenced, [])

    def test_unevidenced_returned_not_dropped_not_invented(self):
        as_at, rows = self.read(_reg(rows=[GOOD, NOEV, PART]))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["code"] for r in rows.unevidenced], ["E2", "E3"])
        for r in rows.unevidenced:
            self.assertIn(r, rows)
        self.assertEqual(rows.unevidenced[0]["evidence_url"], "")

    def test_wrong_header_halts(self):
        h = HEADER[:]
        h[0], h[1] = h[1], h[0]
        _expect_halt(self, self.read, _reg(header=h, rows=[GOOD]))

    def test_missing_column_halts(self):
        _expect_halt(self, self.read, _reg(header=HEADER[:-1], rows=[]))

    def test_extra_column_halts(self):
        msg = _expect_halt(self, self.read,
                           _reg(header=HEADER + ["surprise"], rows=[]))
        self.assertIn("surprise", msg)

    def test_duplicate_key_halts(self):
        _expect_halt(self, self.read, _reg(rows=[GOOD, GOOD]))

    def test_duplicate_key_halts_even_if_unevidenced(self):
        dup = "Z,E2,2021-01-01,,,"
        _expect_halt(self, self.read, _reg(rows=[NOEV, dup]))

    def test_as_at_missing_halts(self):
        _expect_halt(self, self.read,
                     ",".join(HEADER) + "\n" + GOOD + "\n")

    def test_as_at_not_a_date_halts(self):
        for bad in ("register_as_at,", "register_as_at,10/10/2026",
                    "register_as_at,2026-13-45", "register_as_at,soon"):
            _expect_halt(self, self.read, _reg(as_at=bad, rows=[GOOD]))

    def test_short_row_halts(self):
        _expect_halt(self, self.read, _reg(rows=["A,E1,2020-01-01"]))

    def test_key_or_evidence_column_not_in_header_halts(self):
        _expect_halt(self, self.read, _reg(rows=[GOOD]), key_cols=["nope"])
        _expect_halt(self, self.read, _reg(rows=[GOOD]),
                     evidence_cols=["nope"])

    def test_quoted_commas_and_bom(self):
        row = '"A, B",E1,2020-01-01,http://x,"T, t",2026-10-01'
        as_at, rows = self.read("﻿" + _reg(rows=[row]))
        self.assertEqual(rows[0]["name"], "A, B")

    def test_empty_file_halts(self):
        _expect_halt(self, self.read, "")


class Announce(Base):
    def test_prints_manual_source_path_and_checks(self):
        f = self.write(self.root, "a.csv", "x")
        ident = mi.file_identity(f)
        buf = io.StringIO()
        with redirect_stdout(buf):
            mi.announce_manual("S17 MARAC", f, ident)
        out = buf.getvalue()
        self.assertIn("MANUAL INPUT", out)
        self.assertIn("S17 MARAC", out)
        self.assertIn(str(f), out)
        self.assertIn(ident["sha256"], out)
        self.assertIn("checked", out.lower())

    def test_xls_properties_are_not_called_absent(self):
        f = self.write(self.root, "a.xls", "x")
        ident = {"sha256": "ab", "size": 1, "mtime": "t"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            mi.announce_manual("S17 MARAC", f, ident)
        out = buf.getvalue()
        self.assertIn("cannot read .xls document properties", out)
        self.assertNotIn("none recorded", out)


if __name__ == "__main__":
    unittest.main()
