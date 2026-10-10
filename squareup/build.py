import json,glob,os,urllib.parse
os.chdir(os.path.dirname(os.path.abspath(__file__)))
chk={}
for f in ("video_check_a.json","video_check_b.json"): chk.update(json.load(open(f)))
bad={k for k,v in chk.items() if v.get("status")!="ok"}
bank={}
for f in sorted(glob.glob('q/*.json')):
    d=json.load(open(f));qs=[]
    for q in d['questions']:
        if q.get('yt') and (q['yt'] in bad or q['yt'] not in chk): continue
        qs.append({k:q[k] for k in ('q','a','p','ar','alt','yt','start','end','emoji','img') if k in q and q[k] not in (None,"",[])})
    bank[d['id']]={"id":d['id'],"name":d['name'],"questions":qs}
for f in glob.glob('qimg/*.json'):
    d=json.load(open(f))
    for q in d['questions']: bank[d['id']]['questions'].append({k:q[k] for k in ('q','a','p','ar','alt','svg') if k in q and q[k]})
# Inline picture files so index.html stays a single self-contained file (web and APK).
for c in bank.values():
    for q in c['questions']:
        if 'img' in q: q['img']='data:image/svg+xml;charset=utf-8,'+urllib.parse.quote(open(q['img']).read(),safe=" /:=,;")
out=list(bank.values())
t=open('majlis2.template.html').read().replace("__DATA__",json.dumps(out,ensure_ascii=False,separators=(',',':')))
open('index.html','w').write(t)
open(os.path.join('..','majlis-android','app','src','main','assets','index.html'),'w').write(t)
print(len(out),"cats",sum(len(b['questions']) for b in out),"q")
