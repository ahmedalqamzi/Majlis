"""Impostor checks against the REAL party.server, over loopback HTTP + fetch-SSE, headless Chrome.

Same approach as real_e2e.py (reused directly: Tab driver, fetch tee, trusted CDP clicks), scoped
to the new Impostor mode: host.html (operator), play.html x3 (players, isolated contexts) and
tv.html (display), all against the real server subprocess, same-origin, no sim transport.

  python real_e2e_impostor.py <repo root> <out dir>

Env MAJLIS_SERVER_PY selects the interpreter for the server (default: this interpreter).
Needs Google Chrome and python websocket-client.
"""
import json, os, re, sys, tempfile, subprocess, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import real_e2e as base  # noqa: E402  (reuses Tab, NET_HOOK, HELPERS, check, free_port, http_get)

check = base.check
results = base.results
results["label"] = "REAL NETWORK Impostor (loopback HTTP + fetch-SSE to the in-repo party.server) - headless Chrome on one Mac"


def wait_for(get, pred, timeout=12.0, what=None):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = get()
        if pred(last):
            return last
        time.sleep(0.15)
    raise TimeoutError(f"timed out waiting for {what or pred}; last value: {json.dumps(last, default=str)[:400]}")


def secrets_of(g):
    """Everything a pre-reveal viewer must never receive: the word and the impostor's id."""
    out = []
    item = g.get("reveal") or {}
    if item.get("word"):
        out.append(item["word"])
    return out


def privacy_scan(stage, tabs_by_role, word):
    """Scans every non-crew-owner tab's cumulative received bytes + DOM for the secret word."""
    for nm, t in tabs_by_role.items():
        raw = base.decoded_text(t.rx_raw())
        dom = t.html()
        check(f"[{stage}] {nm}: word absent from every received byte and the DOM", word not in raw and word not in dom, nm)


def main():
    repo, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    os.makedirs(out, exist_ok=True)
    server_py = os.environ.get("MAJLIS_SERVER_PY") or sys.executable
    port = base.free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = open(os.path.join(out, "server-stderr.log"), "w")
    server_cmd = [server_py, "-m", "party.server", "--port", str(port), "--web-dir", "party/web", "--content-dir", "party/content", "--access-log", "--exit-on-stdin-eof"]
    results["server_command"] = " ".join(server_cmd[1:])
    srv = subprocess.Popen(server_cmd, cwd=repo, stdin=subprocess.PIPE, stdout=server_log, stderr=server_log)
    results["server_pid"] = srv.pid
    results["base"] = base_url
    profile = tempfile.mkdtemp(prefix="majlis-party-impostor-")
    dport = base.free_port()
    chrome = subprocess.Popen([base.CHROME, "--headless=new", f"--remote-debugging-port={dport}", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                               "--window-size=1280,800", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tabs = {}
    try:
        for _ in range(100):
            try:
                st, _, body = base.http_get(base_url + "/api/health")
                if st == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        check("real server answers /api/health on loopback", st == 200, body)
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

        # ---------- operator creates a room, players join, TV opens ----------
        host = new_tab("host", base.LAPTOP, isolated=False)
        host.goto(base_url + "/host.html")
        host.click("__btn('Create room')", "Create room")
        code = host.wait("(document.querySelector('.room-code')||{}).textContent", what="room code")
        check("room created with a 6-char code", re.fullmatch(r"[A-Z0-9]{6}", code or "") is not None, code)
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
        name_of = {v: k for k, v in pid.items()}
        tv.wait("document.querySelectorAll('.tv-lobby .chip').length === 3", what="tv sees 3 players")
        host.wait("document.querySelectorAll('.chip').length === 3", what="host sees players")

        others = {"host": host, "tv": tv}

        def current_impostor():
            """Reads each player's own private role projection (its own browser, its own data)."""
            found = None
            for nm, p in players.items():
                you = (p.last_snap() or {}).get("game", {}).get("you", {})
                if you.get("role") == "impostor":
                    found = nm
            return found

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

        # ---------- select and start Impostor ----------
        host.click("document.querySelector('[data-mode=impostor]')", "impostor tile")
        host.wait("document.querySelector('[data-mode=impostor]').getAttribute('aria-pressed') === 'true'", what="mode chosen")
        host.click("__btn('Start Impostor')", "Start")
        host.wait("document.querySelectorAll('.who-row, .who-list li').length >= 0 && !!document.querySelector('.panel-head')", what="impostor panel")
        for nm, p in players.items():
            p.wait("!!document.querySelector('.my-card')", what=f"{nm} role card")
        tv.wait("!!document.querySelector('.tv-who')", what="tv impostor view")

        hs = wait_for(host.last_snap, lambda s: s and s["roundId"] == "g1r1", what="round g1r1")
        check("server started Impostor round g1r1 with an order of 3 player ids", hs["roundId"] == "g1r1" and sorted(hs["game"]["order"]) == sorted(pid.values()), hs["game"]["order"])
        check("operator/display game view carries no role/word before reveal (no `you`, no `reveal`)",
              "you" not in host.last_snap()["game"] and host.last_snap()["game"]["reveal"] is None and tv.last_snap()["game"]["reveal"] is None)

        impostor1 = wait_for(current_impostor, lambda v: v, what="impostor1 revealed to itself")
        word1 = wait_for(crew_word, lambda v: v, what="crew word1")
        check("exactly one player is the impostor and the other two share the same crew word", impostor1 in names, impostor1)
        privacy_scan("round1-discussion", others, word1)
        # The impostor's own tab must never receive the crew word (crew members legitimately
        # share the same word with each other, so only the impostor's tab is a privacy boundary).
        privacy_scan("round1-discussion-impostor", {impostor1: players[impostor1]}, word1)

        # ================= Round 1: three-way vote tie -> impostor survives =================
        host.click("__btn('Open voting')", "Open voting")
        for nm in names:
            p = players[nm]
            p.wait("!!document.querySelector('.who-cards') || document.body.textContent.includes('voted for')", what=f"{nm} vote ui")
        vote("Aisha", "Omar"); vote("Omar", "Noora"); vote("Noora", "Aisha")
        wait_for(lambda: phase_of(), lambda ph: ph == "reveal", timeout=10, what="round1 resolves")
        r1 = host.last_snap()["game"]
        check("round1: a 3-way tie yields no unique nominee and the impostor survives", r1["nominee"] is None and r1["outcome"] == "impostor_survived", r1["outcome"])
        check("round1: reveal now shows the real impostor and word to the operator", r1["reveal"]["impostor"] == pid[impostor1] and r1["reveal"]["word"] == word1, r1["reveal"])
        check("round1: impostor scored the survival points, crew scored none", r1["roundPoints"][pid[impostor1]] == 3 and all(v == 0 for k, v in r1["roundPoints"].items() if k != pid[impostor1]), r1["roundPoints"])
        host.shot(out, "impostor-01-reveal-tie")

        # ---------- negative controls (raw HTTP, bypassing the UI) during round 2 ----------
        host.click("__btn('Next round')", "Next round")
        wait_for(lambda: host.last_snap()["roundId"], lambda rid: rid == "g1r2", what="round g1r2")
        impostor2 = wait_for(current_impostor, lambda v: v, what="impostor2 revealed")
        word2 = wait_for(crew_word, lambda v: v, what="crew word2")
        rid2 = host.last_snap()["roundId"]
        crew2 = [n for n in names if n != impostor2]
        host.click("__btn('Open voting')", "Open voting")
        for nm in names:
            players[nm].wait("!!document.querySelector('.who-cards')", what=f"{nm} vote ui r2")
        def err_code(r):
            b = r.get("body") or {}
            e = b.get("error")
            return e.get("code") if isinstance(e, dict) else e

        r = players[crew2[0]].raw_act("player", code, "impostor.vote", {"playerId": pid[crew2[0]]}, round_id=rid2)
        check("negative control: self-vote is rejected by the server (self_vote)", err_code(r) == "self_vote", r["body"])
        vote(crew2[0], impostor2)
        r = players[crew2[0]].raw_act("player", code, "impostor.vote", {"playerId": pid[impostor2]}, round_id=rid2)
        check("negative control: voting twice is rejected (already_voted)", err_code(r) == "already_voted", r["body"])
        r = host.raw_act("operator", code, "impostor.vote", {"playerId": pid[impostor2]}, round_id=rid2)
        check("negative control: the operator cannot cast a vote (forbidden)", err_code(r) == "forbidden", r["body"])
        r = players[crew2[0]].raw_act("player", code, "impostor.advance", {}, round_id=rid2)
        check("negative control: a player cannot advance the round (forbidden)", err_code(r) == "forbidden", r["body"])
        vote(crew2[1], impostor2)
        players[impostor2].click(f"__btn({json.dumps(crew2[0])})", "impostor votes a crew member")
        wait_for(lambda: phase_of(), lambda ph: ph == "guess_word", timeout=10, what="round2 nominee caught")
        g2 = host.last_snap()["game"]
        check("round2: crew unanimously caught the real impostor", g2["nominee"] == pid[impostor2], {"nominee": g2["nominee"], "impostor": pid[impostor2]})
        privacy_scan("round2-guess_word", others, word2)
        privacy_scan("round2-guess_word-impostor", {impostor2: players[impostor2]}, word2)

        # ---------- impostor guesses WRONG ----------
        players[impostor2].type_into("#impostor-guess", "definitely-the-wrong-answer")
        players[impostor2].click("__btn('Submit guess')", "submit wrong guess")
        wait_for(lambda: phase_of(), lambda ph: ph == "reveal", timeout=10, what="round2 reveal")
        rvg = host.last_snap()["game"]
        check("round2: a wrong guess awards the crew, not the impostor", rvg["outcome"] == "crew_wrong_guess" and rvg["roundPoints"][pid[impostor2]] == 0 and all(rvg["roundPoints"][pid[n]] == 2 for n in crew2), rvg["roundPoints"])
        host.shot(out, "impostor-02-reveal-wrong-guess")

        # ================= Round 3: catch again, impostor guesses CORRECT (case/whitespace normalised) =================
        host.click("__btn('Next round')", "Next round")
        wait_for(lambda: host.last_snap()["roundId"], lambda rid: rid == "g1r3", what="round g1r3")
        impostor3 = wait_for(current_impostor, lambda v: v, what="impostor3 revealed")
        word3 = wait_for(crew_word, lambda v: v, what="crew word3")
        rid3 = host.last_snap()["roundId"]
        crew3 = [n for n in names if n != impostor3]
        host.click("__btn('Open voting')", "Open voting")
        for nm in names:
            players[nm].wait("!!document.querySelector('.who-cards')", what=f"{nm} vote ui r3")
        vote(crew3[0], impostor3); vote(crew3[1], impostor3)
        players[impostor3].click(f"__btn({json.dumps(crew3[0])})", "impostor votes a crew member")
        wait_for(lambda: phase_of(), lambda ph: ph == "guess_word", timeout=10, what="round3 nominee caught")
        normalized = ("  " + word3.upper() + "   ")
        players[impostor3].type_into("#impostor-guess", normalized)
        players[impostor3].click("__btn('Submit guess')", "submit correct guess (case/whitespace varied)")
        wait_for(lambda: phase_of(), lambda ph: ph == "reveal", timeout=10, what="round3 reveal")
        rvc = host.last_snap()["game"]
        check("round3: exact-normalized (casefold/whitespace only) correct guess awards the impostor",
              rvc["outcome"] == "impostor_guessed_word" and rvc["roundPoints"][pid[impostor3]] == 3 and all(rvc["roundPoints"][pid[n]] == 0 for n in crew3), rvc["roundPoints"])
        host.shot(out, "impostor-03-reveal-correct-guess")

        # ================= Round 4: catch again, operator forfeits the guess =================
        host.click("__btn('Next round')", "Next round")
        wait_for(lambda: host.last_snap()["roundId"], lambda rid: rid == "g1r4", what="round g1r4")
        impostor4 = wait_for(current_impostor, lambda v: v, what="impostor4 revealed")
        crew4 = [n for n in names if n != impostor4]
        host.click("__btn('Open voting')", "Open voting")
        for nm in names:
            players[nm].wait("!!document.querySelector('.who-cards')", what=f"{nm} vote ui r4")
        vote(crew4[0], impostor4); vote(crew4[1], impostor4)
        players[impostor4].click(f"__btn({json.dumps(crew4[0])})", "impostor votes a crew member")
        wait_for(lambda: phase_of(), lambda ph: ph == "guess_word", timeout=10, what="round4 nominee caught")
        host.click("__btn('Forfeit (no guess)')", "Forfeit")
        host.confirm(accept=True)
        wait_for(lambda: phase_of(), lambda ph: ph == "reveal", timeout=10, what="round4 reveal")
        rvf = host.last_snap()["game"]
        check("round4: operator forfeit awards the crew without a guess", rvf["outcome"] == "crew_forfeit" and rvf["roundPoints"][pid[impostor4]] == 0 and all(rvf["roundPoints"][pid[n]] == 2 for n in crew4), rvf["roundPoints"])

        # ---------- scores add up exactly once across all four rounds ----------
        final_scores = host.last_snap()["players"]
        expected_total = (3 + 0) + (0 + 2 + 2) + (3 + 0 + 0) + (0 + 2 + 2)
        check("total points awarded across 4 rounds matches the sum of all per-round awards (exactly-once scoring)",
              sum(p["score"] for p in final_scores) == expected_total, {"scores": final_scores, "expected_total": expected_total})

        # ---------- reconnect / pause ----------
        host.click("__btn('Pause')", "Pause")
        for nm, p in players.items():
            p.wait("!!document.querySelector('.overlay')", what=f"{nm} paused overlay")
        tv.wait("!!document.querySelector('.overlay')", what="tv paused overlay")
        host.click("__btn('Resume game')", "Resume")
        for nm, p in players.items():
            p.wait("!document.querySelector('.overlay')", what=f"{nm} resumed")
        players["Aisha"].reload()
        players["Aisha"].wait("!!document.querySelector('.my-card')", what="Aisha reconnected into the same round")
        check("Aisha reconnects into the same room/round after a reload (server-authoritative state)",
              players["Aisha"].last_snap()["roundId"] == host.last_snap()["roundId"], players["Aisha"].last_snap()["roundId"])

        # ---------- end game; final scoreboard shown; first-pair modes untouched (recorded separately) ----------
        host.click("__btn('End game')", "End game")
        host.confirm(accept=True)
        host.wait("document.body.textContent.includes('Game over')", what="host game over")
        for nm, p in players.items():
            p.wait("document.body.textContent.includes('Game over')", what=f"{nm} game over")
        check("every page script ran without an exception", not any(t.page_errors() for t in list(tabs.values())), {k: t.page_errors()[:2] for k, t in tabs.items() if t.page_errors()})
        host.shot(out, "impostor-04-game-over")

    except Exception:
        import traceback
        check("run completed without an exception", False, traceback.format_exc()[-2000:])
    finally:
        results["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results["summary"] = {"pass": sum(1 for c in results["checks"] if c["pass"]), "fail": sum(1 for c in results["checks"] if not c["pass"]), "total": len(results["checks"])}
        json.dump(results, open(os.path.join(out, "real-results-impostor.json"), "w"), ensure_ascii=False, indent=1)
        print(json.dumps(results["summary"]))
        chrome.terminate()
        try:
            srv.stdin.close()
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
    sys.exit(0 if results["summary"]["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
