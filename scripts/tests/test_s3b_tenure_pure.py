"""Pure tests for s3b_tenure_editions: the dataset definition, the codelist,
discovery, the data URL, the CSV reader, cells, records, the parts-sum check,
geography, ranks, ledger and planning, the stop conditions and the download
rules.

No database and no network: every CSV and JSON is written by the tests, and
the NOMIS replies and downloads are stubbed. The fixture writers are reused
by test_s3b_tenure_loader.
"""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s3b_tenure_editions as m  # noqa: E402

GT = "local authorities: district / unitary (as of April 2023)"
GT_2021 = "local authorities: district / unitary (as of April 2021)"
LAST = "2023-01-05 09:30:00"
RANK = datetime(2023, 1, 5, 9, 30, 0)
# the fixture's authorities, on the old Barnsley/Sheffield codes (NOMIS's
# form for TYPE424)
CODES = ["E06000001", "E06000002", "E08000016", "E08000019", "E09000033"]
WELSH = ["W06000001", "W06000024"]
NAMES = {"E06000001": "Hartlepool", "E06000002": "Middlesbrough",
         "E08000016": "Barnsley", "E08000019": "Sheffield",
         "E08000038": "Barnsley", "E08000039": "Sheffield",
         "E09000033": "Westminster", "E07000999": "Nowhere",
         "W06000001": "Isle of Anglesey", "W06000024": "Merthyr Tydfil"}
PARTS = [c for c, _ in m.CATEGORIES if c != m.TOTAL]
# per code the eight parts (Hartlepool as published); the total is their sum
BASE_PARTS = {
    "E06000001": [12761, 11073, 239, 3986, 5608, 6338, 856, 68],
    "E06000002": [17224, 15913, 330, 6773, 7233, 11286, 1391, 115],
    "E08000016": [30000, 28000, 500, 9000, 4000, 9000, 1200, 90],
    "E08000019": [70000, 65000, 1500, 40000, 20000, 35000, 4000, 300],
    "E09000033": [15000, 12000, 900, 20000, 15000, 40000, 3000, 400],
}
DEFAULT_PARTS = [100, 90, 5, 20, 10, 30, 4, 1]
SHA_CSV_HELD_BYTES = ("80b690076f2d920329bf424ba6183168"
                      "c3b39ee4ac4971b455fa05d1101df473")
TS054 = {"id": "NM_2072_1", "name": "TS054 - Tenure",
         "mnemonic": "c2021ts054"}
# a declared 2031 dataset, patched in by tests (another census year)
DS_2031 = {"id": "NM_9999_1", "name": "TS054 - Tenure",
           "mnemonic": "c2031ts054", "census_year": "2031",
           "geography": "TYPE999",
           "geography_type": "local authorities: district / unitary (as "
                             "of April 2029)",
           "dimension": "c2031_tenure_9"}


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def counts(code, overrides=None):
    """{column: count} for a code: the eight parts and their sum as total;
    overrides {(code, column): value} applied after (so a total override
    breaks the sum)."""
    parts = BASE_PARTS.get(code, DEFAULT_PARTS)
    out = dict(zip(PARTS, parts))
    out[m.TOTAL] = sum(parts)
    for (c, col), v in (overrides or {}).items():
        if c == code:
            out[col] = v
    return out


def csv_rows(codes=None, welsh=None, *, geography_type=GT, date="2021",
             overrides=None, drop=(), dimension="c2021_tenure_9"):
    """The CSV data rows (dicts), NOMIS order: per area the nine categories
    in codelist order."""
    out = []
    for code in list(codes if codes is not None else CODES) + list(
            welsh if welsh is not None else WELSH):
        cs = counts(code, overrides)
        for col, name in m.CATEGORIES:
            if (code, col) in drop:
                continue
            out.append({"DATE_NAME": date, "GEOGRAPHY_NAME": NAMES.get(
                code, "Somewhere"), "GEOGRAPHY_CODE": code,
                "GEOGRAPHY_TYPE": geography_type,
                f"{dimension.upper()}_NAME": name,
                "OBS_VALUE": cs[col]})
    return out


def write_csv(path, codes=None, welsh=None, *, rows=None, record_count=None,
              offsets=None, header=None, extra_rows=(),
              dimension="c2021_tenure_9", **kw):
    """A NOMIS data CSV as the API writes it (every field quoted except the
    numbers)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows if rows is not None
                else csv_rows(codes, welsh, dimension=dimension, **kw))
    rows += list(extra_rows)
    hdr = header or ["DATE_NAME", "GEOGRAPHY_NAME", "GEOGRAPHY_CODE",
                     "GEOGRAPHY_TYPE", f"{dimension.upper()}_NAME",
                     "OBS_VALUE", "RECORD_OFFSET", "RECORD_COUNT"]
    n = len(rows) if record_count is None else record_count
    buf = io.StringIO()
    buf.write(",".join(f'"{h}"' for h in hdr) + "\n")
    for i, r in enumerate(rows):
        off = i if offsets is None else offsets[i]
        cells = []
        for h in hdr[:6]:
            v = r.get(h, "")
            cells.append(str(v) if h == "OBS_VALUE" else f'"{v}"')
        cells += [str(off), str(n)]
        buf.write(",".join(cells) + "\n")
    path.write_bytes(buf.getvalue().encode("utf-8"))
    return path


def def_json(*, id="NM_2072_1", name="TS054 - Tenure", mnemonic="c2021ts054",
             last=LAST, first=LAST, extra=()):
    """A NOMIS dataset definition (def.sdmx.json) with the annotations S3b
    reads."""
    ann = [{"annotationtext": "Current (being actively updated)",
            "annotationtitle": "Status"}]
    if mnemonic is not None:
        ann.append({"annotationtext": mnemonic, "annotationtitle": "Mnemonic"})
    if first is not None:
        ann.append({"annotationtext": first,
                    "annotationtitle": "FirstReleased"})
    if last is not None:
        ann.append({"annotationtext": last, "annotationtitle": "LastUpdated"})
    kfs = [{"agencyid": "NOMIS", "annotations": {"annotation": ann},
            "id": id, "name": {"value": name, "lang": "en"}}]
    kfs += list(extra)
    return {"structure": {"header": {"id": id,
                                     "prepared": "2026-10-10T16:05:14Z"},
                          "keyfamilies": {"keyfamily": kfs}}}


def write_def(path, **kw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(def_json(**kw)), encoding="utf-8")
    return path


# the published codelist: value, description, parent, derived
CODELIST = [
    (0, "Total: All households", None, "total"),
    (1001, "Owned", 0, "derived"),
    (1, "Owned: Owns outright", 1001, None),
    (2, "Owned: Owns with a mortgage or loan", 1001, None),
    (1002, "Shared ownership", 0, "derived"),
    (3, "Shared ownership: Shared ownership", 1002, None),
    (1003, "Social rented", 0, "derived"),
    (4, "Social rented: Rents from council or Local Authority", 1003, None),
    (5, "Social rented: Other social rented", 1003, None),
    (1004, "Private rented", 0, "derived"),
    (6, "Private rented: Private landlord or letting agency", 1004, None),
    (7, "Private rented: Other private rented", 1004, None),
    (8, "Lives rent free", 0, None),
    (9996, "Owns with a mortgage or loan or shared ownership", 0, "derived"),
    (9997, "Private rented or lives rent free", 0, "derived"),
]


def codelist_json(entries=None):
    codes = []
    for value, desc, parent, kind in entries or CODELIST:
        ann = [{"annotationtext": "Tenure of household",
                "annotationtitle": "TypeName"}]
        if kind == "total":
            ann += [{"annotationtext": 7, "annotationtitle": "ChildCount"},
                    {"annotationtext": "True", "annotationtitle": "IsTotal"}]
        if kind == "derived":
            ann += [{"annotationtext": 2, "annotationtitle": "ChildCount"},
                    {"annotationtext": "True", "annotationtitle": "isDerived"}]
        c = {"annotations": {"annotation": ann},
             "description": {"value": desc, "lang": "en"}, "value": value}
        if parent is not None:
            c["parentcode"] = parent
        codes.append(c)
    return {"structure": {"codelists": {"codelist": [
        {"id": "CL_2072_1_C2021_TENURE_9", "code": codes}]}}}


def search_json(entries=None):
    entries = entries if entries is not None else [
        ("NM_2072_1", "TS054 - Tenure", "c2021ts054"),
        ("NM_2358_1", "PP008 - Tenure", "c2021pp008")]
    kfs = []
    for i, n, mn in entries:
        kfs.append({"id": i, "name": {"value": n, "lang": "en"},
                    "annotations": {"annotation": [
                        {"annotationtext": mn, "annotationtitle": "Mnemonic"},
                        {"annotationtext": LAST,
                         "annotationtitle": "LastUpdated"}]}})
    return {"structure": {"keyfamilies": {"keyfamily": kfs}}}


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0
        p = mock.patch.object(m, "EXPECTED_AREAS", len(CODES))
        p.start()
        self.addCleanup(p.stop)

    def write(self, **kw):
        self.n += 1
        return write_csv(self.root / f"f{self.n}.csv", **kw)

    def read(self, **kw):
        return m.read_csv(self.write(**kw))

    def raises(self, *needles, **kw):
        path = self.write(**kw)
        with self.assertRaises(ValueError) as e:
            m.read_csv(path)
        for n in needles:
            self.assertIn(n, str(e.exception))
        return str(e.exception)


# ---------------------------------------------------------------------------
# The dataset definition, the codelist, discovery, the URL
# ---------------------------------------------------------------------------

class Definition(unittest.TestCase):

    def test_ts054_is_read_with_its_dates(self):
        d = m.dataset_def(def_json())
        self.assertEqual((d["id"], d["name"], d["mnemonic"], d["census_year"]),
                         ("NM_2072_1", "TS054 - Tenure", "c2021ts054",
                          "2021"))
        self.assertEqual(d["first_released"], RANK)
        self.assertEqual(d["last_updated"], RANK)
        self.assertEqual(m.release_rank(d), RANK)
        # text and bytes are read the same
        self.assertEqual(m.dataset_def(json.dumps(def_json()))["rank"], RANK)

    def test_another_dataset_raises(self):
        for kw, needle in ((dict(name="TS055 - Accommodation type"), "TS055"),
                           (dict(mnemonic="c2021ts055"), "c2021ts055"),
                           (dict(id="NM_2073_1"), "NM_2073_1"),
                           (dict(name="TS054 - Tenure", mnemonic=None),
                            "None")):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError) as e:
                    m.dataset_def(def_json(**kw))
                self.assertIn(needle, str(e.exception))

    def test_lastupdated_missing_or_not_a_date_raises(self):
        with self.assertRaises(ValueError) as e:
            m.dataset_def(def_json(last=None))
        self.assertIn("LastUpdated", str(e.exception))
        with self.assertRaises(ValueError):
            m.dataset_def(def_json(last="5 January 2023"))

    def test_two_datasets_in_one_definition_raise(self):
        other = def_json(id="NM_2073_1")["structure"]["keyfamilies"][
            "keyfamily"]
        with self.assertRaises(ValueError) as e:
            m.dataset_def(def_json(extra=other))
        self.assertIn("2", str(e.exception))

    def test_a_declared_other_census_year_is_read(self):
        with mock.patch.dict(m.DATASETS, {"2031": DS_2031}):
            d = m.dataset_def(def_json(id="NM_9999_1", mnemonic="c2031ts054",
                                       last="2033-03-01 09:30:00"))
        self.assertEqual(d["census_year"], "2031")
        self.assertEqual(d["rank"], datetime(2033, 3, 1, 9, 30))
        # undeclared, it raises
        with self.assertRaises(ValueError):
            m.dataset_def(def_json(id="NM_9999_1", mnemonic="c2031ts054"))


class Codelist(unittest.TestCase):

    def test_the_nine_codes_are_looked_up_by_exact_name(self):
        codes = m.category_codes(codelist_json())
        self.assertEqual(codes, {"total_households": 0, "owned_outright": 1,
                                 "owned_mortgage": 2, "shared_ownership": 3,
                                 "social_rented_council": 4,
                                 "social_rented_other": 5,
                                 "private_rented_landlord": 6,
                                 "private_rented_other": 7,
                                 "lives_rent_free": 8})

    def test_an_aggregate_code_used_as_a_part_raises(self):
        # the publisher's own aggregate (derived, with children) carrying a
        # part's name: never taken as a part
        entries = [e if e[0] != 1 else (1, "Owned: Owns outright", 1001,
                                        "derived") for e in CODELIST]
        with self.assertRaises(ValueError) as e:
            m.category_codes(codelist_json(entries))
        self.assertIn("Owned: Owns outright", str(e.exception))
        self.assertIn("aggregate", str(e.exception))
        entries = [e if e[0] != 1001 else (1001, "Owned: Owns outright", 0,
                                           "derived")
                   for e in CODELIST if e[0] != 1]
        with self.assertRaises(ValueError) as e:
            m.category_codes(codelist_json(entries))
        self.assertIn("1001", str(e.exception))

    def test_a_missing_or_repeated_name_raises(self):
        entries = [e for e in CODELIST if e[0] != 8]
        with self.assertRaises(ValueError) as e:
            m.category_codes(codelist_json(entries))
        self.assertIn("Lives rent free", str(e.exception))
        entries = CODELIST + [(42, "Lives rent free", 0, None)]
        with self.assertRaises(ValueError) as e:
            m.category_codes(codelist_json(entries))
        self.assertIn("Lives rent free", str(e.exception))

    def test_the_total_must_be_the_publishers_total(self):
        entries = [e if e[0] != 0 else (0, "Total: All households", None,
                                        None) for e in CODELIST]
        with self.assertRaises(ValueError) as e:
            m.category_codes(codelist_json(entries))
        self.assertIn("IsTotal", str(e.exception))


class Discovery(unittest.TestCase):

    def test_the_declared_dataset_is_found_and_what_was_seen_is_named(self):
        lines = m.discover(search_json(), m.DATASETS["2021"])
        text = " ".join(lines)
        self.assertIn("NM_2072_1", text)
        self.assertIn("TS054 - Tenure", text)
        self.assertIn("no other TS054 dataset", text)

    def test_the_declared_dataset_absent_halts_listing_what_was_seen(self):
        with self.assertRaises(ValueError) as e:
            m.discover(search_json([("NM_2358_1", "PP008 - Tenure",
                                     "c2021pp008")]), m.DATASETS["2021"])
        self.assertIn("NM_2072_1", str(e.exception))
        self.assertIn("PP008", str(e.exception))

    def test_an_undeclared_ts054_dataset_halts_naming_it(self):
        s = search_json([("NM_2072_1", "TS054 - Tenure", "c2021ts054"),
                         ("NM_9999_1", "TS054 - Tenure", "c2031ts054")])
        with self.assertRaises(ValueError) as e:
            m.discover(s, m.DATASETS["2021"])
        self.assertIn("NM_9999_1", str(e.exception))
        self.assertIn("c2031ts054", str(e.exception))
        # declared, it is listed and not a stop
        with mock.patch.dict(m.DATASETS, {"2031": DS_2031}):
            lines = m.discover(s, m.DATASETS["2021"])
        self.assertTrue(any("NM_9999_1" in x for x in lines))

    def test_the_declared_id_under_another_name_halts(self):
        with self.assertRaises(ValueError) as e:
            m.discover(search_json([("NM_2072_1", "TS054 - Housing tenure",
                                     "c2021ts054")]), m.DATASETS["2021"])
        self.assertIn("TS054 - Housing tenure", str(e.exception))


class DataUrl(unittest.TestCase):

    def test_the_url_names_the_nine_codes_and_the_2023_geography(self):
        url = m.data_url(m.category_codes(codelist_json()))
        self.assertTrue(url.startswith(
            "https://www.nomisweb.co.uk/api/v01/dataset/NM_2072_1.data.csv?"))
        for part in ("date=latest", "geography=TYPE424", "measures=20100",
                     "c2021_tenure_9=0,1,2,3,4,5,6,7,8",
                     "select=date_name,geography_name,geography_code,"
                     "geography_type,c2021_tenure_9_name,obs_value,"
                     "record_offset,record_count"):
            self.assertIn(part, url)

    def test_the_codes_come_from_the_codelist_not_assumed(self):
        entries = [(v + 10 if v < 1000 else v, d, p, k)
                   for v, d, p, k in CODELIST]
        entries = [(v, d, (p + 10 if p is not None and p < 1000 else p), k)
                   for v, d, p, k in entries]
        url = m.data_url(m.category_codes(codelist_json(entries)))
        self.assertIn("c2021_tenure_9=10,11,12,13,14,15,16,17,18", url)

    def test_a_code_set_without_the_nine_raises(self):
        with self.assertRaises(ValueError):
            m.data_url({"total_households": 0})


# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

class ReadCsv(Tmp):

    def test_identity_rows_and_welsh_ignored(self):
        f = self.read()
        self.assertEqual(f["census_year"], "2021")
        self.assertEqual(f["geography_type"], GT)
        self.assertEqual(sorted(f["rows"]), CODES)
        self.assertEqual(f["welsh"], len(WELSH))
        self.assertEqual(f["record_count"], (len(CODES) + len(WELSH)) * 9)
        self.assertEqual(f["rows"]["E06000001"]["total_households"], 40929)
        self.assertEqual(f["names"]["E08000016"], "Barnsley")

    def test_another_geography_type_raises_naming_it(self):
        # TYPE on 2021 boundaries would give predecessor districts
        self.raises(GT_2021, geography_type=GT_2021)

    def test_one_row_of_another_geography_type_raises(self):
        rows = csv_rows()
        rows[3]["GEOGRAPHY_TYPE"] = "countries"
        self.raises("countries", "row 5", rows=rows)

    def test_another_date_raises(self):
        self.raises("2011", date="2011")

    def test_record_count_disagreeing_raises(self):
        n = (len(CODES) + len(WELSH)) * 9
        self.raises("RECORD_COUNT", str(n + 1), record_count=n + 1)
        offsets = list(range(n))
        offsets[4] = 99
        self.raises("RECORD_OFFSET", offsets=offsets)

    def test_a_missing_authority_raises(self):
        self.raises("4 authorities", "expected 5", codes=CODES[:-1])

    def test_a_missing_category_or_a_duplicate_raises(self):
        self.raises("E06000002", "Lives rent free",
                    drop={("E06000002", "lives_rent_free")})
        rows = csv_rows()
        self.raises("E06000001", "twice", rows=rows + [dict(rows[0])])

    def test_an_unknown_category_or_code_raises(self):
        rows = csv_rows()
        rows[1]["C2021_TENURE_9_NAME"] = "Owned"
        self.raises("'Owned'", rows=rows)
        rows = csv_rows()
        for r in rows[:9]:
            r["GEOGRAPHY_CODE"] = "S12000033"
        self.raises("S12000033", rows=rows)

    def test_a_blank_or_text_cell_raises(self):
        self.raises("blank", overrides={("E06000002", "owned_outright"): ""})
        self.raises("'x'", overrides={("E06000002", "owned_outright"): "x"})
        self.raises("'1.5'", overrides={("E06000002", "owned_outright"):
                                        "1.5"})

    def test_the_header_must_be_exact(self):
        self.raises("header", header=["DATE_NAME", "GEOGRAPHY_NAME",
                                      "GEOGRAPHY_CODE", "GEOGRAPHY_TYPE",
                                      "C2021_TENURE_9_NAME", "OBS_VALUE",
                                      "RECORD_COUNT", "RECORD_OFFSET"])

    def test_a_declared_other_census_year(self):
        with mock.patch.dict(m.DATASETS, {"2031": DS_2031}):
            p = self.write(date="2031", geography_type=DS_2031[
                "geography_type"], dimension="c2031_tenure_9")
            f = m.read_csv(p, m.DATASETS["2031"])
        self.assertEqual(f["census_year"], "2031")
        with self.assertRaises(ValueError):
            m.read_csv(p)            # read as 2021, it halts


class Cells(unittest.TestCase):

    def test_int_cell(self):
        self.assertEqual(m.int_cell("40929", "w"), 40929)
        self.assertEqual(m.int_cell(" 0 ", "w"), 0)
        self.assertEqual(m.int_cell("-3", "w"), -3)    # checks() rejects it
        for bad in ("", " ", None, "x", "1.0", "1,000", "1e3", "NaN"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as e:
                m.int_cell(bad, "where")
            self.assertIn("where", str(e.exception))


# ---------------------------------------------------------------------------
# Records and checks
# ---------------------------------------------------------------------------

class Records(Tmp):

    def test_build_records(self):
        recs = m.build_records(self.read(), lambda c: c)
        self.assertEqual([r["lad24cd"] for r in recs], sorted(CODES))
        r = recs[0]
        self.assertEqual(r["census_year"], "2021")
        self.assertEqual(set(r) - {"lad24cd", "census_year"}, set(m.VALUES))
        self.assertEqual(r["total_households"], 40929)

    def test_unresolved_or_duplicate_key_raises(self):
        f = self.read()
        with self.assertRaises(ValueError):
            m.build_records(f, {c: c for c in CODES[1:]})
        with self.assertRaises(ValueError):
            m.build_records(f, lambda c: "E06000001")

    def test_checks_pass_and_say_what_passed(self):
        lines = m.checks(m.build_records(self.read(), lambda c: c))
        self.assertTrue(any("sum to the publisher's total" in x
                            for x in lines), lines)

    def test_parts_not_summing_to_the_total_raise(self):
        f = self.read(overrides={("E06000002", "total_households"): 60000})
        with self.assertRaises(ValueError) as e:
            m.checks(m.build_records(f, lambda c: c))
        self.assertIn("E06000002", str(e.exception))
        self.assertIn("60000", str(e.exception))

    def test_a_negative_count_raises(self):
        f = self.read(overrides={("E06000002", "lives_rent_free"): -1,
                                 ("E06000002", "total_households"):
                                 counts("E06000002")[m.TOTAL] - 116})
        with self.assertRaises(ValueError) as e:
            m.checks(m.build_records(f, lambda c: c))
        self.assertIn("negative", str(e.exception))

    def test_the_total_is_the_publishers_never_built(self):
        # a total that differs from the parts is never replaced by their
        # sum: build_records keeps it and checks halts
        f = self.read(overrides={("E06000001", "total_households"): 1})
        recs = m.build_records(f, lambda c: c)
        self.assertEqual(recs[0]["total_households"], 1)

    def test_rows_content_sha_is_key_and_nine_counts(self):
        recs = m.build_records(self.read(), lambda c: c)
        lines = sorted("|".join([r["lad24cd"]] + [str(r[c]) for c in
                                                   m.VALUES]) for r in recs)
        self.assertEqual(m.rows_content_sha(recs), hashlib.sha256(
            "\n".join(lines).encode()).hexdigest())
        # where the file came from never enters the sha
        recs2 = m.build_records(self.read(), lambda c: c)
        self.assertEqual(m.rows_content_sha(recs2), m.rows_content_sha(recs))


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

class _Cur:
    """A cursor answering la_boundaries and la_code_lookup reads."""

    def __init__(self, bounds):
        self.bounds = bounds
        self.result = []

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        if "change_type = 'recode'" in s:
            self.result = [("E08000038", "E08000016"),
                           ("E08000039", "E08000019")]
        elif "FROM public.la_boundaries" in s or "FROM la_boundaries" in s:
            self.result = [(c,) for c in self.bounds]
        else:
            raise AssertionError(f"unexpected SQL {s}")

    def fetchall(self):
        return list(self.result)


class Geography(unittest.TestCase):

    def test_old_codes_resolve_to_themselves(self):
        rmap, problems = m.resolve_codes(_Cur(CODES), set(CODES))
        self.assertEqual(problems, [])
        self.assertEqual(rmap, {c: c for c in CODES})

    def test_e08000038_against_old_halts(self):
        _, problems = m.resolve_codes(_Cur(CODES), {"E06000001", "E08000038"})
        self.assertTrue(any("E08000038" in p and "'old'" in p
                            for p in problems), problems)

    def test_an_unknown_code_is_unexplained(self):
        _, problems = m.resolve_codes(_Cur(CODES), {"E06000001", "E07000999"})
        self.assertTrue(any("UNEXPLAINED E07000999" in p for p in problems))

    def test_the_declaration_is_old_with_the_nomis_evidence(self):
        import geography
        form, ev = geography.DATASET_FORM["3b"]
        self.assertEqual(form, "old")
        self.assertIn("NOMIS NM_2072_1 TYPE424", ev)
        self.assertIn("E08000016/19", ev)
        self.assertIn("2026-10-10", ev)


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

R1 = datetime(2022, 11, 2, 9, 30, 0)
R2 = RANK
R3 = datetime(2024, 6, 1, 9, 30, 0)
URL = "https://www.nomisweb.co.uk/api/v01/dataset/NM_2072_1.data.csv?x"


class Ranks(Tmp):

    def test_rank_from_the_definition_never_the_file_name(self):
        self.assertEqual(m.release_rank(m.dataset_def(def_json(
            last="2022-11-02 09:30:00"))), R1)
        self.assertIsNone(m.release_rank("NM_2072_1_TYPE424_2026-10-10.csv"))
        self.assertIsNone(m.release_rank(None))

    def test_rank_from_an_edition_or_ledger_source(self):
        d = m.dataset_def(def_json())
        src = m.edition_source(d, {"file_name": "x.csv", "where": "u"},
                               "abc")
        self.assertEqual(m.release_rank(src), R2)
        self.assertIn("NOMIS LastUpdated 2023-01-05 09:30:00", src)
        lsrc = m.ledger_source(URL, "2021", R2)
        self.assertEqual(m.release_rank(lsrc), R2)
        self.assertEqual(m.parse_ledger_source(lsrc), (URL, "2021", R2))
        self.assertIsNone(m.parse_ledger_source(URL))

    def test_content_sha256_of_bytes(self):
        p = self.root / "b.bin"
        p.write_bytes(b"abc")
        self.assertEqual(m.content_sha256(p),
                         hashlib.sha256(b"abc").hexdigest())


class Ledger(unittest.TestCase):

    def test_ledger_skip_needs_the_pair_for_the_year(self):
        src = m.ledger_source(URL, "2021", R2)
        checked = {"2021": {(src, "s")}}
        self.assertEqual(m.ledger_complete(checked, URL, "s", {"2021": R2}),
                         ["2021"])
        self.assertIsNone(m.ledger_complete(checked, URL, "other",
                                            {"2021": R2}))
        self.assertIsNone(m.ledger_complete(checked, "data/raw/x.csv", "s",
                                            {"2021": R2}))
        self.assertIsNone(m.ledger_complete({"2031": {(src, "s")}}, URL, "s",
                                            {"2021": R2}))
        self.assertIsNone(m.ledger_complete(checked, URL, "s", {"2021": R2},
                                            stranded=["2021"]))

    def test_the_same_content_under_another_lastupdated_is_compared(self):
        src = m.ledger_source(URL, "2021", R2)
        checked = {"2021": {(src, "s")}}
        self.assertEqual(m.ledger_complete(checked, URL, "s", {"2021": R2},
                                           rank=R2), ["2021"])
        for other in (R1, R3):
            self.assertIsNone(m.ledger_complete(checked, URL, "s",
                                                {"2021": R2}, rank=other))

    def test_a_year_held_from_a_newer_definition_is_read_again(self):
        src = m.ledger_source(URL, "2021", R1)
        self.assertIsNone(m.ledger_complete({"2021": {(src, "s")}}, URL, "s",
                                            {"2021": R2}))

    def test_plan(self):
        src = m.ledger_source(URL, "2021", R2)
        self.assertEqual(m.plan_year("2021", R2, {}, {}, src, "s",
                                     recheck=None, allow_older=False),
                         (["2021"], [], {}))
        self.assertEqual(m.plan_year("2021", R3, {"2021": R2}, {}, src, "s",
                                     recheck=None, allow_older=False),
                         ([], ["2021"], {}))
        new, rev, skip = m.plan_year("2021", R1, {"2021": R2}, {}, src, "s",
                                     recheck=None, allow_older=False)
        self.assertEqual((new, rev), ([], []))
        self.assertTrue(skip["2021"].startswith("older"))
        _, rev, _ = m.plan_year("2021", R1, {"2021": R2}, {}, src, "s",
                                recheck=None, allow_older=True)
        self.assertEqual(rev, ["2021"])
        _, _, skip = m.plan_year("2021", R2, {"2021": R2},
                                 {"2021": {(src, "s")}}, src, "s",
                                 recheck=None, allow_older=False)
        self.assertTrue(skip["2021"].startswith("checked"))
        _, rev, _ = m.plan_year("2021", R2, {"2021": R2},
                                {"2021": {(src, "s")}}, src, "s",
                                recheck="2021", allow_older=False)
        self.assertEqual(rev, ["2021"])
        with self.assertRaises(ValueError):
            m.plan_year("2021", R2, {"2021": R2}, {}, src, "s",
                        recheck="2031", allow_older=False)

    def test_an_earlier_census_than_the_newest_held_is_older(self):
        src = m.ledger_source(URL, "2011", R1)
        new, rev, skip = m.plan_year("2011", R1, {"2021": R2}, {}, src, "s",
                                     recheck=None, allow_older=False)
        self.assertEqual((new, rev), ([], []))
        self.assertTrue(skip["2011"].startswith("older"))


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def recs(codes, **vals):
    out = []
    for c in codes:
        r = {"lad24cd": c, "census_year": "2021"}
        r.update({col: vals.get(col, 1) for col in m.VALUES})
        out.append(r)
    return out


class Stops(unittest.TestCase):

    def test_a_revision_missing_or_adding_an_authority_is_partial(self):
        tip = recs(CODES)
        self.assertEqual(m.period_problems(recs(CODES), tip, kind="revised"),
                         [])
        p = m.period_problems(recs(CODES[:-1]), tip, kind="revised")
        self.assertTrue(p and p[0].startswith(m.PARTIAL), p)
        p = m.period_problems(recs(CODES + ["E07000999"]), tip,
                              kind="revised")
        self.assertTrue(p and p[0].startswith(m.PARTIAL), p)

    def test_a_new_census_into_an_empty_live_table_must_be_2021(self):
        self.assertEqual(m.period_problems(recs(CODES), None, kind="new",
                                           vintage="2021", live_empty=True),
                         [])
        p = m.period_problems(recs(CODES), None, kind="new", vintage="2031",
                              live_empty=True)
        self.assertTrue(any("2021" in x and "2031" in x for x in p), p)

    def test_a_held_other_census_into_an_empty_live_table_stops_too(self):
        tip = recs(CODES)
        p = m.period_problems(recs(CODES), tip, kind="revised",
                              vintage="2031", live_empty=True)
        self.assertTrue(any("empty live table" in x and "2031" in x
                            for x in p), p)
        self.assertEqual(m.period_problems(recs(CODES), tip, kind="revised",
                                           vintage="2021", live_empty=True),
                         [])
        self.assertEqual(m.period_problems(recs(CODES), tip, kind="revised",
                                           vintage="2031", live_empty=False),
                         [])

    def test_differs(self):
        a = recs(CODES)
        self.assertFalse(m._differs(a, recs(CODES)))
        b = recs(CODES)
        b[0]["owned_outright"] = 7
        self.assertTrue(m._differs(a, b))
        self.assertTrue(m._differs(a, recs(CODES[:-1])))


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, body, url):
        self.content = body
        self.url = url
        self.text = body.decode("utf-8")

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, body, final):
        self.body, self.final, self.calls = body, final, []

    def get(self, url, **kw):
        self.calls.append(url)
        return _Resp(self.body, self.final)


class Download(Tmp):

    def body(self, **kw):
        self.n += 1
        return write_csv(self.root / "src" / f"b{self.n}.csv",
                         **kw).read_bytes()

    def test_the_csv_is_saved_under_its_dated_name(self):
        body = self.body()
        dest = self.root / "raw" / "NM_2072_1_TYPE424_2026-10-11.csv"
        s = _Session(body, URL + "&final")
        with quiet() as out:
            path, final = m.fetch_api(URL, dest, session=s)
        self.assertEqual(path, dest)
        self.assertEqual(final, URL + "&final")
        self.assertEqual(path.read_bytes(), body)
        self.assertIn("redirected", out.getvalue())

    def test_same_name_different_content_saved_beside_never_overwritten(self):
        raw = self.root / "raw"
        raw.mkdir()
        old = raw / "NM_2072_1_TYPE424_2026-10-11.csv"
        old.write_bytes(self.body())
        before = old.read_bytes()
        new_body = self.body(overrides={("E06000001", "owned_outright"): 1})
        with quiet():
            path, _ = m.fetch_api(URL, old, session=_Session(new_body, URL))
        sha8 = hashlib.sha256(new_body).hexdigest()[:8]
        self.assertEqual(path.name, f"NM_2072_1_TYPE424_2026-10-11-{sha8}.csv")
        self.assertEqual(path.read_bytes(), new_body)
        self.assertEqual(old.read_bytes(), before)

    def test_an_existing_same_named_file_does_not_skip_the_download(self):
        raw = self.root / "raw"
        raw.mkdir()
        body = self.body()
        dest = raw / "x.csv"
        dest.write_bytes(body)
        s = _Session(body, URL)
        with quiet() as out:
            path, _ = m.fetch_api(URL, dest, session=s)
        self.assertEqual(s.calls, [URL])
        self.assertEqual(path, dest)
        self.assertIn("same content", out.getvalue())

    def test_a_reply_that_is_not_the_expected_format_keeps_nothing(self):
        raw = self.root / "raw"
        for name, body in (("a.csv", b"<html>error</html>"),
                           ("a.json", b"not json"), ("a.txt", b"{}")):
            with self.subTest(name=name), self.assertRaises(SystemExit), \
                    quiet():
                m.fetch_api(URL, raw / name, session=_Session(body, URL))
        self.assertEqual(sorted(p.name for p in raw.iterdir())
                         if raw.exists() else [], [])

    def test_the_definition_json_is_saved(self):
        body = json.dumps(def_json()).encode()
        dest = self.root / "raw" / "NM_2072_1_def_2026-10-11.json"
        with quiet():
            path, _ = m.fetch_api(URL, dest, session=_Session(body, URL))
        self.assertEqual(m.dataset_def(path.read_text())["rank"], RANK)


class Module(unittest.TestCase):

    def test_no_zero_coercion_commit_or_recode_dict(self):
        import check_loaders
        reasons = check_loaders.check_loader(Path(m.__file__),
                                             {"revises_back_series": False})
        self.assertFalse([r for r in reasons if "zero coercion" in r
                          or "commit" in r or "recode dict" in r
                          or "editions_core" in r], reasons)

    def test_the_total_is_never_built_from_the_parts_in_the_module(self):
        src = Path(m.__file__).read_text(encoding="utf-8")
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign,)):
                for t in node.targets:
                    if isinstance(t, ast.Subscript) and isinstance(
                            t.slice, ast.Name) and t.slice.id == "TOTAL":
                        self.fail(f"line {node.lineno}: the total is "
                                  "assigned; it is read from the file only")

    def test_the_held_constants(self):
        self.assertEqual(m.LEGACY_LIVE, (296, "16c5d37b8190e40d"
                                              "ac1b1c8a2919eb78"))
        self.assertEqual(m.LEGACY_RANK, RANK)
        self.assertEqual(m.LEGACY_FILE["bytes_sha256"], SHA_CSV_HELD_BYTES)
        self.assertEqual(len(m.LEGACY_FILE["content_sha256"]), 64)
        self.assertEqual(m.LEGACY_FILE["url"], m.data_url(
            m.category_codes(codelist_json())))
        self.assertEqual(sorted(m.LEGACY_LOADED.values()), [4, 292])

    def test_spec_and_profile(self):
        s = m.SPEC
        self.assertEqual((s.live_table, s.editions_table, s.period_col,
                          s.key_cols), ("la_tenure_2021",
                                        "la_tenure_2021_editions",
                                        "census_year", ("lad24cd",)))
        self.assertTrue(s.live_single_period)
        self.assertFalse(s.refresh_key_changes)
        self.assertFalse(s.refresh_source_whole_period)
        self.assertEqual(dict(s.refresh_from)["source"], "release_label")
        self.assertNotIn("source", s.compare_cols)
        self.assertEqual(set(s.compare_cols), set(m.VALUES))
        self.assertEqual(len(m.VALUES), 9)
        for gen in ("social_rented_total", "private_rented_total"):
            self.assertNotIn(gen, s.refresh_cols)
            self.assertNotIn(gen, [c for c, _ in s.value_cols])
        p = m.PROFILE
        self.assertTrue(p.file_checks)
        self.assertEqual((p.run_agent, p.run_source, p.savepoint),
                         ("s3b_tenure_editions", "3b", "s3b_period"))


if __name__ == "__main__":
    unittest.main()
