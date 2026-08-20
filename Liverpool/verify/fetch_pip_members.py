import os,json,urllib.request,urllib.parse,time
from dotenv import load_dotenv
load_dotenv(r"C:\Users\slewi\ucws-repo\.env")
K=os.getenv('StatXplore_API_Key')
def sx(path,tries=5):
    for i in range(tries):
        try:
            r=urllib.request.Request('https://stat-xplore.dwp.gov.uk/webapi/rest/v1/'+path,headers={'APIKey':K,'Accept':'application/json'})
            return json.load(urllib.request.urlopen(r,timeout=300))
        except Exception as e:
            print('  retry',i+1,type(e).__name__,str(e)[:60],flush=True)
            if i==tries-1: raise
            time.sleep(5*(i+1))
vs='str:valueset:PIP_Monthly_new:V_F_PIP_MONTHLY:COA_CODE:V_C_MASTERGEOG21_LA_TO_REGION'
d=sx('schema/'+urllib.parse.quote(vs,safe=':'))
kids=d.get('children',[])
json.dump([{'id':k['id'],'label':k.get('label')} for k in kids],open('src/pip_la_members.json','w'))
print('LA members:',len(kids),flush=True)
print('Liverpool:',[(k['id'],k.get('label')) for k in kids if 'Liverpool' in str(k.get('label'))],flush=True)
