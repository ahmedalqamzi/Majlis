import json,glob,os,urllib.parse
os.chdir(os.path.dirname(os.path.abspath(__file__)))
chk={}
for f in sorted(glob.glob("video_check_*.json")): chk.update(json.load(open(f)))
bad={k for k,v in chk.items() if v.get("status")!="ok"}
bank={}
# Bundled photos: imported by tools/import_photos.py into the APK assets (the APK loads them as files);
# the web build gets one script pack per category in dist/photos/.
PHOTODIR=os.path.join('..','majlis-android','app','src','main','assets','photos')
PHOTOS=json.load(open(os.path.join(PHOTODIR,'index.json'),encoding='utf-8')) if os.path.exists(os.path.join(PHOTODIR,'index.json')) else {}
for f in sorted(glob.glob('q/*.json')):
    d=json.load(open(f));qs=[]
    for q in d['questions']:
        if q.get('yt') and (q['yt'] in bad or q['yt'] not in chk): continue
        q=dict(q)
        for k in ('pic','apic'):
            if q.get(k) in PHOTOS: q[k]='@'+d['id']+'/'+PHOTOS[q[k]]
        if q.get('must') and not str(q.get('pic','')).startswith('@'): continue
        qs.append({k:q[k] for k in ('q','a','p','ar','alt','yt','start','end','emoji','img','pic','apic','audio','qar') if k in q and q[k] not in (None,"",[])})
    bank[d['id']]={"id":d['id'],"name":d['name'],"questions":qs}
for f in glob.glob('qimg/*.json'):
    d=json.load(open(f))
    for q in d['questions']: bank[d['id']]['questions'].append({k:q[k] for k in ('q','a','p','ar','alt','svg') if k in q and q[k]})
# Inline picture files so index.html stays a single self-contained file (web and APK).
for c in bank.values():
    for q in c['questions']:
        if 'img' in q: q['img']='data:image/svg+xml;charset=utf-8,'+urllib.parse.quote(open(q['img']).read(),safe=" /:=,;")
out=[c for c in bank.values() if all(sum(q['p']==p for q in c['questions'])>=2 for p in (200,400,600))]
os.makedirs(os.path.join('dist','photos'),exist_ok=True)
import base64 as _b64
npk=0
for c in out:
    keys=sorted({q[k].split('/')[1] for q in c['questions'] for k in ('pic','apic') if str(q.get(k,'')).startswith('@')})
    if not keys: continue
    c['photos']=1;npk+=1
    pk={k:'data:image/webp;base64,'+_b64.b64encode(open(os.path.join(PHOTODIR,k+'.webp'),'rb').read()).decode() for k in keys}
    open(os.path.join('dist','photos',c['id']+'.js'),'w').write('MAJ_PHOTOS('+json.dumps(pk,separators=(',',':'))+')')
import base64
art={os.path.basename(f)[:-5]:'data:image/webp;base64,'+base64.b64encode(open(f,'rb').read()).decode() for f in sorted(glob.glob('art/*.webp'))}
# Category cover photos (tools/covers.py) ride along in ART as cover-<id>.
art.update({'cover-'+os.path.basename(f)[:-5]:'data:image/webp;base64,'+base64.b64encode(open(f,'rb').read()).decode() for f in sorted(glob.glob('covers/*.webp'))})
t=open('majlis2.template.html').read().replace("__ART__",json.dumps(art)).replace("__DATA__",json.dumps(out,ensure_ascii=False,separators=(',',':')))
open('index.html','w').write(t)
open(os.path.join('dist','index.html'),'w').write(t)
open(os.path.join('..','majlis-android','app','src','main','assets','index.html'),'w').write(t)
print(len(out),"cats",sum(len(b['questions']) for b in out),"q",len(PHOTOS),"photos",npk,"packs")
