"""Usage: python3 tools/apply_ar.py <category_id> <map.json>
map.json: {"<question index>": {"ar": "Arabic for the answer", "qar": [["English term exactly as in the question", "Arabic"], ...]}}
"ar" sets/replaces the answer's Arabic. "qar" replaces the question's term list. Use "ar": null or "qar": [] to remove."""
import json,sys,re,os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)),'..'))
cid,mp=sys.argv[1],sys.argv[2]
d=json.load(open(f'q/{cid}.json'));qs=d['questions'];m=json.load(open(mp))
AR=re.compile(r'[؀-ۿ]');bad=[];n={'ar':0,'qar':0}
for k,v in m.items():
    i=int(k)
    if not 0<=i<len(qs): bad.append(f'{k}: index out of range');continue
    q=qs[i]
    if 'ar' in v:
        a=v['ar']
        if a is None: q.pop('ar',None)
        elif not AR.search(a) or re.search(r'[A-Za-z]',a): bad.append(f'{k}: ar must be Arabic script only: {a!r}')
        else: q['ar']=a.strip();n['ar']+=1
    if 'qar' in v:
        ok=[]
        for pair in v['qar']:
            t,a=pair
            if t not in q['q']: bad.append(f'{k}: term {t!r} not found verbatim in question')
            elif not AR.search(a) or re.search(r'[A-Za-z]',a): bad.append(f'{k}: qar Arabic must be Arabic script only: {a!r}')
            else: ok.append([t,a.strip()])
        if ok: q['qar']=ok;n['qar']+=1
        else: q.pop('qar',None)
if bad: print('PROBLEMS:',*bad[:30],sep='\n  ')
json.dump(d,open(f'q/{cid}.json','w'),ensure_ascii=False,indent=1)
print(cid,len(qs),'questions; set',n,'; now ar',sum('ar' in q for q in qs),'qar',sum('qar' in q for q in qs))
