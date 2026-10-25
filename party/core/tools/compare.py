"""Semantic comparison of oracle (*.py.jsonl) and JSC (*.js.jsonl) traces.

Equality is semantic JSON equality with a short, declared nondeterminism
mapping; anything outside it is a mismatch and makes the exit status 1.

Declared mapping (and nothing else):
  N1  int vs float of equal value (Python clock floats such as 1000.0 vs the
      JS number 1000). bool is never equal to an int.
  N2  object key order is not compared. Order changes are counted and listed.
  N3  {"same": true} observation dedup is expanded per side before comparing.
  N4  json suite: entry order inside a tagged dict {"d": ...} when the only
      difference is JS ordering integer-like keys first. Values must match.
  N5  depth suite: CPython's RecursionError depth depends on the C stack; JS
      uses a fixed limit. Reported raw and kept red (exit status 1).
  N6  text suite: inputs marked jsUnrepresentable (a lone high surrogate then
      a lone low surrogate) have no JS string form. Reported, not compared.

Usage: python3 -I -B compare.py TRACE_DIR OUT_DIR [--self-test]
"""

import collections
import copy
import json
import os
import sys

LIMIT = 400  # raw mismatches written per suite (all are counted)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def diff(a, b, path, out, order, cap=20):
    """Append (path, py, js) for every difference; return True when equal."""
    if len(out) >= cap:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        if type(a) is not type(b) or a != b:
            out.append((path, a, b))
            return False
        return True
    if is_num(a) and is_num(b):
        if a != b and not (a != a and b != b):
            out.append((path, a, b))
            return False
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        ok = True
        if set(a) != set(b):
            out.append((path + ".{keys}", sorted(set(a) - set(b)), sorted(set(b) - set(a))))
            ok = False
        elif list(a) != list(b):
            order.append(path)
        for k in a:
            if k in b and not diff(a[k], b[k], path + "." + str(k), out, order, cap):
                ok = False
        return ok
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path + ".len", len(a), len(b)))
            return False
        ok = True
        for i, (x, y) in enumerate(zip(a, b)):
            if not diff(x, y, "%s[%d]" % (path, i), out, order, cap):
                ok = False
        return ok
    if type(a) is not type(b) or a != b:
        out.append((path, a, b))
        return False
    return True


def short(v, n=300):
    s = json.dumps(v, ensure_ascii=True)
    return s if len(s) <= n else s[:n] + "...(%d chars)" % len(s)


class Suite(object):
    def __init__(self, name, out_dir):
        self.name = name
        self.counts = collections.Counter()
        self.order_paths = collections.Counter()
        self.declared = collections.Counter()
        self.raw = []
        self.mismatches = 0
        self.out_dir = out_dir

    def check(self, where, py, js, extra=None):
        out, order = [], []
        ok = diff(py, js, "", out, order)
        for p in order:
            self.order_paths[p.split("[")[0]] += 1
        self.counts["compared"] += 1
        if ok:
            self.counts["equal"] += 1
            return True
        self.mismatches += 1
        if len(self.raw) < LIMIT:
            rec = {"where": where, "diffs": [[p, short(x), short(y)] for p, x, y in out]}
            if extra is not None:
                rec["context"] = extra
            self.raw.append(rec)
        return False

    def finish(self):
        path = os.path.join(self.out_dir, self.name + ".mismatches.jsonl")
        with open(path, "w", encoding="ascii") as handle:
            for rec in self.raw:
                handle.write(json.dumps(rec, ensure_ascii=True) + "\n")
        return {"suite": self.name, "counts": dict(self.counts), "mismatches": self.mismatches,
                "declared": dict(self.declared), "keyOrderOnly": dict(self.order_paths.most_common(12)),
                "rawFile": os.path.basename(path)}


def lines(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def paired(trace_dir, name):
    py = lines(os.path.join(trace_dir, name + ".py.jsonl"))
    js = lines(os.path.join(trace_dir, name + ".js.jsonl"))
    for a, b in zip_strict(py, js):
        yield a, b


def zip_strict(a, b):
    sentinel = object()
    while True:
        x, y = next(a, sentinel), next(b, sentinel)
        if x is sentinel and y is sentinel:
            return
        if x is sentinel or y is sentinel:
            raise SystemExit("line count differs")
        yield x, y


def role_of(ref):
    if ref is None:
        return "none"
    if "literal" in ref:
        return "literal"
    r = ref["ref"]
    if r.endswith(".host"):
        return "operator"
    if r.endswith(".display"):
        return "display"
    return "player"


def role_of_key(key):
    return "player" if key.startswith("player:") else key


# --------------------------------------------------------------- room suites

def compare_rooms(trace_dir, name, out_dir, mutate=None):
    s = Suite(name, out_dir)
    per_role = collections.defaultdict(collections.Counter)
    per_op = collections.defaultdict(collections.Counter)
    codes = collections.Counter()
    trace = None
    prev = {"py": {}, "js": {}}
    stream_role = {}
    idx = 0
    for py, js in paired(trace_dir, name):
        if "suite" in py:
            s.check("suite-header", True, "runnerError" not in js)
            continue
        if "trace" in py:
            trace, idx = py["trace"], 0
            prev = {"py": {}, "js": {}}
            stream_role = {}
            s.check("%s header" % trace, py["trace"], js.get("trace"))
            continue
        idx += 1
        op = py["op"]
        rec_py = py["py"]
        rec_js = js.get("js")
        if mutate:
            rec_js = mutate(trace, idx, op, rec_js)
        where = "%s #%d %s" % (trace, idx, op["op"])
        if rec_js is None:
            s.check(where, rec_py, js)
            continue
        kind = op["op"]
        if kind == "open" and rec_py["out"].get("opened"):
            stream_role[op["as"]] = role_of(op.get("token"))
        role = stream_role.get(op.get("stream")) if kind in ("poll", "close") else role_of(op.get("token"))
        out_ok = s.check(where + " out", rec_py["out"], rec_js["out"], {"op": op})
        o = rec_py["out"]
        if kind == "action":
            if "body" in o:
                body = o["body"]
                label = "accepted" if body.get("accepted") else body.get("error", {}).get("code", "?")
                if o.get("replayed"):
                    label = "replayed:" + label
                atype = json.loads(op["body"]["text"]).get("type") if "text" in op["body"] else None
                per_op[str(atype)][label] += 1
                codes[label.split(":")[-1]] += 1
            elif "error" in o:
                codes[o["error"]["code"]] += 1
                per_op["(envelope)"][o["error"]["code"]] += 1
        elif "error" in o:
            codes[o["error"]["code"]] += 1
        if kind == "state" or (kind == "poll" and o.get("event") == "state"):
            per_role[role or "?"]["projection-outputs" if out_ok else "projection-outputs-MISMATCH"] += 1
        # observations with N3 expansion
        ob_py, ob_js = rec_py["obs"], rec_js["obs"]
        full = {}
        for side, ob in (("py", ob_py), ("js", ob_js)):
            rooms = {}
            for ref, val in ob["rooms"].items():
                if val == {"same": True}:
                    if ref not in prev[side]:
                        rooms[ref] = {"unexpandable": True}
                    else:
                        rooms[ref] = prev[side][ref]
                    s.declared["N3 same-expanded:" + side] += 1
                else:
                    prev[side][ref] = val
                    rooms[ref] = val
            full[side] = dict(ob, rooms=rooms)
        changed = [ref for ref, val in ob_py["rooms"].items() if val != {"same": True}]
        top_py = {k: v for k, v in full["py"].items() if k != "rooms"}
        top_js = {k: v for k, v in full["js"].items() if k != "rooms"}
        s.check(where + " obs.manager", top_py, top_js)
        if set(full["py"]["rooms"]) != set(full["js"]["rooms"]):
            s.check(where + " obs.roomrefs", sorted(full["py"]["rooms"]), sorted(full["js"]["rooms"]))
        for ref in full["py"]["rooms"]:
            a, b = full["py"]["rooms"][ref], full["js"]["rooms"].get(ref)
            if b is None:
                continue
            if ref not in changed and a == b:
                continue  # identical to an already compared observation
            ap = dict(a)
            bp = dict(b)
            projs_a, projs_b = ap.pop("projections", []), bp.pop("projections", [])
            s.check(where + " obs.room[%s]" % ref, ap, bp)
            if len(projs_a) != len(projs_b):
                s.check(where + " obs.room[%s].projections" % ref, projs_a, projs_b)
                continue
            for (ka, pa), (kb, pb) in zip(projs_a, projs_b):
                r = role_of_key(ka)
                if s.check(where + " obs.room[%s].projection[%s]" % (ref, ka), [ka, pa], [kb, pb]):
                    per_role[r]["projections-equal"] += 1
                else:
                    per_role[r]["projections-MISMATCH"] += 1
    res = s.finish()
    res["perRole"] = {k: dict(v) for k, v in sorted(per_role.items())}
    res["actionOutcomes"] = {k: dict(v) for k, v in sorted(per_op.items())}
    res["errorCodes"] = dict(sorted(codes.items()))
    return res


# --------------------------------------------------------------- other suites

def compare_text(trace_dir, out_dir):
    s = Suite("text", out_dir)
    fields = collections.Counter()
    for py, js in paired(trace_dir, "text"):
        if "suite" in py:
            continue
        if py.get("jsUnrepresentable"):
            s.declared["N6 jsUnrepresentable"] += 1
            continue
        for k, v in py["py"].items():
            if s.check("text %s %s" % (short(py["in"], 80), k), v, js["js"].get(k)):
                fields[k] += 1
    res = s.finish()
    res["fieldsEqual"] = dict(fields)
    return res


def compare_sweep(trace_dir, out_dir):
    s = Suite("sweep", out_dir)
    for py, js in paired(trace_dir, "sweep"):
        if "suite" in py:
            continue
        a = {e["cp"]: e for e in py["py"]["entries"]}
        b = {e["cp"]: e for e in js["js"]["entries"]}
        s.counts["pyEntries"] = len(a)
        s.counts["jsEntries"] = len(b)
        for cp in sorted(set(a) | set(b)):
            s.check("U+%04X" % cp, a.get(cp), b.get(cp))
        s.check("cleanRejected ranges", py["py"]["cleanRejected"], js["js"]["cleanRejected"])
        s.counts["cleanRejectedRanges"] = len(py["py"]["cleanRejected"])
    return s.finish()


def int_like(k):
    return k.isdigit() and (k == "0" or not k.startswith("0"))


def normalize_tag_order(t):
    """N4: sort tagged dict entries only when JS's integer-key-first order explains the difference."""
    if isinstance(t, dict):
        if "d" in t:
            return {"d": sorted([[k, normalize_tag_order(v)] for k, v in t["d"]], key=lambda kv: kv[0])}
        if "l" in t:
            return {"l": [normalize_tag_order(v) for v in t["l"]]}
    return t


def js_key_order(keys):
    ints = sorted([k for k in keys if int_like(k) and int(k) < 2 ** 32 - 1], key=int)
    return ints + [k for k in keys if k not in ints]


def tag_orders_explained(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        if "d" in a and "d" in b:
            ka, kb = [k for k, _ in a["d"]], [k for k, _ in b["d"]]
            if ka != kb and js_key_order(ka) != kb:
                return False
            vb = dict(b["d"])
            return all(k in vb and tag_orders_explained(v, vb[k]) for k, v in a["d"])
        if "l" in a and "l" in b:
            return len(a["l"]) == len(b["l"]) and all(tag_orders_explained(x, y) for x, y in zip(a["l"], b["l"]))
    return True


def compare_json(trace_dir, out_dir):
    s = Suite("json", out_dir)
    for py, js in paired(trace_dir, "json"):
        if "suite" in py:
            continue
        a, b = py["py"], js["js"]
        if a != b and "value" in a and "value" in b and tag_orders_explained(a["value"], b["value"]):
            na, nb = normalize_tag_order(a["value"]), normalize_tag_order(b["value"])
            if diff(na, nb, "", [], []):
                s.declared["N4 integer-like key order"] += 1
                a, b = {"value": na}, {"value": nb}
        s.check("json " + py["b64"][:60], a, b)
        s.counts["error" if "error" in a else "value"] += 1
    return s.finish()


def compare_depth(trace_dir, out_dir):
    rows = []
    for py, js in paired(trace_dir, "depth"):
        if "suite" in py:
            continue
        rows.append([py["depth"], py["py"]["thread"], js["js"]["thread"]])
    differ = [r for r in rows if r[1] != r[2]]
    return {"suite": "depth", "declared": "N5 known difference, kept red", "rows": rows, "knownRed": len(differ),
            "differingDepths": [r[0] for r in differ]}


def compare_simple(trace_dir, name, out_dir, key):
    s = Suite(name, out_dir)
    for py, js in paired(trace_dir, name):
        if "suite" in py:
            continue
        s.check("%s %s" % (name, short({k: py[k] for k in key if k in py}, 120)), py["py"], js["js"])
    return s.finish()


# --------------------------------------------------------------- main

def main():
    trace_dir, out_dir = sys.argv[1], sys.argv[2]
    self_test = "--self-test" in sys.argv
    os.makedirs(out_dir, exist_ok=True)
    if self_test:
        return run_self_test(trace_dir, out_dir)
    results = [
        compare_rooms(trace_dir, "golden", out_dir),
        compare_rooms(trace_dir, "fuzz", out_dir),
        compare_text(trace_dir, out_dir),
        compare_sweep(trace_dir, out_dir),
        compare_json(trace_dir, out_dir),
        compare_simple(trace_dir, "content", out_dir, ["mode"]),
        compare_simple(trace_dir, "shuffle", out_dir, ["seed", "n"]),
        compare_simple(trace_dir, "errors", out_dir, []),
        compare_depth(trace_dir, out_dir),
    ]
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="ascii") as handle:
        json.dump(results, handle, ensure_ascii=True, indent=1)
    red = 0
    for r in results:
        if "mismatches" in r:
            red += r["mismatches"]
            print("%-8s compared=%-7d equal=%-7d MISMATCH=%d declared=%s" % (
                r["suite"], r["counts"].get("compared", 0), r["counts"].get("equal", 0), r["mismatches"],
                r["declared"]))
        else:
            print("%-8s %s: %d differing depths %s" % (r["suite"], r["declared"], r["knownRed"],
                                                       r["differingDepths"]))
    known = sum(r.get("knownRed", 0) for r in results)
    print("TOTAL undeclared mismatches: %d; known red differences: %d" % (red, known))
    return 1 if red or known else 0


def run_self_test(trace_dir, out_dir):
    """The comparator must go red on single-value changes in each area."""
    cases = {
        "flip revision": lambda t, i, op, r: _edit(r, i == 5, lambda r: r["obs"].update(entropyUsed=-1)),
        "flip out status": lambda t, i, op, r: _edit(r, i == 7, lambda r: r["out"].update(status=599)),
        "drop key": lambda t, i, op, r: _edit(r, i == 9, lambda r: r["out"].pop("status", None)),
        "bool for int": lambda t, i, op, r: _edit(r, i == 3 and "status" in r["out"],
                                                    lambda r: r["out"].update(status=True)),
    }
    ok = True
    for label, fn in cases.items():
        res = compare_rooms(trace_dir, "golden", out_dir, mutate=fn)
        red = res["mismatches"] > 0
        print("self-test %-16s -> %s (%d mismatches)" % (label, "RED as expected" if red else "NOT DETECTED",
                                                          res["mismatches"]))
        ok = ok and red
    out = []
    print("self-test %-16s -> %s" % ("1 vs 1.0", "equal (N1)" if diff(1, 1.0, "", out, []) else "red"))
    print("self-test %-16s -> %s" % ("True vs 1", "RED as expected" if not diff(True, 1, "", [], []) else "NOT DETECTED"))
    return 0 if ok else 1


def _edit(rec, cond, fn):
    if cond and rec is not None:
        rec = copy.deepcopy(rec)
        fn(rec)
    return rec


if __name__ == "__main__":
    sys.exit(main())
