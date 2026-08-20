"""S4 care leaver rebuild.

Implements the agreed decision:
  - semi_independent            : existing documented aggregate (semi-independent
                                  transitional + foyers + supported lodgings),
                                  retained unchanged for continuity
  - semi_independent_published  : DfE's published 'Semi-independent, transitional
                                  accommodation' category alone, so the figure
                                  quoted externally is reproducible from DfE
  - total_published             : DfE's own Total row, rather than the sum of
                                  buckets which counts suppressed cells as nought
  - suppressed_flag             : true where any contributing cell was suppressed

Also loads reporting year 2025 and the 24 county councils previously dropped by
the la_code_lookup inner join.
"""
import os
import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\slewi\ucws-repo\.env")
HERE = os.path.dirname(os.path.abspath(__file__))

SEMI = {'Semi-independent, transitional accommodation', 'Foyers', 'Supported lodgings'}
PUBLISHED = 'Semi-independent, transitional accommodation'
IND = {'Independent living'}
FAM = {'With parents or relatives', 'With former foster carers'}
COM = {'Community home'}
UNS = {'Bed and breakfast', 'Emergency accommodation', 'No fixed abode/homeless'}
NK = {'Residence not known', 'Total information not known', 'Local authority not in touch'}
SUPPRESSED = {'c', 'k', 'z', 'x'}


def cell(v):
    """Return (value, was_suppressed)."""
    s = str(v).strip()
    if s in SUPPRESSED:
        return None, True
    if s in ('', 'nan'):
        return None, False
    try:
        return int(float(s.replace(',', ''))), False
    except ValueError:
        return None, False


def build(path, accom_col, count_col):
    d = pd.read_csv(path, low_memory=False)
    d = d[d['geographic_level'] == 'Local authority']
    out = {}
    for _, r in d.iterrows():
        accom = str(r[accom_col])
        if accom == 'Total':
            continue
        key = (r['new_la_code'], int(r['time_period']))
        rec = out.setdefault(key, dict(semi=0, semi_pub=0, foyers=0, suplodg=0,
                                       ind=0, fam=0, com=0,
                                       uns=0, oth=0, nk=0, supp=False,
                                       name=r['la_name']))
        v, was_supp = cell(r[count_col])
        n = v or 0
        if accom in SEMI:
            rec['semi'] += n
            if was_supp:
                rec['supp'] = True
            if accom == PUBLISHED:
                rec['semi_pub'] += n
            elif accom == 'Foyers':
                rec['foyers'] += n
            elif accom == 'Supported lodgings':
                rec['suplodg'] += n
        elif accom in IND:
            rec['ind'] += n
        elif accom in FAM:
            rec['fam'] += n
        elif accom in COM:
            rec['com'] += n
        elif accom in UNS:
            rec['uns'] += n
        elif accom in NK:
            rec['nk'] += n
        else:
            rec['oth'] += n
    # published totals, read from DfE's own Total row
    for _, r in d.iterrows():
        if str(r[accom_col]) != 'Total':
            continue
        key = (r['new_la_code'], int(r['time_period']))
        if key in out:
            v, _ = cell(r[count_col])
            out[key]['total_pub'] = (out[key].get('total_pub') or 0) + (v or 0)
    for rec in out.values():
        rec['total'] = sum(rec[k] for k in ('semi', 'ind', 'fam', 'com', 'uns', 'oth', 'nk'))
        rec.setdefault('total_pub', None)
    return out


# 2019-2020 come only from the 2023 release; 2021-2025 from the current release,
# which carries DfE's revisions.
old = build(os.path.join(HERE, 'src/s4_node1_2023.csv'), 'accommodation_type', 'number')
new = build(os.path.join(HERE, 'src/cl_a504e4b8.csv'), 'breakdown', 'care_leaver_count')

merged = {}
for k, v in old.items():
    if k[1] <= 2020:
        merged[k] = v
merged.update(new)
print('rows built: %d (years %s)' % (len(merged), sorted({k[1] for k in merged})))

conn = psycopg2.connect(host="localhost", port=os.getenv("PG_PORT"),
                        dbname=os.getenv("PG_DATABASE"), user=os.getenv("PG_USER"),
                        password=os.getenv("PG_PASSWORD"))
cur = conn.cursor()
c2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("DROP TABLE IF EXISTS care_leaver_accommodation_bak_20260820")
cur.execute("CREATE TABLE care_leaver_accommodation_bak_20260820 AS "
            "SELECT * FROM care_leaver_accommodation")
for ddl in [
    "ALTER TABLE care_leaver_accommodation ADD COLUMN IF NOT EXISTS semi_independent_published INTEGER",
    "ALTER TABLE care_leaver_accommodation ADD COLUMN IF NOT EXISTS total_published INTEGER",
    "ALTER TABLE care_leaver_accommodation ADD COLUMN IF NOT EXISTS suppressed_flag BOOLEAN DEFAULT FALSE",
    "ALTER TABLE care_leaver_accommodation ADD COLUMN IF NOT EXISTS foyers INTEGER",
    "ALTER TABLE care_leaver_accommodation ADD COLUMN IF NOT EXISTS supported_lodgings INTEGER",
]:
    cur.execute(ddl)
conn.commit()

c2.execute("SELECT old_code, new_code FROM la_code_lookup")
recode = {r['old_code']: r['new_code'] for r in c2.fetchall()}

written = 0
counties = 0
for (code, yr), v in merged.items():
    # counties have no LAD24 successor; they are carried on their own E10 code
    lad = recode.get(code, code if str(code).startswith('E10') else None)
    if lad is None:
        continue
    if str(lad).startswith('E10'):
        counties += 1
    src = ('DfE Children Looked After SSDA903, reporting year %d, age group 17-21' % yr)
    cur.execute("""
        INSERT INTO care_leaver_accommodation
          (lad24cd, reporting_year, age_group, total_care_leavers, semi_independent,
           independent_living, with_family, community_home, unsuitable, other,
           not_known, semi_independent_published, total_published, suppressed_flag,
           foyers, supported_lodgings, uasc_impact_flag, source, loaded_at)
        VALUES (%s,%s,'17-21',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,now())
        ON CONFLICT (lad24cd, reporting_year, age_group) DO UPDATE SET
          total_care_leavers=EXCLUDED.total_care_leavers,
          semi_independent=EXCLUDED.semi_independent,
          independent_living=EXCLUDED.independent_living,
          with_family=EXCLUDED.with_family,
          community_home=EXCLUDED.community_home,
          unsuitable=EXCLUDED.unsuitable,
          other=EXCLUDED.other,
          not_known=EXCLUDED.not_known,
          semi_independent_published=EXCLUDED.semi_independent_published,
          total_published=EXCLUDED.total_published,
          suppressed_flag=EXCLUDED.suppressed_flag,
          foyers=EXCLUDED.foyers,
          supported_lodgings=EXCLUDED.supported_lodgings,
          source=EXCLUDED.source,
          loaded_at=now()
    """, (lad, yr, v['total'], v['semi'], v['ind'], v['fam'], v['com'], v['uns'],
          v['oth'], v['nk'], v['semi_pub'], v.get('total_pub'), v['supp'],
          v['foyers'], v['suplodg'], src))
    written += cur.rowcount
conn.commit()
print('rows written: %d (of which county rows: %d)' % (written, counties))

c2.execute("""SELECT reporting_year, COUNT(*) las,
              SUM(semi_independent) agg, SUM(semi_independent_published) pub,
              SUM(total_published) tot_pub
              FROM care_leaver_accommodation WHERE age_group='17-21'
              GROUP BY reporting_year ORDER BY reporting_year""")
print('\nyear  LAs   aggregate  published  published_total')
for r in c2.fetchall():
    print('%s  %4d  %9s  %9s  %s' % (r['reporting_year'], r['las'], r['agg'],
                                     r['pub'], r['tot_pub']))

c2.execute("""SELECT reporting_year, semi_independent, semi_independent_published,
              total_care_leavers, total_published, suppressed_flag
              FROM care_leaver_accommodation
              WHERE lad24cd='E08000012' AND age_group='17-21'
              ORDER BY reporting_year DESC LIMIT 3""")
print('\nLiverpool:')
for r in c2.fetchall():
    print('  ', dict(r))
conn.close()
