"""Negative controls: each mutates a copy of the JS core or tables, replays the
relevant oracle suites in JSC and requires compare.py to go red.

Usage: python3 -I -B negative_controls.py TREE TRACE_DIR NEG_DIR JSC_RUNNER
Exit status 0 only when every mutation is detected.
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare  # noqa: E402

CORE, TABLES = "majlis-core.js", "unicode13.generated.js"

# name -> (file, old, new, suites, what it breaks)
MUTATIONS = {
    "feud-max-strikes-4": (CORE, "MAX_STRIKES = 3,", "MAX_STRIKES = 4,", ["golden"],
                           "rule change: Feud allows a fourth strike"),
    "feud-second-buzz-wins": (
        CORE,
        'if (state.buzzer.winner !== null) return [state, "buzzer_taken"];\n'
        '        if (state.roundPhase !== "buzzer_open" || !state.buzzer.open) return [state, "buzzer_closed"];',
        'if (state.buzzer.winner === null && (state.roundPhase !== "buzzer_open" || !state.buzzer.open)) '
        'return [state, "buzzer_closed"];',
        ["golden"], "buzz ordering: a later buzz overrides the first winner"),
    "whoami-player-sees-own-card": (CORE, "if (p !== me) m[p] = publicItem(A[p]);", "m[p] = publicItem(A[p]);",
                                    ["golden"], "private-field leak: a player sees their own Who Am I card"),
    "whoami-guesses-to-display": (
        CORE,
        "        order.forEach(function (p) { if (isRev(p)) m[p] = publicItem(A[p]); });\n",
        "        order.forEach(function (p) { if (isRev(p)) m[p] = publicItem(A[p]); });\n"
        "        view.guesses = {}; order.forEach(function (p) { if (has(state.guesses, p)) "
        "view.guesses[p] = state.guesses[p].text; });\n",
        ["golden"], "private-field leak: display/non-member sees pending guesses"),
    "feud-hidden-aliases-leak": (CORE, ": { index: index, revealed: false };",
                                 ": { index: index, revealed: false, aliases: b.answers[index].aliases.slice() };",
                                 ["golden"], "private-field leak: unrevealed answer aliases to every role"),
    "actionid-dedup-off": (CORE, "if (log.has(actionId)) {", "if (false && log.has(actionId)) {", ["golden"],
                           "replay: duplicate actionId is applied again"),
    "token-draw-swapped": (CORE,
                           "var hostToken = Secrets.tokenUrlsafe(pool), displayToken = Secrets.tokenUrlsafe(pool);",
                           "var displayToken = Secrets.tokenUrlsafe(pool), hostToken = Secrets.tokenUrlsafe(pool);",
                           ["golden"], "entropy order: host/display token draws swapped"),
    "fold-drop-U+1E9E": (TABLES, "7838,2,115,115,", "", ["text", "sweep"],
                         "casefold table loses U+1E9E -> ss"),
    "comp-drop-a-acute": (TABLES, "97,769,225,", "", ["text", "sweep"],
                          "composition table loses a + U+0301 -> U+00E1"),
}


def main():
    tree, trace_dir, neg_dir, runner = sys.argv[1:5]
    core_dir = os.path.join(tree, "party", "core")
    ok = True
    summary = []
    for name, (fname, old, new, suites, what) in MUTATIONS.items():
        d = os.path.join(neg_dir, name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        for f in (CORE, TABLES):
            shutil.copyfile(os.path.join(core_dir, f), os.path.join(d, f))
        path = os.path.join(d, fname)
        src = open(path, encoding="utf-8").read()
        n = src.count(old)
        if n != 1:
            print("%-28s MUTATION NOT APPLIED (%d matches)" % (name, n))
            ok = False
            continue
        open(path, "w", encoding="utf-8").write(src.replace(old, new))
        result = {"mutation": name, "breaks": what, "file": fname, "suites": {}}
        red = 0
        for s in suites:
            os.symlink(os.path.join(trace_dir, s + ".py.jsonl"), os.path.join(d, s + ".py.jsonl"))
            proc = subprocess.run([runner, os.path.join(d, TABLES), os.path.join(d, CORE),
                                   os.path.join(core_dir, "runner", "trace-runner.js"),
                                   os.path.join(d, s + ".py.jsonl"), os.path.join(d, s + ".js.jsonl")],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                result["suites"][s] = {"runnerExit": proc.returncode, "stderr": proc.stderr.decode()[-300:]}
                red += 1
                continue
            if s in ("golden", "fuzz"):
                r = compare.compare_rooms(d, s, d)
            elif s == "text":
                r = compare.compare_text(d, d)
            else:
                r = compare.compare_sweep(d, d)
            result["suites"][s] = {"mismatches": r["mismatches"], "compared": r["counts"].get("compared"),
                                   "firstMismatch": first_line(os.path.join(d, s + ".mismatches.jsonl"))}
            red += r["mismatches"]
            os.remove(os.path.join(d, s + ".js.jsonl"))
        result["detected"] = red > 0
        ok = ok and red > 0
        summary.append(result)
        print("%-28s %s  %s" % (name, "RED (detected)" if red else "GREEN (NOT DETECTED)",
                                {s: v.get("mismatches", v.get("runnerExit")) for s, v in result["suites"].items()}))
    with open(os.path.join(neg_dir, "summary.json"), "w", encoding="ascii") as handle:
        json.dump(summary, handle, ensure_ascii=True, indent=1)
    return 0 if ok else 1


def first_line(path):
    try:
        with open(path, encoding="ascii") as handle:
            line = handle.readline()
        return json.loads(line) if line else None
    except OSError:
        return None


if __name__ == "__main__":
    sys.exit(main())
