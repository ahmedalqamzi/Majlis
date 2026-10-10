"""Usage: python3 tools/apply_pics.py <category_id> <map.json>
map.json: {"<question index>": {"pic": "Wikipedia title", "apic": "Wikipedia title"}, ...}
Either key may be omitted. Replaces any existing pic/apic on the listed questions only."""
import json,sys,re,os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)),'..'))
cid,mp=sys.argv[1],sys.argv[2]
d=json.load(open(f'q/{cid}.json'));qs=d['questions'];m=json.load(open(mp))
bad=[];n={'pic':0,'apic':0}
for k,v in m.items():
    i=int(k)
    if not 0<=i<len(qs): bad.append(f'index {k} out of range');continue
    for f in ('pic','apic'):
        t=v.get(f)
        if t is None: continue
        t=t.strip().replace('_',' ')
        if not t or re.search(r'https?:|/|#|\|',t) or len(t)>120: bad.append(f'{k}.{f} bad title {t!r}');continue
        qs[i][f]=t;n[f]+=1
if bad: print('PROBLEMS:',*bad[:20],sep='\n  ')
json.dump(d,open(f'q/{cid}.json','w'),ensure_ascii=False,indent=1)
print(cid,len(qs),'questions; set',n,'; now with pic',sum('pic' in q for q in qs),'apic',sum('apic' in q for q in qs))
