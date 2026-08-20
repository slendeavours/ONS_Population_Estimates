import os,json,urllib.request,urllib.parse,time,psycopg2,psycopg2.extras
from dotenv import load_dotenv
load_dotenv(r"C:\Users\slewi\ucws-repo\.env")
K=os.getenv('StatXplore_API_Key')
DB='str:database:hb_new'; CNT='str:count:hb_new:V_F_HB_NEW'
GEO='str:field:hb_new:V_F_HB_NEW:ADMIN_LA_CODE'
SATA='str:field:hb_new:V_F_HB_NEW:SATA'
DATE='str:field:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME'
VS='str:value:hb_new:V_F_HB_NEW:ADMIN_LA_CODE:V_C_ADMIN_LA:%s'
SATA1='str:value:hb_new:V_F_HB_NEW:SATA:C_SATA:1'
def post(p,t=5):
    for i in range(t):
        try:
            r=urllib.request.Request('https://stat-xplore.dwp.gov.uk/webapi/rest/v1/table',data=json.dumps(p).encode(),
              headers={'APIKey':K,'Content-Type':'application/json','Accept':'application/json'})
            return json.load(urllib.request.urlopen(r,timeout=300))
        except Exception as e:
            if i==t-1: raise
            time.sleep(4*(i+1))
c=psycopg2.connect(host="localhost",port=os.getenv("PG_PORT"),dbname=os.getenv("PG_DATABASE"),user=os.getenv("PG_USER"),password=os.getenv("PG_PASSWORD"))
cur=c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT lad24cd FROM la_boundaries ORDER BY lad24cd"); codes=[r['lad24cd'] for r in cur.fetchall()]

def run(month,label):
    res={}
    MON='str:value:hb_new:F_HB_NEW_DATE:NEW_DATE_NAME:C_HB_NEW_DATE:%s'%month
    B=25
    for i in range(0,len(codes),B):
        b=codes[i:i+B]
        p={"database":DB,"measures":[CNT],
           "recodes":{GEO:{"map":[[VS%x] for x in b],"total":False},
                      SATA:{"map":[[SATA1]],"total":False},
                      DATE:{"map":[[MON]],"total":False}},
           "dimensions":[[GEO],[SATA],[DATE]]}
        try:
            d=post(p); items=d['fields'][0]['items']; vals=d['cubes'][CNT]['values']
            for j,it in enumerate(items): res[it['uris'][0].split(':')[-1]]=vals[j][0][0]
        except Exception as e: print('  batch',i,'FAIL',type(e).__name__,str(e)[:90],flush=True)
        time.sleep(1)
    print('%s: fetched %d LAs, England sum %s'%(label,len(res),sum(int(v) for k,v in res.items() if k.startswith('E0'))),flush=True)
    return res

r_feb=run('202602','Feb-26 (source 8b)')
json.dump(r_feb,open('src/hb_sata1_202602.json','w'))
cur.execute("SELECT lad24cd,claimants FROM la_hb_accom_type_caseload WHERE month='202602' AND accom_type='SA'")
db8b={r['lad24cd']:r['claimants'] for r in cur.fetchall()}
mm=[(k,int(r_feb[k]),db8b[k]) for k in r_feb if k in db8b and int(r_feb[k])!=db8b[k]]
print('SOURCE 8b compared:',len([k for k in r_feb if k in db8b]),'| MISMATCHES:',len(mm),flush=True)
for m in mm[:8]: print('   ',m,flush=True)
print('Liverpool 8b: statxplore=%s db=%s'%(r_feb.get('E08000012'),db8b.get('E08000012')),flush=True)

r_nov=run('202511','Nov-25 (source 8)')
json.dump(r_nov,open('src/hb_sata1_202511.json','w'))
cur.execute("SELECT lad24cd,hb_sa_claimants FROM la_hb_sa_caseload WHERE month='202511'")
db8={r['lad24cd']:r['hb_sa_claimants'] for r in cur.fetchall()}
mm2=[(k,int(r_nov[k]),db8[k]) for k in r_nov if k in db8 and int(r_nov[k])!=db8[k]]
print('SOURCE 8 compared:',len([k for k in r_nov if k in db8]),'| MISMATCHES:',len(mm2),flush=True)
for m in mm2[:8]: print('   ',m,flush=True)
print('Liverpool 8: statxplore=%s db=%s'%(r_nov.get('E08000012'),db8.get('E08000012')),flush=True)
c.close()
