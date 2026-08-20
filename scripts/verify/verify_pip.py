import os,json,urllib.request,urllib.parse,time,psycopg2,psycopg2.extras
from dotenv import load_dotenv
load_dotenv(r"C:\Users\slewi\ucws-repo\.env")
K=os.getenv('StatXplore_API_Key')
DB='str:database:PIP_Monthly_new'
CNT='str:count:PIP_Monthly_new:V_F_PIP_MONTHLY'
GEO='str:field:PIP_Monthly_new:V_F_PIP_MONTHLY:COA_CODE'
DATE='str:field:PIP_Monthly_new:F_PIP_DATE:DATE2'
VS='str:value:PIP_Monthly_new:V_F_PIP_MONTHLY:COA_CODE:V_C_MASTERGEOG21_LA_TO_REGION:%s'
MONTH='str:value:PIP_Monthly_new:F_PIP_DATE:DATE2:C_PIP_DATE:202604'

def post(payload,tries=5):
    for i in range(tries):
        try:
            r=urllib.request.Request('https://stat-xplore.dwp.gov.uk/webapi/rest/v1/table',
                data=json.dumps(payload).encode(),
                headers={'APIKey':K,'Content-Type':'application/json','Accept':'application/json'})
            return json.load(urllib.request.urlopen(r,timeout=300))
        except Exception as e:
            if i==tries-1: raise
            time.sleep(4*(i+1))

c=psycopg2.connect(host="localhost",port=os.getenv("PG_PORT"),dbname=os.getenv("PG_DATABASE"),user=os.getenv("PG_USER"),password=os.getenv("PG_PASSWORD"))
cur=c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT lad24cd FROM la_boundaries ORDER BY lad24cd")
codes=[r['lad24cd'] for r in cur.fetchall()]
print('LAs to query:',len(codes),flush=True)

res={}
B=25
for i in range(0,len(codes),B):
    batch=codes[i:i+B]
    payload={"database":DB,"measures":[CNT],
             "recodes":{GEO:{"map":[[VS%x] for x in batch],"total":False},
                        DATE:{"map":[[MONTH]],"total":False}},
             "dimensions":[[GEO],[DATE]]}
    try:
        d=post(payload)
        items=d['fields'][0]['items']
        vals=d['cubes'][CNT]['values']
        for j,it in enumerate(items):
            uri=it['uris'][0].split(':')[-1]
            res[uri]=vals[j][0]
        print('  batch %d-%d ok (%d)'%(i,i+len(batch),len(items)),flush=True)
    except Exception as e:
        print('  batch %d FAILED %s %s'%(i,type(e).__name__,str(e)[:100]),flush=True)
    time.sleep(1)

json.dump(res,open('src/pip_statxplore_202604.json','w'))
print('fetched',len(res),'LA values',flush=True)
cur.execute("SELECT lad24cd,pip_total_claimants FROM la_pip_claimants WHERE month='Apr-26'")
db={r['lad24cd']:r['pip_total_claimants'] for r in cur.fetchall()}
both=[k for k in res if k in db]
mism=[(k,res[k],db[k]) for k in both if int(res[k])!=db[k]]
print('compared:',len(both),'| MISMATCHES:',len(mism),flush=True)
for m in mism[:10]: print('   ',m,flush=True)
print('Stat-Xplore England sum:',sum(int(v) for k,v in res.items() if k.startswith('E0')),flush=True)
print('DB England sum:',sum(db.values()),flush=True)
print('Liverpool: statxplore=%s db=%s'%(res.get('E08000012'),db.get('E08000012')),flush=True)
c.close()
