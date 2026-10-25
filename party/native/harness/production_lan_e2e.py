"""Production-surface LAN acceptance for the native host (L01B).

The PRODUCTION build of majlis-party-native binds a real LAN address of this Mac
(--host <LAN IPv4> --allow-lan) and serves only the player surface. The operator
drives the room through the private stdin/stdout bridge (--operator-stdin), never
over the network. Three players run in isolated headless-Chrome contexts against
http://<LAN IP>:<port>/play.html?code=ROOM, rendered and touched through CDP.

What this proves: the production binary, over a LAN-bound TCP socket, plays full
Who Am I and Majlis Feud games with server-authoritative scoring, contested buzz
races, private projections in every received byte and the DOM, refresh/offline
resume, and that the network surface issues no operator/display credentials and
accepts no operator control. Same-host traffic to the Mac's own LAN address does
not leave the machine, so this is not evidence about Wi-Fi, a phone, offline or WAN.

  python production_lan_e2e.py <repo root> <native production binary> <out dir> <LAN IPv4>

Reuses the reference real_e2e.py browser helpers (Tab, NET_HOOK, HELPERS); needs
Google Chrome and websocket-client.
"""
import hashlib, json, os, queue, re, secrets, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request

REPO = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.join(REPO, "party", "web", "tests"))
import real_e2e as R  # noqa: E402  (browser helpers only; its main() is not run)
from real_e2e import Tab, NET_HOOK, PHONE, SMALL, LANDSCAPE, CHROME, decoded_text, sse_events  # noqa: E402

results = {"label": "PRODUCTION surface, LAN-bound socket on this Mac, operator via private stdin bridge, 3 isolated headless-Chrome players. "
                    "Same-host traffic; not a phone, not Wi-Fi, not offline/WAN evidence.", "checks": []}


def check(name, ok, detail=None):
    results["checks"].append({"name": name, "pass": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if detail is None else "  " + json.dumps(detail, ensure_ascii=False, default=str)[:300]), flush=True)
    return ok


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


class Operator:
    """One JSON request per stdin line, one reply per stdout line."""

    def __init__(self, proc, out):
        self.proc, self.lines, self.lock = proc, queue.Queue(), threading.Lock()
        self.transcript = open(os.path.join(out, "operator-bridge.jsonl"), "w")
        self.raw = []  # every reply line, for leak scans and the positive control
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for ln in self.proc.stdout:
            self.lines.put(ln.decode("utf-8"))

    def banner(self, timeout=10):
        return json.loads(self.lines.get(timeout=timeout))

    def call(self, req):
        with self.lock:
            self.proc.stdin.write(json.dumps(req, ensure_ascii=False).encode() + b"\n")
            self.proc.stdin.flush()
            ln = self.lines.get(timeout=10)
        self.raw.append(ln)
        self.transcript.write(json.dumps({"req": req, "reply": json.loads(ln)}, ensure_ascii=False) + "\n")
        self.transcript.flush()
        return json.loads(ln)

    def decoded(self):
        return "\n".join(json.dumps(json.loads(ln), ensure_ascii=False) for ln in self.raw)

    def act(self, code, type_, payload=None, round_id=None, action_id=None):
        body = {"actionId": action_id or secrets.token_hex(8), "type": type_, "payload": payload or {}}
        if round_id is not None:
            body["roundId"] = round_id
        r = self.call({"op": "action", "code": code, "body": body})
        time.sleep(0.12)
        return r

    def state(self, code):
        return self.call({"op": "state", "code": code})["state"]


def http(method, url, headers=None, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def keys_deep(v, acc):
    if isinstance(v, dict):
        for k, x in v.items():
            acc.add(k)
            keys_deep(x, acc)
    elif isinstance(v, list):
        for x in v:
            keys_deep(x, acc)
    return acc


def main():
    native, out, lan = os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]), sys.argv[4]
    os.makedirs(out, exist_ok=True)
    s = socket.socket(); s.bind((lan, 0)); port = s.getsockname()[1]; s.close()
    assert port != 8765
    base = f"http://{lan}:{port}"
    origin = base
    slog = open(os.path.join(out, "server-stderr.log"), "w")
    cmd = [native, "--host", lan, "--port", str(port), "--allow-lan", "--web-dir", "party/web", "--content-dir", "party/content",
           "--operator-stdin", "--exit-on-stdin-eof", "--access-log"]
    results["server_command"] = " ".join(["majlis-party-native"] + cmd[1:])
    srv = subprocess.Popen(cmd, cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slog)
    results["server_pid"] = srv.pid
    op = Operator(srv, out)
    banner = op.banner()
    results["banner"] = banner
    check("production banner: surface=production, bound to the LAN address, persistence memory-only",
          banner.get("surface") == "production" and banner.get("host") == lan and banner.get("url") == base and banner.get("event") == "listening", banner)

    profile = tempfile.mkdtemp(prefix="majlis-l01b-lan-")
    dport_s = socket.socket(); dport_s.bind(("127.0.0.1", 0)); dport = dport_s.getsockname()[1]; dport_s.close()
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={dport}", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                               "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                               "--window-size=1280,800", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results["chrome_pid"] = chrome.pid
    tabs, net_log, tokens = {}, [], {}
    try:
        # ------------------------------------------------ production network surface
        st, h, body = http("GET", base + "/api/health")
        check("LAN /api/health 200 over the LAN-bound socket", st == 200 and json.loads(body).get("ok") is True, body)
        st, h, page = http("GET", base + "/play.html")
        check("player page served with the HTML CSP", st == 200 and "script-src 'self'" in h.get("Content-Security-Policy", ""), st)
        hidden = {}
        for p in ("/host.html", "/tv.html", "/js/host.js", "/js/tv.js"):
            hidden[p] = http("GET", base + p)[0]
        check("operator and display pages/scripts are not served on the production LAN surface (404)", set(hidden.values()) == {404}, hidden)
        qa, qb = http("GET", base + "/host.html?code=ABCDEF"), http("GET", base + "/no-such-page.html?code=ABCDEF")
        check("a query on the hidden host page is answered exactly like a query on a page that never existed (no surface disclosure)",
              qa[0] == qb[0] == 400 and qa[2] == qb[2], {"host.html?": qa[0], "no-such?": qb[0]})
        st, h, body = http("POST", base + "/api/rooms", {"Content-Type": "application/json", "Origin": origin, "Sec-Fetch-Site": "same-origin"}, b"{}")
        check("POST /api/rooms (room creation = host+display token issue) is not routed on production (404 not_found)",
              st == 404 and json.loads(body)["error"]["code"] == "not_found", {"status": st, "body": body[:200]})
        st, h, body = http("POST", base + "/api/rooms", {"Content-Type": "application/json"}, b'{"operatorName":"x"}')
        check("POST /api/rooms without browser headers is also 404 (no token-issuing route exists)", st == 404, st)

        created = op.call({"op": "create", "body": {}})
        code = created["code"]
        check("bridge create: room code only, no credential in the reply", created["ok"] and re.fullmatch(r"[A-Z0-9]{6}", code) is not None
              and not any("token" in k.lower() for k in keys_deep(created, set())), sorted(created))

        # A forged operator-looking bearer is unusable on the network.
        forged = secrets.token_urlsafe(32)
        st, _, body = http("POST", f"{base}/api/rooms/{code}/actions", {"Content-Type": "application/json", "Origin": origin,
                           "Authorization": "Bearer " + forged}, json.dumps({"actionId": "a" * 16, "type": "room.start", "payload": {}}).encode())
        check("forged bearer refused for an operator control (401 unauthorized)", st == 401 and json.loads(body)["error"]["code"] == "unauthorized", body[:200])
        st, _, body = http("GET", f"{base}/api/rooms/{code}/state", {"Origin": origin, "Authorization": "Bearer " + forged})
        check("forged bearer cannot read room state (401)", st == 401, st)
        st, _, body = http("GET", f"{base}/api/rooms/{code}/state", {"Host": f"127.0.0.1:{port}"})
        check("loopback Host header refused on the LAN listener (not in the allowed host set)", st in (400, 403, 421) and "error" in body, {"status": st, "body": body[:160]})

        # ------------------------------------------------ browser players
        browser = None
        for _ in range(100):
            try:
                ver = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json/version", timeout=2).read())
                browser = Tab(ver["webSocketDebuggerUrl"], "browser")
                break
            except Exception:
                time.sleep(0.1)

        def new_tab(name, vp):
            ctx = browser.call("Target.createBrowserContext", disposeOnDetach=False)["browserContextId"]
            tid = browser.call("Target.createTarget", url="about:blank", browserContextId=ctx, newWindow=True)["targetId"]
            t = Tab(f"ws://127.0.0.1:{dport}/devtools/page/{tid}", name)
            t.call("Page.enable"); t.call("Runtime.enable"); t.call("Network.enable")
            t.call("Page.addScriptToEvaluateOnNewDocument", source=NET_HOOK)
            t.call("Emulation.setFocusEmulationEnabled", enabled=True)
            t.viewport(vp)
            tabs[name] = t
            return t

        names = ["Aisha", "Omar", "Noora"]
        vps = {"Aisha": PHONE, "Omar": PHONE, "Noora": SMALL}
        players = {}
        for nm in names:
            p = new_tab(nm, vps[nm])
            if nm == "Omar":
                p.goto(base + "/play.html")
                p.type_into("#join-code", code.lower())
            else:
                p.goto(f"{base}/play.html?code={code}")
            p.type_into("#join-name", nm)
            p.click("__btn('Join game')", "Join game")
            p.wait("document.body.textContent.includes('You’re in')", what="lobby")
            players[nm] = p
            time.sleep(0.3)
        pid = {}
        for nm, p in players.items():
            rec = json.loads(p.storage()[f"majlisParty.v1.player.{code}"])
            tokens[f"player:{nm}:{code}"] = rec["token"]
            pid[nm] = rec["playerId"]
            keys = sorted(k for k in p.storage() if k.startswith("majlisParty."))
            check(f"{nm}: isolated context holds only its own player seat", keys == sorted([f"majlisParty.v1.player.{code}", "majlisParty.v1.last.player"]), keys)
        check("three distinct player seats issued over the LAN", len({tokens[f'player:{n}:{code}'] for n in names}) == 3 and len(set(pid.values())) == 3)
        name_of = {v: k for k, v in pid.items()}
        players["Aisha"].shot(out, "01-lan-player-lobby-393")
        a, o, n = players["Aisha"], players["Omar"], players["Noora"]

        # A player credential cannot drive operator controls over the network.
        for t in ("room.set_mode", "room.start", "room.close"):
            bad = a.raw_act("player", code, t, {"mode": "who_am_i"} if t == "room.set_mode" else {})
            check(f"player bearer refused for operator control {t} (403 forbidden), room unchanged",
                  bad["status"] == 403 and bad["body"]["error"]["code"] == "forbidden" and op.state(code)["phase"] == "lobby", bad)

        # ================= Who Am I (operator via bridge) =================
        r = op.act(code, "room.set_mode", {"mode": "who_am_i"})
        r2 = op.act(code, "room.start")
        check("bridge set_mode + start accepted", r["ok"] and r["status"] == 200 and r2["ok"] and r2["status"] == 200, [r.get("status"), r2.get("status")])
        hs = op.state(code)
        r1 = hs["roundId"]
        assign = hs["game"]["assignments"]
        check("server started Who Am I g1r1 with an assignment per player", r1 == "g1r1" and sorted(assign) == sorted(pid.values()), r1)
        labels = {name_of[k]: v for k, v in assign.items()}

        def secrets_of(x):
            return [v for v in (x.get("label"), x.get("ar")) if v]

        for nm, p in players.items():
            p.wait("!!document.querySelector('.my-card')", what="who am i")
            others = [labels[x]["label"] for x in names if x != nm]
            check(f"{nm}: sees the other two characters", all(x in p.html() for x in others), others)

        def who_privacy(stage):
            for nm, p in players.items():
                raw_all = decoded_text(p.rx_raw())
                dom = p.html()
                you = (p.last_snap()["game"] or {}).get("you", {})
                if not you.get("revealed"):
                    check(f"[{stage}] {nm}: own character absent from every received byte and the DOM",
                          not any(v in raw_all or v in dom for v in secrets_of(labels[nm])), labels[nm]["label"])
            braw = op.decoded()
            check(f"[{stage}] positive control: the scan finds every character in the operator bridge bytes",
                  all(v in braw for x in labels.values() for v in secrets_of(x)))

        who_privacy("asking")
        a.shot(out, "02-lan-player-whoami-393", full=True)
        n.shot(out, "03-lan-player-whoami-360")

        plant = {k: f"PLANT-{k}-{secrets.token_hex(4)}" for k in ("A1", "N1")}
        a.type_into("#guess", plant["A1"])
        a.click("__btn('Send guess')", "guess A1")
        a.wait(f"document.body.textContent.includes({json.dumps(plant['A1'])})", what="guess pending")
        n.type_into("#guess", plant["N1"])
        n.click("__btn('Send guess')", "guess N1")
        n.wait(f"document.body.textContent.includes({json.dumps(plant['N1'])})", what="guess pending")
        hs = op.state(code)
        check("operator bridge sees both pending guesses", sorted(hs["game"]["pendingGuesses"]) == sorted([pid["Aisha"], pid["Noora"]]), hs["game"]["pendingGuesses"])
        r = op.act(code, "whoami.award", {"playerId": pid["Aisha"]}, round_id=r1)
        a.wait("[...document.querySelectorAll('.score-row')].some(x => x.textContent.includes('Aisha') && x.textContent.trim().endsWith('1'))", what="Aisha scored")
        check("bridge award: Aisha's score 1 in her own SSE snapshot", [q["score"] for q in a.last_snap()["players"] if q["id"] == pid["Aisha"]] == [1], r.get("status"))
        r = op.act(code, "whoami.dismiss", {"playerId": pid["Noora"]}, round_id=r1)
        n.wait("!(document.body.textContent.includes('is with the host'))", what="dismissed")
        check("bridge dismiss: Noora's pending guess gone from her snapshot", n.last_snap()["game"]["you"]["guess"] is None, r.get("status"))
        r = op.act(code, "whoami.dismiss", {"playerId": pid["Noora"]}, round_id=r1)
        check("bridge dismiss with no pending guess refused 409 no_pending_guess", r["status"] == 409 and r["result"]["error"]["code"] == "no_pending_guess", r)
        bad = a.raw_act("player", code, "whoami.guess", {"text": "x"}, round_id="g1r9")
        check("wrong-round player action refused 409 stale_round", bad["status"] == 409 and bad["body"]["error"]["code"] == "stale_round", bad)
        bad = a.raw_act("player", code, "whoami.award", {"playerId": pid["Aisha"]}, round_id=r1)
        check("player cannot send an operator game action (403 forbidden)", bad["status"] == 403 and bad["body"]["error"]["code"] == "forbidden", bad)
        sc = {q["id"]: q["score"] for q in op.state(code)["players"]}
        check("rejected actions changed no score", sc == {pid["Aisha"]: 1, pid["Omar"]: 0, pid["Noora"]: 0}, sc)
        op.act(code, "whoami.reveal", {"playerId": pid["Omar"]}, round_id=r1)
        o.wait("!!document.querySelector('.my-card.revealed')", what="Omar revealed")
        check("individual reveal: Noora still cannot see her own character", labels["Noora"]["label"] not in decoded_text(n.rx_raw()))
        who_privacy("after individual reveal")
        op.act(code, "whoami.reveal", {}, round_id=r1)
        for p in players.values():
            p.wait("!!document.querySelector('.my-card.revealed')", what="global reveal")
        check("global reveal reaches every phone", True)
        for k, owner in (("A1", "Aisha"), ("N1", "Noora")):
            for nm, p in players.items():
                seen = plant[k] in decoded_text(p.rx_raw()) or plant[k] in p.html()
                if nm == owner:
                    check(f"guess {k}: owner {nm} received it", seen)
                else:
                    check(f"guess {k}: {nm} never received it (bytes + DOM)", not seen)
            check(f"guess {k}: positive control - operator bridge bytes contain it", plant[k] in op.decoded())

        aid = secrets.token_hex(8)
        nx = op.act(code, "round.next", {}, round_id=r1, action_id=aid)
        rep = op.act(code, "round.next", {}, round_id=r1, action_id=aid)
        dup = op.act(code, "round.next", {}, round_id=r1)
        rr = op.state(code)["roundId"]
        check("round.next: g1r2; same actionId replayed (replayed=true); fresh duplicate for old round refused stale_round",
              nx["status"] == 200 and rep["replayed"] is True and dup["status"] == 409 and dup["result"]["error"]["code"] == "stale_round" and rr == "g1r2",
              {"next": nx.get("status"), "replayed": rep.get("replayed"), "dup": dup.get("result"), "round": rr})
        labels = {name_of[k]: v for k, v in op.state(code)["game"]["assignments"].items()}
        for p in players.values():
            p.wait("!!document.querySelector('.my-card') && !document.querySelector('.my-card.revealed')", what="round 2")
        who_privacy("round 2")

        op.act(code, "room.pause")
        for p in players.values():
            p.wait("document.body.textContent.toLowerCase().includes('paused')", what="paused")
        bad = a.raw_act("player", code, "whoami.ready", {"ready": True}, round_id="g1r2")
        check("action while paused refused 409 paused", bad["status"] == 409 and bad["body"]["error"]["code"] == "paused", bad)
        op.act(code, "room.resume")

        # Refresh resume: same seat, same score.
        a.reload()
        a.wait("!!document.querySelector('.my-card')", what="Aisha after refresh")
        check("player refresh resumes the same seat and score", a.last_snap()["self"]["id"] == pid["Aisha"]
              and [q["score"] for q in a.last_snap()["players"] if q["id"] == pid["Aisha"]] == [1])
        # Offline -> reconnect with Last-Event-ID, catching up on a score given meanwhile.
        o.call("Network.emulateNetworkConditions", offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        o.wait("document.body.dataset.conn === 'reconnecting'", timeout=15, what="reconnecting")
        op.act(code, "whoami.award", {"playerId": pid["Noora"]}, round_id="g1r2")
        o.call("Network.emulateNetworkConditions", offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        o.wait("document.body.dataset.conn === 'live'", timeout=15, what="live again")
        o.wait("[...document.querySelectorAll('.score-row')].some(x => x.textContent.includes('Noora') && x.textContent.trim().endsWith('1'))", what="caught up")
        last_ids = [t["lastEventId"] for t in o.eval("window.__tx") if t["path"].endswith("/events")]
        check("offline then online: reconnects itself with Last-Event-ID and shows the award made meanwhile", any(last_ids), last_ids[-3:])
        o.shot(out, "04-lan-player-reconnected-393")

        op.act(code, "room.end")
        for nm, p in players.items():
            p.wait("document.body.textContent.includes('Game over')", what=f"{nm} ended")
        final = {q["name"]: q["score"] for q in op.state(code)["players"]}
        check("final Who Am I scores: bridge state and every phone agree", final == {"Aisha": 1, "Omar": 0, "Noora": 1}
              and all({q["name"]: q["score"] for q in p.last_snap()["players"]} == final for p in players.values()), final)

        # The player page closes its stream at Game over (by design), so a new game is a new room,
        # as the reference flow does: close room 1, create room 2, the same three phones join it.
        cl = op.act(code, "room.close")
        op.call({"op": "forget", "code": code})
        codes = [code]
        code = op.call({"op": "create", "body": {}})["code"]
        codes.append(code)
        check("bridge closes room 1 and creates room 2", cl["status"] == 200 and code != codes[0], cl.get("status"))
        for nm, p in players.items():
            p.goto(f"{base}/play.html?code={code}")
            p.type_into("#join-name", nm)
            p.click("__btn('Join game')", "Join game")
            p.wait("document.body.textContent.includes('You’re in')", what="lobby 2")
            rec = json.loads(p.storage()[f"majlisParty.v1.player.{code}"])
            tokens[f"player:{nm}:{code}"] = rec["token"]
            pid[nm] = rec["playerId"]
            time.sleep(0.2)
        name_of = {v: k for k, v in pid.items()}

        # ================= Majlis Feud =================
        op.act(code, "room.set_mode", {"mode": "feud"})
        r = op.act(code, "room.start")
        hs = op.state(code)
        b1, board = hs["roundId"], hs["game"]["answers"]
        check("bridge set_mode + start in room 2: Feud board g1b1 with 6 answers (operator projection only)", r["status"] == 200 and b1 == "g1b1" and len(board) == 6, b1)
        for p in players.values():
            p.wait("!!document.querySelector('.feud-board')", what="feud")
        ps = n.last_snap()["game"]
        check("player projection: hidden slots only, no answers[] list", not any(x["revealed"] for x in ps["slots"]) and "answers" not in ps, len(ps["slots"]))

        def feud_privacy(stage, board):
            shown = {x["id"] for x in op.state(code)["game"]["slots"] if x["revealed"]}
            for t in players.values():
                raw_t = decoded_text(t.rx_raw())
                dom = t.eval("document.body.innerText")
                hidden_leak = [x["text"] for x in board if x["id"] not in shown and (json.dumps(x["text"], ensure_ascii=False) in raw_t or x["text"] in dom)]
                alias_leak = [al for x in board for al in x["aliases"] if json.dumps(al, ensure_ascii=False) in raw_t]
                check(f"[{stage}] {t.name}: no hidden answer and no alias received or shown", not hidden_leak and not alias_leak, {"hidden": hidden_leak, "aliases": alias_leak})
            braw = json.dumps(op.state(code), ensure_ascii=False)
            check(f"[{stage}] positive control: operator bridge state contains every answer and alias",
                  all(json.dumps(x["text"], ensure_ascii=False) in braw and all(json.dumps(al, ensure_ascii=False) in braw for al in x["aliases"]) for x in board))

        feud_privacy("board 1 closed", board)
        op.act(code, "feud.open_buzzer", {}, round_id=b1)
        o.wait("document.getElementById('buzzer').classList.contains('state-open')", what="buzz open")
        o.click("document.getElementById('buzzer')", "buzz (trusted touch)", block="center")
        o.wait("document.getElementById('buzzer').classList.contains('state-won')", what="Omar won")
        hs = op.state(code)
        check("trusted touch buzz: server winner = control = Omar, phase answering",
              hs["game"]["buzzer"]["winner"] == pid["Omar"] and hs["game"]["control"] == pid["Omar"] and hs["game"]["roundPhase"] == "answering", hs["game"]["buzzer"])
        op.act(code, "feud.reveal", {"answerId": board[0]["id"]}, round_id=b1)
        for p in players.values():
            p.wait(f"document.body.innerText.includes({json.dumps(board[0]['text'])})", what="answer 1")
        for _ in range(3):
            op.act(code, "feud.strike", {}, round_id=b1)
        op.act(code, "feud.steal", {"playerId": pid["Noora"]}, round_id=b1)
        n.wait("document.body.textContent.includes('Your chance to steal')", what="steal")
        hs = op.state(code)
        check("three strikes then steal: control = Noora, stealFrom = Omar, phase steal",
              hs["game"]["strikes"] == 3 and hs["game"]["control"] == pid["Noora"] and hs["game"]["stealFrom"] == pid["Omar"] and hs["game"]["roundPhase"] == "steal",
              {k: hs["game"][k] for k in ("strikes", "control", "stealFrom", "roundPhase")})
        op.act(code, "feud.reveal", {"answerId": board[2]["id"]}, round_id=b1)
        expect = board[0]["points"] + board[2]["points"]
        aid = secrets.token_hex(8)
        aw = op.act(code, "feud.award", {"playerId": pid["Noora"]}, round_id=b1, action_id=aid)
        n.wait(f"document.querySelector('.me-chip').textContent.endsWith('· {expect}')", what="Noora score")
        rep = op.act(code, "feud.award", {"playerId": pid["Noora"]}, round_id=b1, action_id=aid)
        again = op.act(code, "feud.award", {"playerId": pid["Aisha"]}, round_id=b1)
        sc = {q["id"]: q["score"] for q in op.state(code)["players"]}
        check("award: server sums revealed points for Noora; replay returns replayed=true; fresh award refused round_awarded; scores unchanged",
              aw["status"] == 200 and rep["replayed"] is True and again["status"] == 409 and again["result"]["error"]["code"] == "round_awarded"
              and sc == {pid["Noora"]: expect, pid["Omar"]: 0, pid["Aisha"]: 0}, sc)
        bad = a.raw_act("player", code, "feud.buzz", {}, round_id="g1b9")
        check("wrong-round buzz refused 409 stale_round", bad["status"] == 409 and bad["body"]["error"]["code"] == "stale_round", bad)
        bad = a.raw_act("player", code, "feud.award", {"playerId": pid["Aisha"]}, round_id=b1)
        check("player cannot award (403 forbidden)", bad["status"] == 403, bad)
        feud_privacy("board 1 awarded", board)
        n.shot(out, "05-lan-player-feud-awarded-360")

        op.act(code, "round.next", {}, round_id=b1)
        hs = op.state(code)
        b2, board2 = hs["roundId"], hs["game"]["answers"]
        check("next question: g1b2, strikes 0, buzzer closed", b2 == "g1b2" and hs["game"]["strikes"] == 0 and hs["game"]["roundPhase"] == "closed", b2)
        feud_privacy("board 2 closed", board2)
        races = []
        for race in range(3):
            op.act(code, "feud.open_buzzer", {}, round_id=b2)
            for p in players.values():
                p.wait("document.getElementById('buzzer').classList.contains('state-open')", what="open")
                p.eval("window.__rxMark = window.__rx.length; window.__clickMark = window.__clicks.length; true")
            barrier, stamps = threading.Barrier(3), {}

            def fire(nm, p):
                info = p.eval("(() => __aim(document.getElementById('buzzer'), 'center'))()")
                barrier.wait()
                stamps[nm] = time.time()
                p._press(info["x"], info["y"], "touch")
            th = [threading.Thread(target=fire, args=(nm, p)) for nm, p in players.items()]
            [t.start() for t in th]
            [t.join() for t in th]
            for p in players.values():
                p.wait("!document.getElementById('buzzer').classList.contains('state-open') && !document.getElementById('buzzer').classList.contains('state-sending')", what="settled")
            time.sleep(0.3)
            winner = op.state(code)["game"]["buzzer"]["winner"]
            outcomes = {}
            for nm, p in players.items():
                acts = [x for x in p.rx()[p.eval("window.__rxMark"):] if x["path"].endswith("/actions")]
                outcomes[nm] = [(x["status"], json.loads("".join(x["chunks"]) or "{}").get("accepted"), (json.loads("".join(x["chunks"]) or "{}").get("error") or {}).get("code")) for x in acts]
            taps = {nm: p.eval("window.__clicks.slice(window.__clickMark).map(c => c.type + '/' + c.trusted + '/' + c.pointerType + '/' + c.id)") for nm, p in players.items()}
            views = {nm: p.last_snap()["game"]["buzzer"]["winner"] for nm, p in players.items()}
            won = [nm for nm, p in players.items() if p.eval("document.getElementById('buzzer').classList.contains('state-won')")]
            spread = max(stamps.values()) - min(stamps.values())
            accepted = [nm for nm, oc in outcomes.items() if any(x[1] is True for x in oc)]
            races.append({"winner": name_of.get(winner), "outcomes": outcomes, "won_ui": won, "taps": taps, "dispatch_spread_s": round(spread, 4)})
            check(f"race {race + 1}: three trusted touches within {spread * 1000:.1f} ms, exactly one accepted by the server, every screen agrees",
                  winner is not None and accepted == [name_of[winner]] and won == [name_of[winner]] and set(views.values()) == {winner}
                  and all(t[:1] == ["pointerdown/true/touch/buzzer"] for t in taps.values())
                  and all(len(oc) <= 1 for oc in outcomes.values())
                  and all(oc[0][0] == 409 and oc[0][2] in ("buzzer_taken", "buzzer_closed") for nm, oc in outcomes.items() if oc and nm != name_of[winner]), races[-1])
            if race < 2:
                op.act(code, "feud.reset_buzzer", {}, round_id=b2)
        results["races"] = races
        results["contested_races"] = sum(1 for x in races if sum(1 for oc in x["outcomes"].values() if oc) == 3)
        check("at least one buzz race was contested at the server (3 buzzes, 1 winner)", results["contested_races"] >= 1, results["contested_races"])
        win2 = op.state(code)["game"]["buzzer"]["winner"]
        op.act(code, "feud.reveal", {"answerId": board2[0]["id"]}, round_id=b2)
        op.act(code, "feud.award", {"playerId": win2}, round_id=b2)
        players[name_of[win2]].shot(out, "06-lan-player-race-winner")
        o.call("Emulation.setDeviceMetricsOverride", width=LANDSCAPE["w"], height=LANDSCAPE["h"], deviceScaleFactor=3, mobile=True)
        o.vp = LANDSCAPE
        check("734x393 landscape: no horizontal overflow on the LAN player page", not o.eval("__overflow()"))
        o.shot(out, "07-lan-player-landscape-734")
        n.reload()
        n.wait("!!document.querySelector('.feud-board')", what="Noora after refresh")
        check("player refresh mid-Feud keeps seat and score", n.last_snap()["self"]["id"] == pid["Noora"]
              and [q["score"] for q in n.last_snap()["players"] if q["id"] == pid["Noora"]] == [q["score"] for q in op.state(code)["players"] if q["id"] == pid["Noora"]])
        op.act(code, "room.end")
        final = {q["id"]: q["score"] for q in op.state(code)["players"]}
        expect_final = {pid["Noora"]: expect, pid["Omar"]: 0, pid["Aisha"]: 0}
        expect_final[win2] += board2[0]["points"]
        for p in players.values():
            p.wait("document.body.textContent.includes('Game over')", what="feud over")
        check("final Feud scores equal the awards made and agree on every phone",
              final == expect_final and all({q["id"]: q["score"] for q in p.last_snap()["players"]} == final for p in players.values()), final)

        # Close after the game: the room refuses further use.
        cl = op.act(code, "room.close")
        post = a.raw_act("player", code, "feud.buzz", {}, round_id="g1b2")
        check("closed room refuses further player use (409 room_closed)", cl["status"] == 200 and post["status"] == 409 and post["body"]["error"]["code"] == "room_closed", post)
        fr = op.call({"op": "forget", "code": code})
        st = op.call({"op": "state", "code": code})
        check("bridge forget drops the operator credential (state now refused)", fr["ok"] and st["ok"] is False, st.get("error"))

        # Lobby close with a live stream: the phone is told, drops its seat, does not reconnect.
        code = op.call({"op": "create", "body": {}})["code"]
        codes.append(code)
        o.goto(f"{base}/play.html?code={code}")
        o.type_into("#join-name", "Omar")
        o.click("__btn('Join game')", "Join game 3")
        o.wait("document.body.textContent.includes('You’re in')", what="Omar lobby 3")
        tokens[f"player:Omar:{code}"] = json.loads(o.storage()[f"majlisParty.v1.player.{code}"])["token"]
        op.act(code, "room.close")
        o.wait("document.body.textContent.includes('Room closed')", what="Omar told")
        n_ev = sum(1 for t in o.eval("window.__tx") if t["path"].endswith("/events"))
        time.sleep(2.5)
        n_ev2 = sum(1 for t in o.eval("window.__tx") if t["path"].endswith("/events"))
        check("lobby close via bridge: phone told 'Room closed', seat dropped, no reconnect",
              f"majlisParty.v1.player.{code}" not in o.storage() and n_ev == n_ev2, {"events_before": n_ev, "after": n_ev2})
        o.shot(out, "08-lan-player-room-closed")
        op.call({"op": "forget", "code": code})

        # ------------------------------------------------ credential and leak scans
        for t in tabs.values():
            for e in t.events:
                if e.get("method") == "Network.requestWillBeSent":
                    rq = e["params"]["request"]
                    net_log.append({"tab": t.name, "method": rq["method"], "url": rq["url"],
                                    "headers": {k: ("<redacted bearer>" if k.lower() == "authorization" else v) for k, v in rq.get("headers", {}).items()},
                                    "hadAuthorization": any(k.lower() == "authorization" for k in rq.get("headers", {}))})
        all_rx = "\n".join(decoded_text(t.rx_raw()) for t in players.values())
        rx_keys = set()
        for t in players.values():
            for snap in t.snaps():
                keys_deep(snap, rx_keys)
        check("no operator/display credential field anywhere in player-received JSON/SSE",
              not ({"hostToken", "displayToken"} & rx_keys) and "hostToken" not in all_rx and "displayToken" not in all_rx, sorted(k for k in rx_keys if "oken" in k))
        roles = {s.get("role") for t in players.values() for s in t.snaps()}
        check("every snapshot the network delivered is a player projection", roles == {"player"}, sorted(map(str, roles)))
        server_log = open(os.path.join(out, "server-stderr.log")).read()
        bridge = "\n".join(op.raw)
        leaks = []
        for k, tok in tokens.items():
            for t in tabs.values():
                if tok in t.eval("document.documentElement.outerHTML + location.href") or tok in t.console_text():
                    leaks.append((k, t.name))
        check("no player token in any DOM, URL or console", not leaks, leaks)
        check("no player token in any request URL", not any(tok in r_["url"] for tok in tokens.values() for r_ in net_log))
        check("tokens travelled only in the Authorization header", any(r_["hadAuthorization"] for r_ in net_log)
              and not any(tok in json.dumps(r_["headers"]) for tok in tokens.values() for r_ in net_log))
        check("no token in the server access log; access log is non-empty", len(server_log) > 0 and not any(tok in server_log for tok in tokens.values()))
        check("access log has route templates only (no room code in paths)", not any(c in server_log for c in codes), server_log.splitlines()[:2])
        check("operator bridge replies carry no token field and no player token",
              not any("token" in k.lower() for ln in op.raw for k in keys_deep(json.loads(ln), set())) and not any(tok in bridge for tok in tokens.values()))
        check("no page script errors on any phone", not any(t.page_errors() for t in players.values()), {k: t.page_errors()[:2] for k, t in players.items() if t.page_errors()})
        results["token_sha256"] = {k: sha(v) for k, v in tokens.items()}
        json.dump(net_log, open(os.path.join(out, "cdp-requests-redacted.json"), "w"), indent=1)
        json.dump({t.name: [{k: v for k, v in r_.items()} for r_ in t.rx()] for t in players.values()},
                  open(os.path.join(out, "wire-received.json"), "w"), ensure_ascii=False, indent=1)
    except Exception:
        import traceback
        check("run completed without an exception", False, traceback.format_exc()[-1500:])
    finally:
        for t in list(tabs.values()):
            try: t.ws.close()
            except Exception: pass
        chrome.terminate()
        try: chrome.wait(10)
        except Exception: chrome.kill()
        try:
            srv.stdin.close()
            srv.wait(10)
        except Exception:
            srv.kill()
        results["server_exit"] = srv.returncode
        results["screenshots"] = R.results["screenshots"]
        # Evidence holds token hashes only: redact any token that reached a written file.
        for fn in os.listdir(out):
            pth = os.path.join(out, fn)
            if fn.endswith((".json", ".jsonl", ".log")):
                txt = open(pth, encoding="utf-8").read()
                red = txt
                for k, tok in tokens.items():
                    red = red.replace(tok, "<token sha256=" + sha(tok)[:16] + ">")
                if red != txt:
                    open(pth, "w", encoding="utf-8").write(red)
    p_ = sum(1 for c in results["checks"] if c["pass"])
    f_ = len(results["checks"]) - p_
    results["summary"] = {"pass": p_, "fail": f_, "total": len(results["checks"])}
    json.dump(results, open(os.path.join(out, "lan-results.json"), "w"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps(results["summary"]))
    sys.exit(0 if f_ == 0 and p_ > 0 else 1)


if __name__ == "__main__":
    main()
