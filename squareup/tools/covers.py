"""Pick one cover photo per category from its bundled photos and write small webp covers to covers/<id>.webp.
Prefers landscape, colourful, well-exposed shots of 200/400-point questions. OVERRIDE pins a specific Wikipedia title."""
import json, glob, os, io
from PIL import Image, ImageStat, ImageOps
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PH = os.path.join(ROOT, '..', 'majlis-android', 'app', 'src', 'main', 'assets', 'photos')
OUT = os.path.join(ROOT, 'covers'); os.makedirs(OUT, exist_ok=True)
idx = json.load(open(os.path.join(PH, 'index.json'), encoding='utf-8'))
OVERRIDE = json.load(open(os.path.join(ROOT, 'tools', 'cover_override.json'), encoding='utf-8')) if os.path.exists(os.path.join(ROOT, 'tools', 'cover_override.json')) else {}
def score(k):
    im = Image.open(os.path.join(PH, k + '.webp')).convert('RGB'); w, h = im.size; r = w / h
    if r < 1.15 or r > 2.4 or w < 240: return -1, im
    hsv = im.convert('HSV'); s = ImageStat.Stat(hsv).mean[1]; st = ImageStat.Stat(im.convert('L'))
    v, sd = st.mean[0], st.stddev[0]
    if v < 55 or v > 215: return -1, im
    small = im.resize((96, 60)); nc = len(small.getcolors(96 * 60) or [])
    if nc < 2600 or s > 170: return -1, im  # flags, logos and title cards have few distinct colours
    return small.convert('L').entropy() * 20 + sd + min(s, 120) * .3, im
n = 0; used = set()
for f in sorted(glob.glob(os.path.join(ROOT, 'q', '*.json'))):
    d = json.load(open(f, encoding='utf-8')); cid = d['id']
    titles = []
    for q in d['questions']:
        for k in ('pic', 'apic'):
            t = q.get(k)
            if t in idx and t not in titles: titles.append(t)
    best = None
    if cid in OVERRIDE and OVERRIDE[cid] in idx:
        best = (1e9, Image.open(os.path.join(PH, idx[OVERRIDE[cid]] + '.webp')).convert('RGB'))
    elif False:
        for t in titles[:120]:
            sc, im = score(idx[t])
            if sc > 0 and idx[t] not in used and (best is None or sc > best[0]): best = (sc, im); bk = idx[t]
        if best: used.add(bk)
    p = os.path.join(OUT, cid + '.webp')
    if not best:
        if os.path.exists(p): os.remove(p)
        continue
    im = ImageOps.fit(best[1], (480, 300), Image.LANCZOS, centering=(0.5, 0.3))
    im.save(p, 'WEBP', quality=58, method=6); n += 1
print(n, 'covers')
