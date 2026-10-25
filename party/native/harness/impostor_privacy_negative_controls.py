"""Planted-secret negative controls for Impostor privacy, run against the
REAL compiled native host over HTTP, across every pre-reveal phase and all
three viewer roles (operator, display, impostor) -- not the Python-oracle,
single-phase, project()-monkeypatch coverage that already exists in
party/core/tools/impostor_privacy_mutation_test.py (kept, unchanged, still
exercising a different and legitimate angle: direct oracle project() calls).

Each mutant copies party/core + party/native into a scratch directory
(never touches the checkout), applies exactly one deliberate textual
mutation to party/core/majlis-core.js, builds it into a real native binary
with build.sh, launches that binary as the actual HTTP server, drives a
3-player Impostor room through discussion -> vote -> guess_word with the
real HTTP API (tests/support.Api), and at each phase runs the exact
whole-payload scanner already accepted for this mode
(tests/impostor_privacy.pre_reveal_leaks / text_leaks, the same functions
test_impostor_http.py's check_secrecy uses) against every viewer's real
HTTP response body.

A control passes only when:
  - the unmutated baseline is clean (scanner green) at every phase, every
    role, AND
  - the mutant is caught (scanner red) at every pre-reveal phase, for the
    role(s) the mutation targets.

Two mutants:
  shared-reveal-leak -- Impostor.project()'s `reveal` field
    (party/core/majlis-core.js) is set once on the single `view` object
    returned to EVERY role (operator, display, crew, impostor alike -- read
    in full: there is no per-role branch for it at all, unlike the
    `view.you` branch below). Unconditionally including it therefore leaks
    to all four viewer labels identically; the baseline run of this harness
    confirmed that empirically (the first version of this mutant, scoped to
    only operator+display, was caught as a control failure because the
    mutant also flagged crew/impostor -- proving the architecture is a
    single universal gate, not two role-specific ones).
  impostor-own-word-leak -- Impostor.project()'s player branch is mutated so
    the impostor's own `view.you` carries the secret word; this IS a
    separate, role-specific code path (only reached when role === "player"),
    so it is a distinct mutant that targets only the impostor viewer.

Usage: python3 impostor_privacy_negative_controls.py REPO_ROOT SCRATCH_DIR EVIDENCE_DIR
Exit 0 only if every mutant is caught at every pre-reveal phase/role and the
baseline stays clean throughout.
"""
import json, os, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_FOR_IMPORT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_FOR_IMPORT, "tests"))

MUTANTS = {
    "shared-reveal-leak": (
        "core/majlis-core.js",
        'reveal: revealed ? { word: state.item.word, impostor: state.impostor } : null',
        'reveal: { word: state.item.word, impostor: state.impostor }',
        ["operator", "display", "crew", "impostor"],
    ),
    "impostor-own-word-leak": (
        "core/majlis-core.js",
        'view.you = (me === state.impostor) ? { role: "impostor" } : { role: "crew", word: state.item.word };',
        'view.you = (me === state.impostor) ? { role: "impostor", word: state.item.word } : { role: "crew", word: state.item.word };',
        ["impostor"],
    ),
}

PHASES = ("discussion", "vote", "guess_word")


def build_binary(repo, scratch, mutant_rel=None, old=None, new=None):
    root = os.path.join(scratch, mutant_rel and mutant_rel.replace("/", "_") or "baseline")
    shutil.rmtree(root, ignore_errors=True)
    for sub in ("core", "native"):
        shutil.copytree(os.path.join(repo, "party", sub), os.path.join(root, "party", sub),
                         ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if mutant_rel:
        path = os.path.join(root, "party", mutant_rel)
        text = open(path, encoding="utf-8").read()
        count = text.count(old)
        if count != 1:
            raise AssertionError("anchor text occurs %d times (expected 1) in %s" % (count, path))
        open(path, "w", encoding="utf-8").write(text.replace(old, new, 1))
    build_out = os.path.join(root, "build")
    subprocess.run(["sh", os.path.join(root, "party", "native", "build.sh"), build_out], check=True,
                    cwd=root, env=dict(os.environ))
    return os.path.join(build_out, "majlis-party-native-test")


def start_native(binary, content_dir):
    cmd = [binary, "--host", "127.0.0.1", "--port", "0", "--content-dir", content_dir,
           "--exit-on-stdin-eof", "--heartbeat", "0.2", "--sweep-interval", "0.1",
           "--request-rate", "2000", "--request-burst", "5000", "--create-per-minute", "6000",
           "--create-burst", "500", "--join-per-minute", "6000", "--join-burst", "500",
           "--action-rate", "2000", "--action-burst", "5000"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = proc.stdout.readline().decode("utf-8")
    banner = json.loads(line)
    return proc, banner["port"]


def stop_native(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def drive_and_scan(port, content_dir):
    """Create a 3-player Impostor room, advance through every pre-reveal
    phase, and scan every viewer's real HTTP response body at each phase.
    Returns {phase: {role_or_pid_label: [finding, ...]}}."""
    import support  # tests/support.py
    from impostor_privacy import pre_reveal_leaks, text_leaks

    api = support.Api(port)
    with open(os.path.join(content_dir, "impostor.json"), encoding="utf-8") as f:
        items = json.load(f)["items"]

    r = api.create_room()
    code, host, display = r.json["code"], r.json["hostToken"], r.json["displayToken"]
    players = []
    for nm in ("Aisha", "Omar", "Noora"):
        jr = api.join(code, nm)
        players.append({"id": jr.json["playerId"], "token": jr.json["playerToken"]})
    api.action(code, host, "room.set_mode", payload={"mode": "impostor"})
    api.action(code, host, "room.start")

    def snap(token):
        return api.state(code, token)

    def learn():
        snaps = {p["id"]: snap(p["token"]).json for p in players}
        imp = [pid for pid, s in snaps.items() if s["game"]["you"]["role"] == "impostor"][0]
        word = [s["game"]["you"]["word"] for pid, s in snaps.items() if pid != imp][0]
        rid = snaps[imp]["roundId"]
        return rid, imp, word

    rid, imp, word = learn()
    crew = [p["id"] for p in players if p["id"] != imp]
    tok = {p["id"]: p["token"] for p in players}

    results = {}

    def scan_phase(label, expect_phase):
        findings = {}
        viewers = [("operator", host, None), ("display", display, None)]
        for p in players:
            viewers.append(("impostor" if p["id"] == imp else "crew", p["token"], p["id"]))
        for role, token, pid in viewers:
            resp = snap(token)
            body, data = resp.text, resp.json
            assert data["game"]["roundPhase"] == expect_phase, (label, role, data["game"]["roundPhase"])
            own = word if role == "crew" else None
            findings[role + (":" + pid if pid else "")] = {
                "pre_reveal_leaks": pre_reveal_leaks(data, items, role, own),
                "text_leaks": text_leaks(body, items, own),
            }
        results[label] = findings

    scan_phase("discussion", "discussion")
    api.action(code, host, "impostor.advance", round_id=rid)
    scan_phase("vote", "vote")
    api.action(code, tok[crew[0]], "impostor.vote", round_id=rid, payload={"playerId": imp})
    api.action(code, tok[crew[1]], "impostor.vote", round_id=rid, payload={"playerId": imp})
    api.action(code, tok[imp], "impostor.vote", round_id=rid, payload={"playerId": crew[0]})
    scan_phase("guess_word", "guess_word")
    return results


def summarize(results, targeted_roles):
    """True if every targeted role has >=1 finding at every pre-reveal phase,
    and every non-targeted role/phase has zero findings."""
    ok = True
    detail = {}
    for phase in PHASES:
        for key, v in results.get(phase, {}).items():
            role = key.split(":")[0]
            flagged = bool(v["pre_reveal_leaks"]) or bool(v["text_leaks"])
            detail[(phase, key)] = flagged
            if role in targeted_roles:
                ok = ok and flagged
            else:
                ok = ok and not flagged
    return ok, detail


def main():
    repo, scratch, evid = (os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]))
    content_dir = os.path.join(repo, "tests", "fixtures", "content")
    os.makedirs(evid, exist_ok=True)
    report = {"baseline": None, "mutants": {}}
    all_ok = True

    binary = build_binary(repo, scratch)
    proc, port = start_native(binary, content_dir)
    try:
        baseline = drive_and_scan(port, content_dir)
    finally:
        stop_native(proc)
    base_ok, base_detail = summarize(baseline, targeted_roles=set())
    report["baseline"] = {"results": baseline, "clean": base_ok}
    all_ok = all_ok and base_ok
    print(json.dumps({"baseline_clean": base_ok}))

    for name, (rel, old, new, targeted) in MUTANTS.items():
        binary = build_binary(repo, scratch, rel, old, new)
        proc, port = start_native(binary, content_dir)
        try:
            results = drive_and_scan(port, content_dir)
        finally:
            stop_native(proc)
        caught, detail = summarize(results, targeted_roles=set(targeted))
        report["mutants"][name] = {"results": results, "control_passed": caught, "targeted_roles": targeted}
        all_ok = all_ok and caught
        print(json.dumps({"mutant": name, "control_passed": caught, "targeted_roles": targeted}))

    with open(os.path.join(evid, "impostor_privacy_negative_controls.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("ALL PLANTED LEAKS DETECTED AT EVERY PRE-REVEAL PHASE AND TARGETED ROLE, BASELINE CLEAN" if all_ok
          else "FAILED: see impostor_privacy_negative_controls.json")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
