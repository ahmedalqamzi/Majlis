"""Import downloaded photos (map.json + images/ from fetch.ps1, as a folder or zip) into the APK assets.
Each photo becomes majlis-android/app/src/main/assets/photos/<key>.webp (max 360px, keyed by content hash)
and photos/index.json maps Wikipedia title -> key. build.py reads that index.
Usage: python3 -I tools/import_photos.py <folder-or-zip> [more ...]"""
import sys, os, json, zipfile, hashlib, io
from PIL import Image, ImageOps
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, '..', 'majlis-android', 'app', 'src', 'main', 'assets', 'photos')
os.makedirs(OUT, exist_ok=True)
IDX = os.path.join(OUT, 'index.json')
index = json.load(open(IDX, encoding='utf-8')) if os.path.exists(IDX) else {}
def entries(src):
    if zipfile.is_zipfile(src):
        z = zipfile.ZipFile(src); names = {n.replace('\\', '/'): n for n in z.namelist()}
        mp = json.loads(z.read(names['map.json']).decode('utf-8-sig'))
        for t, f in mp.items():
            n = names.get('images/' + f)
            if n: yield t, z.read(n)
    else:
        mp = json.load(open(os.path.join(src, 'map.json'), encoding='utf-8-sig'))
        for t, f in mp.items():
            p = os.path.join(src, 'images', f)
            if os.path.exists(p): yield t, open(p, 'rb').read()
added = bad = 0
for src in sys.argv[1:]:
    for t, raw in entries(src):
        try:
            im = Image.open(io.BytesIO(raw)); im = ImageOps.exif_transpose(im)
            if getattr(im, 'n_frames', 1) > 1: im.seek(0)
            im = im.convert('RGBA' if im.mode in ('RGBA', 'LA', 'P') else 'RGB')
            if min(im.size) < 80: bad += 1; continue
            im.thumbnail((360, 360))
            b = io.BytesIO(); im.save(b, 'WEBP', quality=62, method=6); data = b.getvalue()
        except Exception as e:
            bad += 1; continue
        k = hashlib.sha1(data).hexdigest()[:12]
        p = os.path.join(OUT, k + '.webp')
        if not os.path.exists(p): open(p, 'wb').write(data)
        index[t] = k; added += 1
json.dump(dict(sorted(index.items())), open(IDX, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
print('imported', added, 'skipped', bad, 'index now', len(index), 'files', len(os.listdir(OUT)) - 1)
