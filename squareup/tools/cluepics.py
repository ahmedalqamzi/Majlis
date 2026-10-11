"""Give photo-less questions a clue photo that does not give the answer away.

Writes "cpic" into q/<id>.json: a bundled-photo title named in the question text
(e.g. "Which club beat Borussia Dortmund..." -> Borussia Dortmund), or a flag
image for "<country> flag" questions. A title is skipped when it shares a word
with the answer or its alternatives. Set "cpic": false on a question to opt out;
existing cpic values are kept. A photo is used at most 3 times per category. Run from squareup/: python3 tools/cluepics.py
"""
import json,glob,re,unicodedata,collections,os
P=json.load(open('../majlis-android/app/src/main/assets/photos/index.json'))
def norm(s):return ' '+' '.join(re.findall(r'[a-z0-9]+',unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode().lower()))+' '
def words(s):return {w for w in norm(s).split() if len(w)>2 and w not in {'the','and','for','with'}}
def base(t):return re.sub(r'\s*\(.*?\)','',t).strip()
files=sorted(glob.glob('q/*.json'));D={f:json.load(open(f,encoding='utf-8')) for f in files}
own=collections.defaultdict(set)
for f,d in D.items():
  for q in d['questions']:
    for k in ('pic','apic'):
      if q.get(k) in P:own[d['id']].add(q[k])
glob_t=[t for t in P if '(' not in t and len(t.split())>=2 and len(t)>=10]
# flags: country name/alias -> flag image from the Flags category
FL={}
for q in D.get('q/flags.json',{'questions':[]})['questions']:
  if q.get('img'):
    for n in [q['a']]+q.get('alt',[]):FL[norm(n)]=q['img']
FL[norm('UAE')]=FL.get(norm('United Arab Emirates'));FL[norm('Emirati')]=FL[norm('UAE')]
def flag(q):
  nq=norm(q['q'])
  if ' flag ' not in nq and ' flags ' not in nq:return None
  for n,img in sorted(FL.items(),key=lambda x:-len(x[0])):
    if img and (n.strip() and (f'{n.strip()} flag ' in nq or f'flag of {n.strip()} ' in nq or f'flag of the {n.strip()} ' in nq)):return img
added=0
for f,d in D.items():
  if d['id']=='flags':continue
  cand=sorted({t for t in own[d['id']] if len(base(t))>=4}|set(glob_t),key=lambda t:-len(norm(base(t))))
  cand=[(norm(base(t)),t,words(base(t))) for t in cand]
  ch=False;used=collections.Counter()
  for q in d['questions']:
    if 'cpic' in q or q.get('pic') or q.get('img') or q.get('svg') or q.get('emoji') or q.get('yt'):continue
    aw=words(q['a']+' '+' '.join(q.get('alt',[])));hit=flag(q)
    if hit and words('United Arab Emirates UAE')&aw and 'United Arab Emirates' in hit:hit=None
    if not hit:
      nq=norm(q['q'])
      for nt,t,tw in cand:
        if nt in nq and not tw&aw and used[t]<3:hit=t;break
    if hit:q['cpic']=hit;used[hit]+=1;added+=1;ch=True
  if ch:open(f,'w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=1)+'\n')
print('clue photos added',added)
