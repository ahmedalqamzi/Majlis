"""Negative controls for the L01B native host harnesses.

Each mutant copies party/core and party/native into a scratch directory outside the
checkout, applies one deliberate defect (exactly one textual replacement), builds it with
build.sh, and runs the harness that is supposed to catch it. A control passes only when the
harness exits non-zero AND every expected check turns red. The checkout is never modified.

  python negative_controls.py <repo root> <scratch dir> <evidence dir> <LAN IPv4> <harness python> [mutant ...]
"""
import json, os, shutil, subprocess, sys, time

REPO, SCRATCH, EVID, LAN, HPY = (os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]),
                                 sys.argv[4], os.path.abspath(sys.argv[5]))
ONLY = set(sys.argv[6:])

# name: (file under party/, old text, new text, harness, binary used as production, expected red check substrings)
MUTANTS = {
    "host-guard-off": ("native/Sources/Server.swift",
                       "guard hosts.count == 1, allowedHosts.contains(Server.strip(hosts[0]).lowercased()) else {",
                       "guard hosts.count >= 0 else {", "attack", "prod",
                       ["[host] missing Host", "[host] foreign Host (DNS rebinding)", "[host] two Host headers"]),
    # Layer check, not a negative control: with only the Swift route opened, the adapter's
    # requireTest() still refuses, so the harness must stay green.
    "production-room-route-swift-only": ("native/Sources/Server.swift",
                                         'if path == "/api/rooms" && surface == .testLoopback {', 'if path == "/api/rooms" {', "attack", "prod",
                                         "LAYERED"),
    "production-room-route-both-layers": ([("native/Sources/Server.swift", 'if path == "/api/rooms" && surface == .testLoopback {', 'if path == "/api/rooms" {'),
                                           ("native/majlis-native.js", 'function requireTest() { if (!isTest()) throw new PartyError("not_found"); }',
                                            "function requireTest() { }")], None, None, "attack", "prod",
                                          ["production LAN surface: POST /api/rooms 404"]),
    "bridge-token-leak": ("native/majlis-native.js",
                          "return line({ ok: true, op: op, code: created.code, revision: created.revision });",
                          "return line({ ok: true, op: op, code: created.code, revision: created.revision, hostToken: S.operatorTokens.get(created.code) });",
                          "attack", "prod", ["bridge create reply has no token field"]),
    "access-log-leak": ("native/Sources/Server.swift",
                        'log("access \\(c.method ?? "-") \\(c.route) \\(c.status.map { String($0) } ?? "-")")',
                        'log("access \\(c.method ?? "-") \\(c.req?.target ?? "-") \\(c.req?.first("authorization") ?? "") \\(c.status.map { String($0) } ?? "-")")',
                        "attack", "prod", ["production access log has no capability and no room code"]),
    "depth-guard-off": ("core/majlis-core.js", "var maxDepth = opts.maxDepth || 900;", "var maxDepth = opts.maxDepth || 1000000;",
                        "attack", "prod", ["depth: 900 inner arrays", "[body] JSON nesting 900 inner arrays"]),
    "room-action-auth-off": ("core/majlis-core.js", 'if (isRoomAction && role !== "operator") return [state, "forbidden"];', "",
                             "attack", "prod", ["[action] player sends room.start"]),
    "connection-cap-off": ("native/Sources/Server.swift", "if conns.count >= limits.maxConnections {", "if false {",
                           "attack", "prod", ["connection cap 40"]),
    "csprng-failure-ignored": ("native/Sources/Support.swift", "if failForTesting { return nil }",
                               "if failForTesting { return [UInt8](repeating: 0, count: n) }", "attack", "prod",
                               ["CSPRNG failure: bridge create", "CSPRNG failure leaves no partial state"]),
    "sse-write-timeout-off": ("native/Sources/Server.swift", "c.writeTimer = timer(limits.sseWriteTimeout) {",
                              "c.writeTimer = timer(3600) {", "attack", "prod", ["SSE backpressure"]),
    "feud-answers-to-players": ("core/majlis-core.js",
                                'if (get(viewer, "role") === "operator") {\n        view.boardId = b.id;',
                                'if (true) {\n        view.boardId = b.id;', "e2e", "prod",
                                ["[board 1 closed] Aisha: no hidden answer and no alias", "[board 1 closed] Omar: no hidden answer and no alias"]),
}


def main():
    os.makedirs(EVID, exist_ok=True)
    summary = []
    for name, (rel, old, new, harness, _, expect) in MUTANTS.items():
        edits = rel if isinstance(rel, list) else [(rel, old, new)]
        if ONLY and name not in ONLY:
            continue
        root = os.path.join(SCRATCH, name)
        shutil.rmtree(root, ignore_errors=True)
        for sub in ("core", "native"):
            shutil.copytree(os.path.join(REPO, "party", sub), os.path.join(root, "party", sub),
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        row = {"mutant": name, "files": ["party/" + e[0] for e in edits], "occurrences": []}
        for erel, eold, enew in edits:
            path = os.path.join(root, "party", erel)
            text = open(path, encoding="utf-8").read()
            row["occurrences"].append(text.count(eold))
            open(path, "w", encoding="utf-8").write(text.replace(eold, enew, 1))
        if row["occurrences"] != [1] * len(edits):
            row.update(control="INVALID", reason="anchor text must occur exactly once")
            summary.append(row)
            print(json.dumps(row), flush=True)
            continue
        build = os.path.join(root, "build")
        b = subprocess.run(["/bin/sh", os.path.join(root, "party", "native", "build.sh"), build], capture_output=True, text=True)
        row["build_exit"] = b.returncode
        if b.returncode != 0:
            row.update(control="INVALID", reason="mutant did not build", tail=(b.stderr or b.stdout)[-400:])
            summary.append(row)
            print(json.dumps(row), flush=True)
            continue
        out = os.path.join(EVID, name)
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        prod, test = os.path.join(build, "majlis-party-native"), os.path.join(build, "majlis-party-native-test")
        t0 = time.time()
        if harness == "attack":
            argv = [HPY, "-I", "-B", os.path.join(REPO, "party/native/harness/attack_suite.py"), REPO, prod, test, out, LAN]
        else:
            argv = [HPY, "-I", "-B", os.path.join(REPO, "party/native/harness/production_lan_e2e.py"), REPO, prod, out, LAN]
        with open(os.path.join(out, "harness.log"), "w") as log:
            r = subprocess.run(argv, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        lines = open(os.path.join(out, "harness.log"), encoding="utf-8", errors="replace").read().splitlines()
        fails = [ln[5:] for ln in lines if ln.startswith("FAIL ")]
        if expect == "LAYERED":
            row.update(harness=harness, harness_exit=r.returncode, secs=round(time.time() - t0, 1), expected_red={},
                       red_checks=len(fails), red_sample=[f[:160] for f in fails[:12]])
            row["control"] = "PASS" if r.returncode == 0 else "FAIL"
            row["note"] = "layer check: one guard removed, the remaining guard still refuses; harness stays green"
        else:
            hit = {e: any(e in f for f in fails) for e in expect}
            row.update(harness=harness, harness_exit=r.returncode, secs=round(time.time() - t0, 1), expected_red=hit,
                       red_checks=len(fails), red_sample=[f[:160] for f in fails[:12]])
            row["control"] = "PASS" if r.returncode != 0 and all(hit.values()) else "FAIL"
        summary.append(row)
        print(json.dumps({k: row[k] for k in ("mutant", "control", "harness_exit", "expected_red", "red_checks", "secs")}), flush=True)
    ok = all(r.get("control") == "PASS" for r in summary)
    json.dump({"label": "Mutant builds live only under the scratch directory; the checkout is not modified.",
               "controls": summary, "all_pass": ok}, open(os.path.join(EVID, "negative-controls.json"), "w"), indent=1)
    print(json.dumps({"controls": len(summary), "pass": sum(r.get("control") == "PASS" for r in summary)}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
