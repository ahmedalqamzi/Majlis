"""No plaintext capability in exported evidence.

* every *Token field is {"sha256": <64 hex>} (or null);
* op.token is a harness reference {"ref": name} or a forged literal {"literal": s};
  no forged literal hashes to any issued token;
* no other string anywhere has the 43-char token_urlsafe(32) shape.
"""
import collections, hashlib, json, os, re, sys
HEX = re.compile(r"^[0-9a-f]{64}$")
URLSAFE43 = re.compile(r"^[A-Za-z0-9_-]{43}$")
bad, issued, literals, n = [], set(), collections.Counter(), collections.Counter()

def walk(v, path, f):
    if isinstance(v, dict):
        for k, x in v.items():
            if path.endswith(".op") and k == "token" and isinstance(x, dict) and list(x) in (["ref"], ["literal"]):
                n["op.token " + list(x)[0]] += 1
                if "literal" in x and isinstance(x["literal"], str):
                    literals[x["literal"]] += 1
                continue
            if k.lower().endswith("token") and x is not None:
                n["*Token fields"] += 1
                if isinstance(x, dict) and list(x) == ["sha256"] and HEX.match(x["sha256"]):
                    issued.add(x["sha256"])
                else:
                    bad.append((f, path + "." + k, type(x).__name__))
                continue
            walk(x, path + "." + k, f)
    elif isinstance(v, list):
        for x in v:
            walk(x, path, f)
    elif isinstance(v, str) and URLSAFE43.match(v):
        bad.append((f, path, "43-char token_urlsafe-shaped string"))

for root in sys.argv[1:]:
    for dp, _, fs in os.walk(root):
        for fn in sorted(fs):
            if fn.endswith((".jsonl", ".json")):
                with open(os.path.join(dp, fn), encoding="utf-8") as h:
                    for line in (h if fn.endswith(".jsonl") else [h.read()]):
                        if line.strip():
                            walk(json.loads(line), "", fn)
collide = [l[:4] + "..." for l in literals if hashlib.sha256(l.encode("utf-8", "surrogatepass")).hexdigest() in issued]
print("counts:", dict(n), "distinct issued token hashes:", len(issued))
print("forged literals (distinct):", len(literals), "hashing to an issued token:", len(collide))
print("violations:", len(bad), bad[:5])
sys.exit(1 if bad or collide else 0)
