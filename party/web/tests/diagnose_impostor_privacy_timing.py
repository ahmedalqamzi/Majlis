"""Diagnoses the 1-in-4 `real_e2e_impostor.py` round2-guess_word privacy signal
reported in RESULT.md: is it (a) a legitimate later `reveal` SSE event landing
in the *cumulative* byte scan after a stale `wait_for(phase_of, ...)` poll, or
(b) a genuine pre-reveal leak?

`real_e2e_impostor.py`'s `privacy_scan` greps the tab's whole cumulative
`rx_raw()` byte blob for the secret word. That cannot tell which SSE frame the
word actually arrived in. This script instead walks `tab.snaps()` — the same
parsed per-frame `state` events the production client already consumes — and
for each individual frame that contains the word, reports that frame's own
`game.roundPhase` / `game.reveal` fields (data the protocol already carries;
no new sequence number or wire field is added). If every occurrence is inside
a frame whose own `game.reveal` is already non-null, the word only ever
arrived already-revealed and the cumulative-log theory is confirmed, not
guessed. If any occurrence is in a frame with `game.reveal is None`, that is a
real pre-reveal leak.

Replays round1 (tie, no reveal) then round2 (caught, guess_word) exactly as
real_e2e_impostor.py does, stopping at the same `privacy_scan("round2-
guess_word", ...)` point, so it reproduces the identical window under
scrutiny. Runs N times (default 12) and persists every run's raw evidence
(JSON) to <out>/run-NN.json regardless of pass/fail, plus a summary.

    python diagnose_impostor_privacy_timing.py <repo root> <out dir> [runs]
"""
import json, os, sys, tempfile, subprocess, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import real_e2e as base  # noqa: E402


def attributed_scan(tab, word):
    """Per-frame attribution: which exact SSE state frame(s) carried `word`,
    and what that frame's own roundPhase/reveal said at the instant it arrived."""
    hits = []
    for i, snap in enumerate(tab.snaps()):
        blob = json.dumps(snap, ensure_ascii=False)
        if word in blob:
            g = snap.get("game") or {}
            hits.append({
                "frame_index": i,
                "roundId": snap.get("roundId"),
                "roundPhase": g.get("roundPhase"),
                "reveal_present": g.get("reveal") is not None,
            })
    return hits


def run_once(repo, out, n):
    server_py = os.environ.get("MAJLIS_SERVER_PY") or sys.executable
    port = base.free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = open(os.path.join(out, f"run-{n:02d}-server-stderr.log"), "w")
    server_cmd = [server_py, "-m", "party.server", "--port", str(port), "--web-dir", "party/web",
                  "--content-dir", "party/content", "--access-log", "--exit-on-stdin-eof"]
    srv = subprocess.Popen(server_cmd, cwd=repo, stdin=subprocess.PIPE, stdout=server_log, stderr=server_log)
    profile = tempfile.mkdtemp(prefix="majlis-party-diag-")
    dport = base.free_port()
    chrome = subprocess.Popen([base.CHROME, "--headless=new", f"--remote-debugging-port={dport}", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                               "--window-size=1280,800", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tabs = {}
    report = {"run": n, "verdict": None, "attributed_hits": {}, "cumulative_hits": [], "error": None}
    try:
        for _ in range(100):
            try:
                st, _, body = base.http_get(base_url + "/api/health")
                if st == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        for _ in range(100):
            try:
                ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json/version")); break
            except Exception:
                time.sleep(0.2)
        browser = base.Tab(ver["webSocketDebuggerUrl"], "browser")

        def new_tab(name, vp, isolated):
            if isolated:
                ctx = browser.call("Target.createBrowserContext", disposeOnDetach=False)["browserContextId"]
                tid = browser.call("Target.createTarget", url="about:blank", browserContextId=ctx, newWindow=True)["targetId"]
            else:
                tid = browser.call("Target.createTarget", url="about:blank")["targetId"]
            t = base.Tab(f"ws://127.0.0.1:{dport}/devtools/page/{tid}", name)
            t.call("Page.enable"); t.call("Runtime.enable"); t.call("Network.enable")
            t.call("Page.addScriptToEvaluateOnNewDocument", source=base.NET_HOOK)
            t.call("Emulation.setFocusEmulationEnabled", enabled=True)
            t.viewport(vp)
            tabs[name] = t
            return t

        host = new_tab("host", base.LAPTOP, isolated=False)
        host.goto(base_url + "/host.html")
        host.click("__btn('Create room')", "Create room")
        code = host.wait("(document.querySelector('.room-code')||{}).textContent", what="room code")
        join_url = host.eval("document.querySelector('[data-join-url]').getAttribute('data-join-url')")
        tv = new_tab("tv", base.TV, isolated=False)
        tv.goto(f"{base_url}/tv.html#code={code}")
        tv.wait("!!document.querySelector('.tv-lobby')", what="tv lobby")
        names = ["Aisha", "Omar", "Noora"]
        players = {}
        for nm in names:
            p = new_tab(nm, base.PHONE, isolated=True)
            p.goto(join_url)
            p.type_into("#join-name", nm)
            p.click("__btn('Join game')", "Join game")
            p.wait("document.body.textContent.includes('You’re in')", what="lobby")
            players[nm] = p
        pid = {}
        for nm, p in players.items():
            rec = json.loads(p.storage()[f"majlisParty.v1.player.{code}"])
            pid[nm] = rec["playerId"]
        tv.wait("document.querySelectorAll('.tv-lobby .chip').length === 3", what="tv sees 3 players")
        host.wait("document.querySelectorAll('.chip').length === 3", what="host sees players")
        others = {"host": host, "tv": tv}

        def current_impostor():
            for nm, p in players.items():
                you = (p.last_snap() or {}).get("game", {}).get("you", {})
                if you.get("role") == "impostor":
                    return nm
            return None

        def crew_word():
            for nm, p in players.items():
                you = (p.last_snap() or {}).get("game", {}).get("you", {})
                if you.get("role") == "crew" and you.get("word"):
                    return you["word"]
            return None

        def vote(nm, target):
            players[nm].click(f"__btn({json.dumps(target)})", f"vote {target}")

        def phase_of(t=None):
            t = t or host
            return (t.last_snap() or {}).get("game", {}).get("roundPhase")

        def wait_for(get, pred, timeout=12.0, what=None):
            end = time.time() + timeout
            last = None
            while time.time() < end:
                last = get()
                if pred(last):
                    return last
                time.sleep(0.15)
            raise TimeoutError(f"timed out waiting for {what or pred}; last: {json.dumps(last, default=str)[:300]}")

        host.click("document.querySelector('[data-mode=impostor]')", "impostor tile")
        host.wait("document.querySelector('[data-mode=impostor]').getAttribute('aria-pressed') === 'true'", what="mode chosen")
        host.click("__btn('Start Impostor')", "Start")
        for nm, p in players.items():
            p.wait("!!document.querySelector('.my-card')", what=f"{nm} role card")
        tv.wait("!!document.querySelector('.tv-who')", what="tv impostor view")
        wait_for(host.last_snap, lambda s: s and s["roundId"] == "g1r1", what="round g1r1")
        impostor1 = wait_for(current_impostor, lambda v: v, what="impostor1 revealed")
        word1 = wait_for(crew_word, lambda v: v, what="crew word1")

        host.click("__btn('Open voting')", "Open voting")
        for nm in names:
            players[nm].wait("!!document.querySelector('.who-cards') || document.body.textContent.includes('voted for')", what=f"{nm} vote ui")
        vote("Aisha", "Omar"); vote("Omar", "Noora"); vote("Noora", "Aisha")
        wait_for(lambda: phase_of(), lambda ph: ph == "reveal", timeout=10, what="round1 resolves")

        host.click("__btn('Next round')", "Next round")
        wait_for(lambda: host.last_snap()["roundId"], lambda rid: rid == "g1r2", what="round g1r2")
        impostor2 = wait_for(current_impostor, lambda v: v, what="impostor2 revealed")
        word2 = wait_for(crew_word, lambda v: v, what="crew word2")
        rid2 = host.last_snap()["roundId"]
        crew2 = [nm for nm in names if nm != impostor2]
        host.click("__btn('Open voting')", "Open voting")
        for nm in names:
            players[nm].wait("!!document.querySelector('.who-cards')", what=f"{nm} vote ui r2")
        vote(crew2[0], impostor2); vote(crew2[1], impostor2)
        players[impostor2].click(f"__btn({json.dumps(crew2[0])})", "impostor votes a crew member")
        wait_for(lambda: phase_of(), lambda ph: ph == "guess_word", timeout=10, what="round2 nominee caught")

        # ---- the exact moment under scrutiny ----
        for nm, t in {**others, impostor2: players[impostor2]}.items():
            raw = base.decoded_text(t.rx_raw())
            cum_hit = word2 in raw or word2 in t.html()
            hits = attributed_scan(t, word2)
            report["attributed_hits"][nm] = hits
            if cum_hit:
                report["cumulative_hits"].append(nm)

        pre_reveal_leak = any(h["reveal_present"] is False and h["roundPhase"] != "reveal" for v in report["attributed_hits"].values() for h in v)
        contamination_only = bool(report["cumulative_hits"]) and not pre_reveal_leak
        report["verdict"] = ("GENUINE_PRE_REVEAL_LEAK" if pre_reveal_leak else
                              "CUMULATIVE_LOG_CONTAMINATION_FROM_LEGITIMATE_REVEAL" if contamination_only else
                              "CLEAN")
    except Exception as e:
        report["error"] = repr(e)
        report["verdict"] = "ERROR"
    finally:
        try:
            chrome.terminate(); chrome.wait(timeout=5)
        except Exception:
            pass
        try:
            srv.stdin.close()
        except Exception:
            pass
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
    return report


def main():
    repo, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    runs = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    os.makedirs(out, exist_ok=True)
    summary = []
    for n in range(1, runs + 1):
        r = run_once(repo, out, n)
        with open(os.path.join(out, f"run-{n:02d}.json"), "w") as f:
            json.dump(r, f, indent=2, default=str)
        summary.append({"run": n, "verdict": r["verdict"], "error": r["error"]})
        print(json.dumps(summary[-1]), flush=True)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    leaks = [s for s in summary if s["verdict"] == "GENUINE_PRE_REVEAL_LEAK"]
    errors = [s for s in summary if s["verdict"] == "ERROR"]
    print(f"\n{runs} runs: {len(leaks)} genuine pre-reveal leak(s), {len(errors)} error(s)")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
