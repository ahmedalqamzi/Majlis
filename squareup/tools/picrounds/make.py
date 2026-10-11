# Builds the picture-round banks (q/pr_*.json) from the .txt lists in this folder.
# Each question shows the photo of its Wikipedia article (bundled at build time);
# build.py drops any question whose photo was not downloaded ("must": true).
import json, os
here = os.path.dirname(os.path.abspath(__file__))
ROUNDS = {
  "cars":    ("pr_cars",    "Car Spotter",       "Which brand makes this car?"),
  "places":  ("pr_places",  "Where Is This?",    "Where is this?"),
  "animals": ("pr_animals", "Name That Animal",  "What animal is this?"),
  "dishes":  ("pr_dishes",  "Name That Dish",    "What is this dish called?"),
  "players": ("pr_players", "Who's That Player?","Who is this footballer?"),
}
titles = []
for src, (cid, name, q) in ROUNDS.items():
    qs = []
    for line in open(os.path.join(here, src + ".txt"), encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"): continue
        art, ans, pts, extra = (line.split("|") + [""])[:4]
        x = {"q": q, "a": ans, "p": int(pts), "pic": art, "must": True}
        if src == "cars":
            x["a"] = f"{ans} ({extra})"; x["alt"] = [ans]
        elif src == "places":
            x["a"] = f"{ans} ({extra})"; x["alt"] = [ans, extra]
        elif extra:
            x["alt"] = [s for s in extra.split(",") if s]
        qs.append(x); titles.append(art)
    json.dump({"id": cid, "name": name, "questions": qs}, open(os.path.join(here, "..", "..", "q", cid + ".json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(cid, len(qs))
json.dump(sorted(set(titles)), open(os.path.join(here, "titles.json"), "w", encoding="utf-8"), ensure_ascii=False)
