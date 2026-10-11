"""Tests for the S7 loader in s7_boundaries_editions (the commands,
migrate-legacy, restore-edition) on throwaway tables.

No network: the ArcGIS replies (the service list) and the downloads (the
layer JSON and the GeoJSON) are stubbed, and every file is written by the
test (write_geojson and write_layer from test_s7_boundaries_pure). Database
tests run on a connection from _db.get_conn() inside a transaction that is
always rolled back (rolled_back below); nothing here commits. The commands
get a stand-in connection whose commit and rollback act on a savepoint of
that transaction, and a cursor proxy whose .connection does the same, so
the engine's per-period commit never reaches the real connection. The only
tables written are zz_s7_live (a LIKE copy of la_boundaries, no foreign
key), zz_s7_editions and its ledger (no foreign key either), created inside
that transaction (refused if any already exists), so they never persist.
The run log is stubbed; the real la_boundaries and la_code_lookup are read
(never written).
"""
import contextlib
import dataclasses
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
import s7_boundaries_editions as m  # noqa: E402
from _db import get_conn  # noqa: E402
from test_s7_boundaries_pure import (CODES, CODES_NEW, LAST_MS, P24,  # noqa
                                     P26, RANK, V24, V26, geometry,
                                     service_name, services_json,
                                     write_geojson, write_layer)

ZZ = dataclasses.replace(m.SPEC, name="zz_s7", live_table="zz_s7_live",
                         editions_table="zz_s7_editions")
LEDGER = "zz_s7_editions_file_checks"
N = len(CODES)
LOADED = "2026-03-25 15:13:11.744860+00"
LIVE_COLS = ("lad24cd", "lad24nm", "longitude", "latitude", "shape_area",
             "geojson", "source_date", "loaded_at")
LATER_MS = 1800000000000        # a later lastEditDate (2027-01-15)
EARLIER_MS = 1700000000000      # an earlier one (2023-11-14)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class _Tx:
    SP = "zz_s7_cmd"

    def __init__(self, cur):
        self.cur = cur
        cur.execute(f"SAVEPOINT {self.SP}")

    def commit(self):
        self.cur.execute(f"RELEASE SAVEPOINT {self.SP}")
        self.cur.execute(f"SAVEPOINT {self.SP}")

    def rollback(self):
        self.cur.execute(f"ROLLBACK TO SAVEPOINT {self.SP}")

    def done(self):
        self.cur.execute(f"RELEASE SAVEPOINT {self.SP}")


class _FakeConn:
    def __init__(self, real, tx):
        self.encoding = real.encoding
        self.tx = tx

    def commit(self):
        self.tx.commit()

    def rollback(self):
        self.tx.rollback()


class _Cur:
    def __init__(self, cur, tx):
        self._cur = cur
        self.connection = _FakeConn(cur.connection, tx)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _Borrowed:
    def __init__(self, cur):
        self.tx = _Tx(cur)
        self.cur = _Cur(cur, self.tx)

    @contextmanager
    def cursor(self):
        yield self.cur

    def commit(self):
        self.tx.commit()

    def rollback(self):
        self.tx.rollback()

    def close(self):
        pass


@contextmanager
def specs():
    with mock.patch.object(m, "SPEC", ZZ), \
            mock.patch.object(m, "EXPECTED_AREAS", N):
        yield


@contextmanager
def rolled_back(conn, *, ddl=True):
    cur = conn.cursor()
    try:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('zz_s7_live', 'zz_s7_editions', %s)", (LEDGER,))
        if cur.fetchall():
            raise RuntimeError("a zz_s7 table already exists as a real "
                               "table; refusing to run")
        cur.execute("CREATE TABLE public.zz_s7_live (LIKE "
                    "public.la_boundaries INCLUDING ALL)")
        with specs():
            if ddl:
                m.create_all(cur)
            yield cur
    finally:
        cur.close()
        conn.rollback()


def count(cur, table, where="", args=None):
    cur.execute(f"SELECT COUNT(*) FROM public.{table} {where}", args)
    return cur.fetchone()[0]


def editions(cur, period=P24):
    cur.execute("SELECT edition, supersedes, COUNT(*) FROM "
                "public.zz_s7_editions WHERE source_date = %s "
                "GROUP BY 1, 2 ORDER BY 1", (period,))
    return cur.fetchall()


def ledger(cur):
    cur.execute(f"SELECT source_date::text, source_file, outcome, edition "
                f"FROM public.{LEDGER} ORDER BY id")
    return cur.fetchall()


def live_val(cur, la, col="lad24nm"):
    cur.execute(f"SELECT {col} FROM public.zz_s7_live WHERE lad24cd = %s",
                (la,))
    r = cur.fetchone()
    return r[0] if r else None


def live_md5(cur):
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in LIVE_COLS)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"lad24cd)) FROM public.zz_s7_live")
    return cur.fetchone()


def real_md5(cur, table, key):
    cur.execute(f"SELECT COUNT(*), md5(string_agg(t::text, E'\\n' ORDER BY "
                f"t::text)) FROM public.{table} t")
    return cur.fetchone()


class Fixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = get_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0
        self.api = {m.list_url(): services_json()}
        self.urls = {}
        self.final = {}
        self.fetched = []

    def release(self, codes=None, vintage=V24, *, last=LAST_MS,
                register=True, **kw):
        """Write a layer JSON and a GeoJSON reply; register them as what
        ArcGIS returns for the vintage."""
        self.n += 1
        d = self.root / f"t{self.n}"
        layer = write_layer(d / "layer.json", vintage, last=last)
        gj = write_geojson(d / "reply.geojson", codes, vintage, **kw)
        if register:
            svc = service_name(vintage)
            self.urls[m.layer_url(svc)] = layer
            self.urls[m.query_url(svc, f"LAD{m.VINTAGES[vintage]['yy']}"
                                       "CD")] = gj
        return gj, layer

    def run_main(self, cur, argv):
        """main(argv) on the throwaway tables: (rc or 'halt', text, log)."""
        borrowed = _Borrowed(cur)

        def fetch_api(url, dest, session=None):
            self.fetched.append((url, Path(dest).name))
            return self.urls[url], self.final.get(url, url)

        out = io.StringIO()
        with specs(), \
                mock.patch.object(m, "_conn", return_value=borrowed), \
                mock.patch.object(m, "fetch_json",
                                  side_effect=lambda u, s=None: self.api[u]), \
                mock.patch.object(m, "fetch_api", side_effect=fetch_api), \
                mock.patch.object(m, "log_run") as logged, \
                contextlib.redirect_stdout(out):
            try:
                rc = m.main([str(a) for a in argv])
            except SystemExit as e:
                rc = "halt"
                out.write(f"\n{e.code}")
            finally:
                borrowed.tx.done()
        return rc, out.getvalue(), logged

    def ok(self, cur, argv):
        rc, text, logged = self.run_main(cur, argv)
        self.assertEqual(rc, 0, text)
        return text, logged

    def seed_live(self, cur, codes=None, **kw):
        """Held-style live rows built from a fixture reply (one loaded_at,
        source_date 2024-05-01, as the real table holds them)."""
        gj, layer = self.release(codes, register=False, **kw)
        recs = m.build_records(m.read_geojson(gj, V24))
        for r in recs:
            cur.execute(
                "INSERT INTO public.zz_s7_live (lad24cd, lad24nm, longitude, "
                "latitude, shape_area, geojson, source_date, loaded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (r["lad24cd"], r["lad24nm"], r["longitude"], r["latitude"],
                 r["shape_area"], r["geojson"], P24, LOADED))
        files = {"name": gj.name, "content_sha256": m.rows_content_sha(recs),
                 "bytes_sha256": m.content_sha256(gj),
                 "url": m.query_url(service_name(V24), "LAD24CD")}
        return gj, layer, files

    def legacy(self, cur):
        return m.live_state(cur)[:2]

    def loaded(self, cur):
        return m.live_state(cur)[2]

    def migrate(self, cur, gj, layer, files, write=True, **kw):
        with quiet() as out:
            plan = m.migrate_legacy(
                cur, gj, layer, write=write,
                legacy=kw.get("legacy") or self.legacy(cur),
                loaded=kw.get("loaded") or self.loaded(cur), files=files)
        return plan, out.getvalue()

    def seed(self, cur):
        """Live rows, then edition 1 as loaded and its ledger row; the same
        reply registered as what ArcGIS returns."""
        gj, layer, files = self.seed_live(cur)
        self.migrate(cur, gj, layer, files)
        self.urls[m.layer_url(service_name(V24))] = layer
        self.urls[files["url"]] = gj
        return gj, layer, files


class Migrate(Fixture):

    def test_preview_before_ddl_reads_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            gj, layer, files = self.seed_live(cur)
            plan, text = self.migrate(cur, gj, layer, files, write=False)
            self.assertIn("0 differences", text)
            self.assertIn(f"{N} of {N} geometries identical", text)
            self.assertEqual(plan["cells"], N * 5)
            self.assertFalse(pe.table_exists(cur, "zz_s7_editions"))
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, gj, layer, files)
            self.assertIn("ddl", str(e.exception.code))

    def test_load_preview_before_ddl_compares_with_live(self):
        with rolled_back(self.conn, ddl=False) as cur:
            gj, layer, _ = self.seed_live(cur)
            text, _ = self.ok(cur, ["load", "--file", gj, "--layer", layer])
            self.assertIn("compares the reply with the LIVE table", text)
            self.assertIn(f"{P24}: unchanged", text)
            rc, text, _ = self.run_main(cur, ["load", "--file", gj, "--layer",
                                              layer, "--commit"])
            self.assertEqual(rc, "halt")
            self.assertIn("ddl --commit", text)

    def test_proof_passes_and_byte_identical_rereads_are_unchanged(self):
        with rolled_back(self.conn) as cur:
            gj, layer, files = self.seed_live(cur)
            before = live_md5(cur)
            plan, text = self.migrate(cur, gj, layer, files)
            self.assertIn("0 differences", text)
            self.assertEqual(plan["rows"], N)
            self.assertEqual(editions(cur), [(1, None, N)])
            cur.execute("SELECT DISTINCT source_file, release_label, "
                        "published_date FROM public.zz_s7_editions")
            (sf, label, pd_), = cur.fetchall()
            self.assertEqual(sf, f"as loaded: {gj.name}; source_date "
                                 f"{P24}; ArcGIS lastEditDate {LAST_MS} "
                                 "(2024-07-02 13:28:58 UTC)")
            self.assertEqual(label, core.AS_LOADED_LABEL)
            self.assertEqual(pd_, date(2026, 3, 25))
            self.assertEqual(ledger(cur), [(
                P24, f"{files['url']} (source_date {P24}; ArcGIS "
                     f"lastEditDate {LAST_MS} (2024-07-02 13:28:58 UTC))",
                "unchanged", 1)])
            self.assertTrue(pe.status(cur, m.profile())["ok"])
            # the edition holds the live jsonb exactly
            cur.execute("SELECT COUNT(*) FROM public.zz_s7_live l JOIN "
                        "public.zz_s7_editions e USING (lad24cd) WHERE "
                        "l.geojson::text = e.geojson::text")
            self.assertEqual(cur.fetchone()[0], N)
            # the same content on every path: unchanged against edition 1
            self.urls[m.layer_url(service_name(V24))] = layer
            self.urls[files["url"]] = gj
            text, _ = self.ok(cur, ["load"])
            self.assertIn("nothing compared", text)
            for argv in (["load", "--recheck", P24],
                         ["load", "--file", gj, "--layer", layer],
                         ["load", "--file", gj, "--layer", layer, "--commit"],
                         ["load", "--file", gj, "--layer", layer, "--recheck",
                          P24, "--commit"],
                         ["load", "--recheck", P24, "--commit"]):
                with self.subTest(argv=argv):
                    text, _ = self.ok(cur, argv)
                    self.assertIn(f"{P24}: unchanged", text)
                    self.assertNotIn("REJECTED", text)
                    self.assertNotIn("two sources claim", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual([o for _, _, o, _ in ledger(cur)],
                             ["unchanged"] * 4)
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("would write: none", text)
            self.assertNotIn("Scott's decision", text)
            self.assertEqual(live_md5(cur), before)

    def test_a_planted_held_difference_stops_and_rolls_back(self):
        with rolled_back(self.conn) as cur:
            gj, layer, files = self.seed_live(cur)
            cur.execute("UPDATE public.zz_s7_live SET geojson = %s::jsonb "
                        "WHERE lad24cd = 'E06000001'",
                        (json.dumps(geometry(0, d=0.0001)),))
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_LOADED", self.loaded(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                rc, text, logged = self.run_main(
                    cur, ["migrate-legacy", "--file", gj, "--layer", layer,
                          "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("proof failed", text)
            self.assertIn("E06000001", text)
            self.assertEqual(count(cur, "zz_s7_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)
            logged.assert_not_called()

    def test_the_command_previews_then_commits(self):
        with rolled_back(self.conn) as cur:
            gj, layer, files = self.seed_live(cur)
            argv = ["migrate-legacy", "--file", gj, "--layer", layer]
            with mock.patch.object(m, "LEGACY_LIVE", self.legacy(cur)), \
                    mock.patch.object(m, "LEGACY_LOADED", self.loaded(cur)), \
                    mock.patch.object(m, "LEGACY_FILE", files):
                text, logged = self.ok(cur, argv)
                self.assertIn("PREVIEW", text)
                self.assertEqual(count(cur, "zz_s7_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, argv + ["--simulate"])
                self.assertIn("SIMULATION", text)
                self.assertEqual(count(cur, "zz_s7_editions"), 0)
                logged.assert_not_called()
                text, logged = self.ok(cur, argv + ["--commit"])
            self.assertEqual(editions(cur), [(1, None, N)])
            logged.assert_called_once()
            rc, text, _ = self.run_main(cur, ["migrate-legacy", "--file", gj])
            self.assertEqual(rc, "halt")
            self.assertIn("--layer", text)

    def test_a_wrong_content_live_state_or_loaded_at_stops(self):
        with rolled_back(self.conn) as cur:
            gj, layer, files = self.seed_live(cur)
            bad = dict(files, content_sha256="0" * 64)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, gj, layer, bad)
            self.assertIn("expected 00000000", str(e.exception.code))
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, gj, layer, files, legacy=(N, "x"))
            self.assertIn("not as surveyed", str(e.exception.code))
            loaded = dict(self.loaded(cur))
            cur.execute("UPDATE public.zz_s7_live SET loaded_at = now() "
                        "WHERE lad24cd = 'E06000001'")
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, gj, layer, files, loaded=loaded)
            self.assertIn("loaded_at", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s7_editions"), 0)
            self.assertEqual(count(cur, LEDGER), 0)

    def test_a_layer_of_another_edit_or_vintage_stops(self):
        with rolled_back(self.conn) as cur:
            gj, _, files = self.seed_live(cur)
            later = write_layer(self.root / "later.json", last=LATER_MS)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, gj, later, files)
            self.assertIn("lastEditDate", str(e.exception.code))
            other = write_layer(self.root / "v26.json", V26)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, gj, other, files)
            self.assertIn("LAD_MAY_2026_UK_BGC", str(e.exception.code))
            self.assertEqual(count(cur, "zz_s7_editions"), 0)

    def test_a_non_empty_editions_table_stops(self):
        with rolled_back(self.conn) as cur:
            gj, layer, files = self.seed_live(cur)
            legacy, loaded = self.legacy(cur), self.loaded(cur)
            self.migrate(cur, gj, layer, files)
            with self.assertRaises(SystemExit) as e:
                self.migrate(cur, gj, layer, files, legacy=legacy,
                             loaded=loaded)
            self.assertIn("runs once", str(e.exception.code))


class Load(Fixture):

    def test_a_geometry_change_to_the_live_vintage_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live_md5(cur)
            moved = geometry(0, d=0.001)
            self.release(last=LATER_MS, geoms={"E06000001": moved})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P24}: REJECTED", text)
            self.assertIn("geometry changed 1 (E06000001)", text)
            self.assertIn("Scott's decision", text)
            self.assertIn(f"--acknowledge {P24}", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 1)
            self.assertIn("PARTIAL RUN (exit 1)", logged.call_args[0][2])
            text, logged = self.ok(cur, ["load", "--commit", "--acknowledge",
                                         P24])
            self.assertIn("ACKNOWLEDGED", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertIn("--acknowledge", logged.call_args[0][2])
            # live waits for refresh-latest, which needs --acknowledge too
            self.assertEqual(live_md5(cur), before)
            self.assertEqual(pe.status(cur, m.profile())["pending_refresh"],
                             [P24])
            text, _ = self.ok(cur, ["refresh-latest"])
            self.assertIn("this changes the map's geometry: Scott's decision",
                          text)
            self.assertIn("geometry changed 1 (E06000001)", text)
            self.assertEqual(live_md5(cur), before)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Scott's decision", text)
            self.assertEqual(live_md5(cur), before)
            self.ok(cur, ["refresh-latest", "--commit", "--acknowledge", P24])
            self.assertEqual(json.loads(live_val(cur, "E06000001",
                                                 "geojson::text")), moved)
            self.assertEqual(count(cur, "zz_s7_live"), N)
            cur.execute("SELECT COUNT(*) FROM public.zz_s7_live l JOIN "
                        "public.zz_s7_editions e USING (lad24cd) WHERE "
                        "e.edition = 2 AND l.loaded_at = e.loaded_at AND "
                        "l.lad24cd = 'E06000001'")
            self.assertEqual(cur.fetchone()[0], 1)
            self.assertTrue(pe.status(cur, m.profile())["ok"])

    def test_a_name_change_to_the_live_vintage_needs_acknowledge(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(last=LATER_MS, overrides={
                ("E06000002", "LAD24NM"): "Middlesbrough Town"})
            rc, text, _ = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("Middlesbrough Town", text)
            self.assertIn(f"--acknowledge {P24}", text)
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_a_later_layer_changing_only_attributes_is_the_next_edition(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            before = live_md5(cur)
            self.release(last=LATER_MS,
                         overrides={("E06000002", "LAT"): 52.35})
            text, _ = self.ok(cur, ["load", "--commit"])
            self.assertIn(f"{P24}: revised", text)
            self.assertNotIn("REJECTED", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])
            self.assertEqual(live_md5(cur), before)
            rc, text, _ = self.run_main(cur, ["refresh-latest", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("Scott's decision", text)

    def test_equal_last_edit_date_with_different_content_stops(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(overrides={("E06000002", "LAT"): 52.35})
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn("two sources claim the same release", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            rc, text, logged = self.run_main(cur, ["load"])
            self.assertEqual(rc, 1, text)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--commit",
                                         "--accept-reissue", P24])
            self.assertIn("ACCEPTED REISSUE", text)
            self.assertEqual(editions(cur), [(1, None, N), (2, 1, N)])

    def older(self, cur, how):
        """Seed from the May 2024 lastEditDate, then offer a layer of an
        earlier one (with the same content) by the API or by --file."""
        self.seed(cur)
        gj, layer = self.release(last=EARLIER_MS, register=how != "file")
        argv = ["load", "--commit"]
        if how == "file":
            argv += ["--file", gj, "--layer", layer]
        return argv

    def test_an_earlier_last_edit_date_halts_on_every_path(self):
        for how in ("api", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                rc, text, logged = self.run_main(cur, self.older(cur, how))
                self.assertEqual(rc, "halt", text)
                self.assertIn(f"{P24} (older", text)
                self.assertEqual(editions(cur), [(1, None, N)])
                self.assertEqual(len(ledger(cur)), 1)
                logged.assert_not_called()

    def test_allow_older_file_overrides_and_is_logged(self):
        with rolled_back(self.conn) as cur:
            argv = self.older(cur, "file") + ["--allow-older-file"]
            text, logged = self.ok(cur, argv)
            self.assertIn(f"{P24}: unchanged", text)
            self.assertIn("--allow-older-file given", logged.call_args[0][2])
            self.assertEqual(editions(cur), [(1, None, N)])

    def test_a_second_vintage_is_stored_as_editions_only(self):
        for how in ("api", "file"):
            with self.subTest(how=how), rolled_back(self.conn) as cur:
                self.seed(cur)
                before = live_md5(cur)
                gj, layer = self.release(CODES_NEW, V26, last=1788960865457,
                                         register=how == "api")
                argv = (["load", "--vintage", V26, "--commit"] if how == "api"
                        else ["load", "--file", gj, "--layer", layer,
                              "--commit"])
                text, logged = self.ok(cur, argv)
                self.assertIn(f"{P26}: held-not-live", text)
                self.assertIn("stored edition 1 only", text)
                self.assertIn("codes added 2 (E08000038, E08000039); removed "
                              "2 (E08000016, E08000019)", text)
                self.assertIn("names changed 0", text)
                self.assertEqual(editions(cur, P26), [(1, None, N)])
                self.assertEqual(live_md5(cur), before)
                cur.execute("SELECT DISTINCT source_date::text FROM "
                            "public.zz_s7_live")
                self.assertEqual(cur.fetchall(), [(P24,)])
                self.assertEqual([(p, o) for p, _, o, _ in ledger(cur)],
                                 [(P24, "unchanged"), (P26, "held-not-live")])
                self.assertIn(f"{P26} held-not-live", logged.call_args[0][2])
                st = pe.status(cur, m.profile())
                self.assertTrue(st["ok"], st)
                self.assertEqual(st["held_not_live"], [P26])
                text, _ = self.ok(cur, ["status"])
                self.assertIn(f"held, not live: {P26}", text)
                self.assertIn("Scott's decision, never a load", text)
                text, _ = self.ok(cur, ["refresh-latest"])
                self.assertIn("would write: none", text)
                rc, text, _ = self.run_main(cur, ["refresh-latest",
                                                  "--commit", "--accept-drift",
                                                  P26])
                self.assertEqual(rc, "halt", text)
                self.assertIn(P26, text)
                rc, text, _ = self.run_main(cur, ["refresh-latest",
                                                  "--commit", "--acknowledge",
                                                  P26])
                self.assertEqual(rc, "halt", text)
                self.assertEqual(live_md5(cur), before)
                # the same reply again: ledger skip, or unchanged on recheck
                if how == "file":
                    text, _ = self.ok(cur, argv)
                    self.assertIn("nothing compared", text)
                    text, _ = self.ok(cur, argv + ["--recheck", P26])
                    self.assertIn(f"{P26}: unchanged", text)
                self.assertEqual(editions(cur, P26), [(1, None, N)])

    def test_acknowledge_is_only_for_the_live_vintage(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(CODES_NEW, V26)
            rc, text, _ = self.run_main(cur, ["load", "--vintage", V26,
                                              "--commit", "--acknowledge",
                                              P26])
            self.assertEqual(rc, "halt", text)
            self.assertIn("only the live vintage", text)
            rc, text, _ = self.run_main(cur, ["load", "--commit",
                                              "--acknowledge", "2011-01-01"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("2011-01-01", text)
            # a flag on a run whose reply the ledger already holds: refused
            rc, text, _ = self.run_main(cur, ["load", "--commit",
                                              "--accept-reissue", P24])
            self.assertEqual(rc, "halt", text)
            self.assertIn("nothing compared", text)
            self.assertIn("--accept-reissue", text)
            self.assertEqual(count(cur, "zz_s7_editions"), N)

    def test_a_reissue_with_another_key_set_is_partial_whatever_the_flags(
            self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(CODES[:4] + ["E09000001"], last=LATER_MS)
            rc, text, logged = self.run_main(
                cur, ["load", "--commit", "--acknowledge", P24,
                      "--accept-reissue", P24])
            self.assertEqual(rc, 1, text)
            self.assertIn(m.PARTIAL, text)
            self.assertIn("E09000033", text)
            self.assertEqual(editions(cur), [(1, None, N)])
            self.assertIn("PARTIAL RUN", logged.call_args[0][2])

    def test_a_failing_ledger_insert_rolls_back_and_logs_a_partial_run(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(CODES_NEW, V26)
            with mock.patch.object(pe, "record_file_check",
                                   side_effect=RuntimeError("boom")):
                rc, text, logged = self.run_main(cur, ["load", "--vintage",
                                                       V26, "--commit"])
            self.assertEqual(rc, 1, text)
            self.assertIn(f"{P26}: FAILED", text)
            self.assertEqual(editions(cur, P26), [])
            self.assertEqual(len(ledger(cur)), 1)
            logged.assert_called_once()
            notes = logged.call_args[0][2]
            self.assertIn("PARTIAL RUN", notes)
            self.assertIn(f"failed ['{P26}']", notes)

    def test_preview_writes_nothing_and_rerun_is_idempotent(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(CODES_NEW, V26)
            text, logged = self.ok(cur, ["load", "--vintage", V26])
            self.assertIn("would store edition 1 only", text)
            self.assertIn("PREVIEW: nothing written", text)
            self.assertEqual(editions(cur, P26), [])
            self.assertEqual(len(ledger(cur)), 1)
            logged.assert_not_called()
            text, logged = self.ok(cur, ["load", "--vintage", V26,
                                         "--simulate"])
            self.assertIn("SIMULATION", text)
            self.assertEqual(editions(cur, P26), [])
            logged.assert_not_called()
            self.ok(cur, ["load", "--vintage", V26, "--commit"])
            text, logged = self.ok(cur, ["load", "--vintage", V26,
                                         "--commit"])
            self.assertIn("nothing compared", text)
            logged.assert_not_called()
            self.assertEqual(editions(cur, P26), [(1, None, N)])
            self.assertEqual(len(ledger(cur)), 2)

    def test_file_checks_halt_and_store_nothing(self):
        cases = ((dict(exceeded=True), "exceededTransferLimit"),
                 (dict(codes=CODES[:-1]), f"{N - 1} English"),
                 (dict(overrides={("E06000002", "LONG"): None}), "LONG"),
                 (dict(crs="EPSG:27700"), "EPSG:27700"))
        for kw, needle in cases:
            with self.subTest(needle=needle), rolled_back(self.conn) as cur:
                self.seed(cur)
                self.release(last=LATER_MS, **kw)
                rc, text, logged = self.run_main(cur, ["load", "--commit"])
                self.assertEqual(rc, "halt", text)
                self.assertIn(needle, text)
                self.assertEqual(editions(cur), [(1, None, N)])
                self.assertEqual(len(ledger(cur)), 1)
                logged.assert_not_called()

    def test_a_layer_of_other_fields_or_another_vintage_halts(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            gj, _ = self.release(register=False)
            bad = self.root / "bad.json"
            from test_s7_boundaries_pure import layer_json, write_json
            write_json(bad, layer_json(drop=("LAT",)))
            rc, text, _ = self.run_main(cur, ["load", "--file", gj, "--layer",
                                              bad])
            self.assertEqual(rc, "halt", text)
            self.assertIn("LAT", text)
            l26 = write_layer(self.root / "l26.json", V26)
            rc, text, _ = self.run_main(cur, ["load", "--file", gj, "--layer",
                                              l26, "--vintage", V24])
            self.assertEqual(rc, "halt", text)
            self.assertIn("LAD_MAY_2026_UK_BGC", text)
            rc, text, _ = self.run_main(cur, ["load", "--file", gj])
            self.assertEqual(rc, "halt", text)
            self.assertIn("--layer", text)

    def test_discovery_fails_loudly(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["load"])
            self.assertIn(service_name(V24), text)
            self.assertIn("December_2024", text)
            self.assertIn("not declared", text)
            self.api[m.list_url()] = services_json(
                [("Local_Authority_Districts_May_2024_Boundaries_UK_BGC_V2",
                  "FeatureServer")])
            rc, text, _ = self.run_main(cur, ["load"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("_UK_BGC_V2", text)
            self.assertNotIn("nothing to do", text)

    def test_an_empty_live_table_is_never_rebuilt(self):
        with rolled_back(self.conn) as cur:
            self.release()
            rc, text, logged = self.run_main(cur, ["load", "--commit"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("never rebuilds", text)
            self.assertEqual(count(cur, "zz_s7_live"), 0)
            self.assertEqual(count(cur, "zz_s7_editions"), 0)
            logged.assert_not_called()

    def test_the_engine_insert_hook_refuses(self):
        with rolled_back(self.conn) as cur, self.assertRaises(SystemExit) as e:
            m.refuse_live_insert(cur, m.profile(), P24, [])
        self.assertIn("never inserts", str(e.exception.code))

    def test_a_redirect_records_the_final_url(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(CODES_NEW, V26)
            url = m.query_url(service_name(V26), "LAD26CD")
            self.final[url] = url + "&final=1"
            self.ok(cur, ["load", "--vintage", V26, "--commit"])
            src = ledger(cur)[-1][1]
            self.assertTrue(src.startswith(url + "&final=1 (source_date"))

    def test_restore_edition_round_trip(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            self.release(last=LATER_MS,
                         overrides={("E06000002", "LAT"): 52.35})
            self.ok(cur, ["load", "--commit"])
            self.ok(cur, ["refresh-latest", "--commit", "--acknowledge", P24])
            self.assertEqual(str(live_val(cur, "E06000002", "latitude")),
                             "52.350000")
            text, logged = self.ok(cur, ["restore-edition", P24, "1",
                                         "--commit"])
            self.assertIn("as edition 3", text)
            self.assertIn("--acknowledge", text)
            self.assertEqual(editions(cur)[-1], (3, 2, N))
            logged.assert_called_once()
            self.ok(cur, ["refresh-latest", "--commit", "--acknowledge", P24])
            self.assertEqual(str(live_val(cur, "E06000002", "latitude")),
                             "52.100000")
            rc, text, _ = self.run_main(cur, ["restore-edition", P24, "3"])
            self.assertEqual(rc, "halt")
            self.assertIn("is the tip", text)

    def test_the_real_spine_and_lookup_are_not_touched(self):
        with rolled_back(self.conn) as cur:
            b = real_md5(cur, "la_boundaries", "lad24cd")
            lk = real_md5(cur, "la_code_lookup", "old_code")
            self.seed(cur)
            self.release(last=LATER_MS, geoms={"E06000001": geometry(
                0, d=0.001)})
            self.ok(cur, ["load", "--commit", "--acknowledge", P24])
            self.ok(cur, ["refresh-latest", "--commit", "--acknowledge", P24])
            self.assertEqual(real_md5(cur, "la_boundaries", "lad24cd"), b)
            self.assertEqual(real_md5(cur, "la_code_lookup", "old_code"), lk)


class Commands(Fixture):

    def test_ddl_preview_then_commit_then_status(self):
        with rolled_back(self.conn, ddl=False) as cur:
            text, _ = self.ok(cur, ["ddl"])
            self.assertIn("DRY RUN", text)
            self.assertFalse(pe.table_exists(cur, "zz_s7_editions"))
            self.ok(cur, ["ddl", "--commit"])
            for t in ("zz_s7_editions", LEDGER):
                self.assertTrue(pe.table_exists(cur, t), t)
            cur.execute("SELECT COUNT(*) FROM pg_constraint WHERE conrelid = "
                        "'public.zz_s7_editions'::regclass AND contype = 'f'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_status_without_editions_table_is_clean_and_creates_nothing(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("not exist yet", text)
            self.assertIn("migrate-legacy --file GEOJSON --layer JSON", text)
            self.assertFalse(pe.table_exists(cur, "zz_s7_editions"))
            self.assertFalse(pe.table_exists(cur, LEDGER))

    def test_refresh_latest_without_tables_names_migrate_legacy(self):
        with rolled_back(self.conn, ddl=False) as cur:
            rc, text, _ = self.run_main(cur, ["refresh-latest"])
            self.assertEqual(rc, "halt", text)
            self.assertIn("migrate-legacy", text)

    def test_status_lines_after_migration(self):
        with rolled_back(self.conn) as cur:
            self.seed(cur)
            text, _ = self.ok(cur, ["status"])
            self.assertIn(f"ArcGIS lastEditDate {LAST_MS}", text)
            self.assertIn("; live; ledger latest: unchanged", text)

    def test_spec_types_match_the_live_table(self):
        with rolled_back(self.conn, ddl=False) as cur:
            cur.execute("SELECT column_name, data_type, numeric_precision, "
                        "numeric_scale FROM information_schema.columns WHERE "
                        "table_schema = 'public' AND table_name = "
                        "'la_boundaries'")
            live = {r[0]: r[1:] for r in cur.fetchall()}
            self.assertEqual(live["lad24nm"][0], "character varying")
            self.assertEqual(live["longitude"], ("numeric", 10, 6))
            self.assertEqual(live["latitude"], ("numeric", 10, 6))
            self.assertEqual(live["shape_area"], ("numeric", 20, 2))
            self.assertEqual(live["geojson"][0], "jsonb")
            self.assertEqual(live["source_date"][0], "date")
            self.assertNotIn("source", live)
            self.assertEqual(set(live) - {"lad24cd", "source_date",
                                          "loaded_at"}, set(m.VALUES))
            self.assertEqual(m.release_rank({"rank": RANK}), RANK)


if __name__ == "__main__":
    unittest.main()
