"""Check the reconstructed host-ICU drift set (icu-drift.json) against the Python oracle."""
import json, sys, unicodedata
sys.path.insert(0, sys.argv[1])
from party.server import textutil as TU
rows = [r for r in json.load(open(sys.argv[2])) if r["hostNFKC"] != r["coreNFKC"]]
cps = lambda s: [ord(c) for c in s]
bad, kept, assigned13 = [], 0, 0
for r in rows:
    ch = chr(r["cp"])
    if unicodedata.category(ch) != "Cn":
        assigned13 += 1
    py = {"nfkc": cps(unicodedata.normalize("NFKC", ch)), "matchKey": cps(TU.match_key(ch)),
          "clean24": None if TU.clean_text(ch, 24) is None else "kept"}
    js = {"nfkc": r["coreNFKC"], "matchKey": r["coreMatchKey"], "clean24": r["coreClean24"]}
    if py != js:
        bad.append({"cp": r["cp"], "py": py, "js": js})
    if js["clean24"] == "kept":
        kept += 1
out = {"driftSet": len(rows), "coreVsPythonMismatches": len(bad), "assignedInUnicode13": assigned13,
       "acceptedByCleanText": kept, "hostDiffersFromPython": sum(1 for r in rows if r["hostNFKC"] != cps(unicodedata.normalize("NFKC", chr(r["cp"])))),
       "cpRange": ["U+%04X" % rows[0]["cp"], "U+%04X" % rows[-1]["cp"]] if rows else [],
       "codePoints": ["U+%04X" % r["cp"] for r in rows], "mismatches": bad}
json.dump(out, open(sys.argv[3], "w"), indent=1)
print({k: v for k, v in out.items() if k not in ("codePoints", "mismatches")})
for s in ["U+A7F1", "U+A7F2", "U+A7F3"]:
    print(s, s in out["codePoints"])
