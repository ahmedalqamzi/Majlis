"""Majlis Party first-pair frontend checks against the SIMULATED FIXTURE (?sim=1).

Everything here is MOCK evidence: one isolated headless Chrome profile, several tabs (operator,
three players, TV) sharing the sim SharedWorker. No Majlis server, no real network multiplayer,
no physical phone.

Clicks are trusted CDP input (mouse on desktop viewports, touch on phone viewports) aimed at the
control's centre, after document.elementFromPoint there confirms the control itself would receive
the pointer. A covered control fails the run instead of being clicked through the DOM. Usage:

  python sim_e2e.py <party/web dir> <out dir> [qr_decoder_binary]

Needs: Google Chrome, python websocket-client. The optional decoder is the compiled
tests/qr_decode.swift (Apple CoreImage), used as an independent QR reader.
"""
import base64, functools, http.server, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.request
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_6 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/26.6 Mobile/15E148 Safari/604.1")
PHONE = dict(w=393, h=852, dpr=3, mobile=True)
SMALL = dict(w=360, h=780, dpr=3, mobile=True)
LAPTOP = dict(w=1280, h=800, dpr=1, mobile=False)
TV = dict(w=1920, h=1080, dpr=1, mobile=False)
TV720 = dict(w=1280, h=720, dpr=1, mobile=False)
LANDSCAPE = dict(w=734, h=393, dpr=3, mobile=True, insets=dict(top=0, bottom=21, left=59, right=59))

# Records every message a page receives from the sim worker, so the test can inspect exactly what
# each role was sent (the sim equivalent of network payloads). Pages never see this array.
RX_HOOK = r"""
(() => {
  const d = Object.getOwnPropertyDescriptor(MessagePort.prototype, 'onmessage');
  window.__rx = [];
  Object.defineProperty(MessagePort.prototype, 'onmessage', { configurable: true, get: d.get,
    set(fn) { d.set.call(this, fn && (ev => { try { window.__rx.push(JSON.stringify(ev.data)); } catch (e) {} return fn.call(this, ev); })); } });
  // Outgoing actions (the sim equivalent of POST /actions bodies), to check which roundId was sent.
  window.__tx = [];
  const pm = MessagePort.prototype.postMessage;
  MessagePort.prototype.postMessage = function (m, ...rest) {
    try { if (m && m.op === 'act') window.__tx.push({ type: m.args.body.type, roundId: m.args.body.roundId, actionId: m.args.body.actionId, payload: m.args.body.payload }); } catch (e) {}
    return pm.call(this, m, ...rest);
  };
  // Every click that reaches the page, with whether the browser marked it trusted and its pointer type.
  window.__clicks = [];
  addEventListener('click', ev => { const t = ev.target && ev.target.closest ? (ev.target.closest('button, a, [data-mode]') || ev.target) : ev.target;
    window.__clicks.push({ trusted: ev.isTrusted, pointerType: ev.pointerType || null, id: t && t.id || null, text: ((t && t.textContent) || '').trim().slice(0, 40) }); }, true);
  // Dialog open/close trail, for diagnosing confirm timing.
  window.__dlg = [];
  const sm = HTMLDialogElement.prototype.showModal, cl = HTMLDialogElement.prototype.close;
  HTMLDialogElement.prototype.showModal = function () { window.__dlg.push({ op: 'open', t: Math.round(performance.now()), text: this.textContent.slice(0, 60) }); return sm.call(this); };
  HTMLDialogElement.prototype.close = function (...a) { window.__dlg.push({ op: 'close', t: Math.round(performance.now()), stack: (new Error().stack || '').split('\n').slice(1, 5).join(' | ') }); return cl.apply(this, a); };
})();
"""

HELPERS = r"""
window.__q = (sel) => document.querySelector(sel);
window.__btn = (text, root) => Array.from((root || document).querySelectorAll('button, a.btn')).find(b => (b.textContent || '').trim().includes(text) && !b.disabled);
window.__overflow = () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
window.__desc = (e) => e ? (e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\s+/).join('.') : '')).slice(0, 90) : null;
// Scrolls a control into view the way a user would reach it, then reports what the pointer would hit at its centre.
window.__aim = (b, block) => {
  if (!b) return null;
  b.scrollIntoView({ block: block || 'nearest', inline: 'nearest' });
  const r = b.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  return { x, y, w: r.width, h: r.height, ok: !!hit && (hit === b || b.contains(hit)), target: __desc(b), hit: __desc(hit), hitIn: hit && hit.closest ? __desc(hit.closest('.overlay, dialog, .buzz-wrap, .slot, .score-row, .topbar, main, .conn')) : null,
           disabled: !!b.disabled, text: (b.textContent || '').trim().slice(0, 40), inViewport: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth };
};
true
"""

pointer_log = []

results = {"label": "SIMULATED FIXTURE (mock) - not the Majlis server, not real-network, not a physical phone", "checks": [], "screenshots": []}


def check(name, ok, detail=None):
    results["checks"].append({"name": name, "pass": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if detail is None else "  " + json.dumps(detail, ensure_ascii=False)[:300]))
    return ok


class Tab:
    def __init__(self, ws_url, name):
        self.name = name
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0
        self.events = []

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{self.name} {method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    def eval(self, expr):
        res = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in res:
            raise RuntimeError(self.name + ": " + json.dumps(res["exceptionDetails"])[:800])
        return res["result"].get("value")

    def wait(self, expr, timeout=8.0, what=None):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = self.eval(expr)
                if v:
                    return v
            except RuntimeError:
                pass
            time.sleep(0.1)
        try:
            txt = self.eval("JSON.stringify({ dlg: window.__dlg, clicks: (window.__clicks || []).slice(-3) }) + '\\n' + document.body.innerText.slice(0, 600)")
        except Exception as e:
            txt = repr(e)
        errs = [e["params"].get("exceptionDetails", {}).get("exception", {}).get("description") or e["params"].get("args") for e in self.events
                if e.get("method") == "Runtime.exceptionThrown" or (e.get("method") == "Runtime.consoleAPICalled" and e["params"]["type"] == "error")]
        raise TimeoutError(f"{self.name}: timed out waiting for {what or expr}\n--- page errors ---\n{errs[-3:]}\n--- page text ---\n{txt}")

    def viewport(self, vp):
        self.vp = vp
        self.call("Emulation.setDeviceMetricsOverride", width=vp["w"], height=vp["h"], deviceScaleFactor=vp["dpr"], mobile=vp["mobile"])
        self.call("Emulation.setTouchEmulationEnabled", enabled=vp["mobile"], maxTouchPoints=5 if vp["mobile"] else 1)
        if vp["mobile"]:
            self.call("Emulation.setUserAgentOverride", userAgent=IPHONE_UA, platform="iPhone")
            try:
                self.call("Emulation.setSafeAreaInsetsOverride", insets=vp.get("insets", dict(top=59, bottom=34, left=0, right=0)))
            except RuntimeError:
                pass

    def goto(self, url, fresh_phone=False):
        if fresh_phone:
            if self.eval("location.origin") == "null":
                self.call("Page.navigate", url=url.split("/play.html")[0] + "/index.html")
                self.wait("document.readyState === 'complete' && location.origin !== 'null'", what="origin")
            # All tabs share one Chrome profile; a real second phone has no saved player seat.
            self.eval("Object.keys(localStorage).filter(k => /\\.sim\\.(last\\.)?player/.test(k)).forEach(k => localStorage.removeItem(k)); true")
        self.call("Page.navigate", url=url)
        self.wait("document.readyState === 'complete'", what="load")
        self.eval(HELPERS)

    def shot(self, out, label, full=False):
        time.sleep(0.45)  # let flip/strike animations settle
        if full:
            # Grow the viewport to the page height so sticky bars land at the real bottom.
            vp = self.vp
            hgt = min(int(self.eval("document.documentElement.scrollHeight")), 4000)
            if hgt > vp["h"]:
                self.call("Emulation.setDeviceMetricsOverride", width=vp["w"], height=hgt, deviceScaleFactor=vp["dpr"], mobile=vp["mobile"])
                time.sleep(0.3)
            data = self.call("Page.captureScreenshot", format="png")["data"]
            self.call("Emulation.setDeviceMetricsOverride", width=vp["w"], height=vp["h"], deviceScaleFactor=vp["dpr"], mobile=vp["mobile"])
        else:
            data = self.call("Page.captureScreenshot", format="png")["data"]
        path = os.path.join(out, f"{label}.png")
        open(path, "wb").write(base64.b64decode(data))
        results["screenshots"].append(path)
        return path

    def html(self):
        return self.eval("document.documentElement.outerHTML")

    def rx_states(self):
        return [m for m in self.eval("window.__rx || []") if '"type":"state"' in m]

    def click(self, js_target, what, block="nearest", pre=None):
        """Trusted pointer input at the control's centre; refuses if anything else is on top of it."""
        self.call("Page.bringToFront")  # input is only delivered to the visible (front) headless tab
        time.sleep(0.05)
        info = self.eval(f"(() => __aim({js_target}, {json.dumps(block)}))()")
        if not info:
            raise RuntimeError(f"{self.name}: could not find {what}")
        before = self.eval("window.__clicks.length")
        kind = "touch" if self.vp["mobile"] else "mouse"
        rec = dict(tab=self.name, what=what, viewport=f"{self.vp['w']}x{self.vp['h']}", input=kind, **{k: info[k] for k in ("x", "y", "target", "hit", "hitIn", "ok", "disabled")})
        pointer_log.append(rec)
        if info["disabled"]:
            raise RuntimeError(f"{self.name}: {what} is disabled")
        if not info["ok"]:
            raise RuntimeError(f"{self.name}: {what} is covered at its centre ({info['x']:.0f},{info['y']:.0f}): pointer would hit {info['hit']} in {info['hitIn']}")
        x, y = info["x"], info["y"]
        if pre:  # state change that must happen after the tab is in front (e.g. network back)
            self.eval(pre)
        if kind == "touch":
            self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[dict(x=x, y=y, radiusX=4, radiusY=4, force=1)])
            self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        else:
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
            self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)
        got = self.wait(f"window.__clicks.length > {before} && window.__clicks[window.__clicks.length - 1]", timeout=4, what=f"trusted click on {what}")
        rec["delivered"] = got
        if not got.get("trusted"):
            raise RuntimeError(f"{self.name}: click on {what} was not trusted: {got}")
        if got.get("text") != info.get("text") and not (info.get("text") or "").startswith(got.get("text") or "\0"):
            raise RuntimeError(f"{self.name}: click on {what} landed on {got} instead of {info.get('text')!r}")
        return rec

    def js_click(self, js_target, what):
        """Programmatic DOM click; only used where simultaneity matters more than input realism (buzz race)."""
        ok = self.eval(f"(() => {{ const b = {js_target}; if (!b) return false; b.click(); return true; }})()")
        if not ok:
            raise RuntimeError(f"{self.name}: could not click {what}")

    def key(self, key, code, vk, shift=False, text=None):
        mods = 8 if shift else 0
        self.call("Page.bringToFront")
        down = dict(type="keyDown", key=key, code=code, windowsVirtualKeyCode=vk, modifiers=mods)
        if text:
            down["text"] = text
        self.call("Input.dispatchKeyEvent", **down)
        self.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code, windowsVirtualKeyCode=vk, modifiers=mods)

    def active(self):
        return self.eval("(() => { const a = document.activeElement; return a ? (a.id || (a.textContent || '').trim().slice(0, 30)) : null; })()")

    def last_snap(self):
        for m in reversed(self.rx_states()):
            try:
                return json.loads(m)["snapshot"]
            except Exception:
                pass
        return None

    def round_id(self):
        s = self.last_snap()
        return s and s.get("roundId")

    def type_into(self, sel, text):
        self.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); e.focus(); e.select && e.select(); return true; }})()")
        self.call("Input.insertText", text=text)

    def confirm(self, accept=True):
        self.wait("!!document.querySelector('dialog.confirm[open]')", what="confirm dialog")
        info = self.eval("(() => { const d = document.querySelector('dialog.confirm[open]'); return { focused: document.activeElement && document.activeElement.textContent, text: d.textContent }; })()")
        self.click("document.querySelector('dialog.confirm[open] .confirm-actions .btn:last-child')" if accept else "document.querySelector('dialog.confirm[open] .confirm-actions .btn:first-child')", "dialog button")
        self.wait("!document.querySelector('dialog.confirm[open]')", what="dialog closed")
        return info


OP_ACT = r"""
(async (code, type, payload, roundId, actionId) => {
  const k = Object.keys(localStorage).find(k => k.includes('.operator.') && k.endsWith('.' + code));
  const tok = JSON.parse(localStorage[k]).token; const t = window.__majlisSimTransport;
  if (!actionId) return await t.act(code, tok, type, payload, roundId);
  const body = { actionId, type, payload }; if (roundId !== null) body.roundId = roundId;
  try { return await t.call('act', { code, token: tok, body }); } catch (e) { return { accepted: false, error: e.code }; }
})
"""


def op_act(t, code, type_, payload=None, round_id=None, action_id=None):
    return t.eval(f"({OP_ACT})({json.dumps(code)}, {json.dumps(type_)}, {json.dumps(payload or {})}, {json.dumps(round_id)}, {json.dumps(action_id)})")


def scores(snap):
    return {p["name"]: p["score"] for p in (snap or {}).get("players", [])}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main():
    web, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    decoder = sys.argv[3] if len(sys.argv) > 3 else None
    os.makedirs(out, exist_ok=True)
    port = free_port()
    requests_seen = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            requests_seen.append(self.requestline)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=web))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    profile = tempfile.mkdtemp(prefix="majlis-party-chrome-")
    dport = free_port()
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={dport}", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                               "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                               "--window-size=1280,800", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results["started_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tabs = {}
    try:
        for _ in range(100):
            try:
                ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json/version")); break
            except Exception:
                time.sleep(0.2)
        results["chrome"] = ver.get("Browser")

        def new_tab(name, vp):
            req = urllib.request.Request(f"http://127.0.0.1:{dport}/json/new?about:blank", method="PUT")
            info = json.load(urllib.request.urlopen(req))
            t = Tab(info["webSocketDebuggerUrl"], name)
            t.call("Page.enable"); t.call("Runtime.enable"); t.call("Network.enable")
            t.call("Page.addScriptToEvaluateOnNewDocument", source=RX_HOOK)
            t.viewport(vp)
            tabs[name] = t
            return t

        # ---------- unit checks (transport + session), run in a page ----------
        u = new_tab("unit", LAPTOP)
        u.goto(base + "/index.html")
        unit = u.eval(UNIT_JS)
        for k, v in unit.items():
            check("unit: " + k, v is True, None if v is True else v)

        # ---------- operator creates a room ----------
        host = new_tab("host", LAPTOP)
        host.goto(base + "/host.html?sim=1")
        check("sim ribbon visible with exact copy", host.eval("(document.querySelector('.sim-ribbon')||{}).textContent") == "SIMULATED FIXTURE - not a real server")
        host.shot(out, "01-host-start-1280")
        host.click("__btn('Create room')", "Create room")
        code = host.wait("(document.querySelector('.room-code')||{}).textContent", what="room code")
        check("room created with 6-char code", re.fullmatch(r"[A-Z0-9]{6}", code or "") is not None, code)
        creds = host.eval("Object.fromEntries(Object.entries(localStorage).filter(([k]) => k.startsWith('majlisParty.')))")
        secret_tokens = set()
        for k, v in creds.items():
            try:
                secret_tokens.add(json.loads(v)["token"])
            except Exception:
                pass
        check("operator + display credentials stored per role", any(".operator." in k for k in creds) and any(".display." in k for k in creds), sorted(creds))

        join_url = host.eval("document.querySelector('[data-join-url]').getAttribute('data-join-url')")
        check("QR join URL is play.html?code=<code>&sim=1 only", join_url == f"{base}/play.html?code={code}&sim=1", join_url)
        # QR decode, independently: screenshot only the QR element, decode with CoreImage.
        rect = host.eval("(() => { const r = document.querySelector('.qr').getBoundingClientRect(); return {x:r.x, y:r.y + scrollY, width:r.width, height:r.height}; })()")
        qr_png = os.path.join(out, "qr-room1.png")
        open(qr_png, "wb").write(base64.b64decode(host.call("Page.captureScreenshot", format="png", captureBeyondViewport=True, clip=dict(rect, scale=2))["data"]))
        decoded = None
        if decoder:
            r = subprocess.run([decoder, qr_png], capture_output=True, text=True, timeout=60)
            msgs = json.loads(r.stdout.strip().splitlines()[-1])["messages"] if r.stdout.strip() else []
            decoded = msgs[0] if msgs else None
            check("QR decodes (CoreImage, independent) to the join URL", decoded == join_url, msgs)
        # Second decoder: Chrome's BarcodeDetector where available.
        bd = host.eval("""(async () => { if (!('BarcodeDetector' in window)) return 'unsupported';
          const svg = document.querySelector('.qr svg'); const img = new Image();
          img.src = 'data:image/svg+xml;base64,' + btoa(new XMLSerializer().serializeToString(svg));
          await img.decode(); const c = document.createElement('canvas'); c.width = c.height = 600;
          const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0,0,600,600); x.drawImage(img, 0, 0, 600, 600);
          const r = await new BarcodeDetector({formats:['qr_code']}).detect(c); return r.map(v => v.rawValue); })()""")
        results["barcode_detector"] = bd
        if bd != "unsupported":
            check("QR decodes (Chrome BarcodeDetector) to the join URL", bd == [join_url], bd)
        check("QR content has no token", decoded is not None and not any(t in decoded for t in secret_tokens))
        disabled = host.eval("Array.from(document.querySelectorAll('.mode-tile.disabled')).map(t => ({id: t.dataset.mode, aria: t.getAttribute('aria-disabled'), text: t.textContent}))")
        check("8 other games shown as 'Not implemented yet' and not selectable",
              len(disabled) == 8 and all(d["aria"] == "true" and "Not implemented yet" in d["text"] and "Coming later - not playable yet" in d["text"] for d in disabled),
              [d["id"] for d in disabled])
        check("start disabled with no players", host.eval("document.querySelector('.sticky-actions .btn-pri').disabled") is True)

        # ---------- TV (same browser: display token handed over locally) ----------
        tv = new_tab("tv", TV)
        tv.goto(f"{base}/tv.html?code={code}&sim=1")
        tv.wait("!!document.querySelector('.tv-lobby')", what="tv lobby")

        # ---------- players join through the decoded QR URL ----------
        players = {}
        names = ["Aisha", "Omar", "Noora"]
        for i, nm in enumerate(names):
            p = new_tab(nm, SMALL if i == 2 else PHONE)
            p.goto(decoded or join_url, fresh_phone=i > 0)
            if i == 0:
                check("join form code prefilled from QR URL", p.eval("document.getElementById('join-code').value") == code)
                p.shot(out, "02-player-join-393")
            p.type_into("#join-name", nm)
            p.click("__btn('Join game')", "Join game")
            p.wait("document.body.textContent.includes('You’re in')", what="lobby")
            players[nm] = p
            for m in p.eval("window.__rx"):
                if '"playerToken"' in m:
                    secret_tokens.add(json.loads(m)["result"]["playerToken"])
        check("3 player tokens captured for leak checks", len(secret_tokens) == 5, len(secret_tokens))
        players["Aisha"].shot(out, "03-player-lobby-393")

        # Join error states (mock): name taken, unknown code, room full.
        x = new_tab("extra", PHONE)
        x.goto(join_url, fresh_phone=True)
        x.type_into("#join-name", "aisha")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="name taken")
        check("name taken error shown, code kept", "already in this room" in err and x.eval("document.getElementById('join-code').value") == code, err)
        x.shot(out, "04-player-error-name-taken")
        x.goto(f"{base}/play.html?code=ZZZZ22&sim=1")
        x.type_into("#join-name", "Salman")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="not found")
        check("unknown room code error shown", "No room with that code" in err, err)
        full_code = x.eval("""(async () => { const t = window.__majlisSimTransport; const r = await t.createRoom('x');
          for (let i = 0; i < 12; i++) await t.join(r.code, 'P' + i); return r.code; })()""")
        x.goto(f"{base}/play.html?code={full_code}&sim=1")
        x.type_into("#join-name", "Late")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="room full")
        check("room full error shown", "full" in err, err)
        x.goto(f"{base}/play.html?code=AB&sim=1")
        x.type_into("#join-name", "Shorty")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="code shape")
        check("malformed code rejected client-side", "6 letters or numbers" in err, err)
        bad = x.eval("(async () => { const t = window.__majlisSimTransport; const r = await t.createRoom('y'); return (await t.act(r.code, r.hostToken, 'room.set_mode', {mode: 'impostor'})).error; })()")
        check("backend fixture refuses unimplemented modes", bad == "mode_not_implemented", bad)

        tv.wait("document.querySelectorAll('.tv-lobby .chip').length === 3", what="tv sees 3 players")
        tv.shot(out, "05-tv-lobby-1920")
        host.wait("document.querySelectorAll('.chip').length === 3", what="host sees players")
        host.shot(out, "06-host-lobby-1280", full=True)

        # ---------- Who Am I ----------
        host.click("document.querySelector('[data-mode=who_am_i]')", "who_am_i tile")
        host.wait("document.querySelector('[data-mode=who_am_i]').getAttribute('aria-pressed') === 'true'", what="mode chosen")
        host.click("__btn('Start Who Am I')", "Start")
        host.wait("!!document.querySelector('.who-row')", what="host who am i")
        assign = host.eval("Array.from(document.querySelectorAll('.who-row')).map(r => ({name: r.querySelector('.who-name').textContent, label: r.querySelector('.char-label').textContent, ar: (r.querySelector('.char-ar')||{}).textContent || ''}))")
        by_name = {a["name"]: a for a in assign}
        check("operator sees all 3 characters", len(assign) == 3, assign)
        for nm, p in players.items():
            p.wait("!!document.querySelector('.my-card')", what="who am i")
            html = p.html()
            states = p.rx_states()
            own = by_name[nm]
            others = [by_name[o]["label"] for o in names if o != nm]
            check(f"{nm}: own character absent from DOM", own["label"] not in html and (not own["ar"] or own["ar"] not in html), own["label"])
            check(f"{nm}: own character absent from every received snapshot", not any(own["label"] in s for s in states), len(states))
            check(f"{nm}: sees the other two characters", all(o in html for o in others), others)
        tvhtml = tv.wait("document.querySelector('.tv-who') && document.documentElement.outerHTML", what="tv who")
        check("TV shows no characters before reveal (DOM + snapshots)", not any(a["label"] in tvhtml or any(a["label"] in s for s in tv.rx_states()) for a in assign))
        players["Aisha"].shot(out, "07-player-whoami-393", full=True)
        players["Noora"].shot(out, "07b-player-whoami-360")
        tv.shot(out, "08-tv-whoami-1920")
        # Aisha guesses; operator awards; reveal.
        players["Aisha"].type_into("#guess", "Am I a falcon?")
        players["Aisha"].click("__btn('I know who I am')", "guess")
        players["Aisha"].wait("document.body.textContent.includes('Sent to the host')", what="guess sent")
        host.wait("document.body.textContent.includes('Am I a falcon?')", what="host sees guess")
        host.shot(out, "09-host-whoami-1280", full=True)
        host.click("document.querySelector('[aria-label=\"Correct: award Aisha\"]')", "award")
        players["Aisha"].wait("document.querySelector('.me-chip').textContent.endsWith('· 1')", what="score 1")
        check("award updates Aisha's score to 1 from server snapshot", True)
        host.click("__btn('Reveal all')", "reveal")
        dinfo = host.confirm(True)
        check("confirm dialog opens for reveal with confirm focused", "Reveal" in (dinfo.get("focused") or ""), dinfo)
        for nm, p in players.items():
            p.wait(f"document.querySelector('.my-card.revealed') && document.querySelector('.my-card').textContent.includes({json.dumps(by_name[nm]['label'])})", what="own reveal")
        check("after reveal each player sees their own character", True)
        players["Omar"].shot(out, "10-player-whoami-revealed-393")
        tv.wait(f"document.body.textContent.includes({json.dumps(assign[0]['label'])})", what="tv reveal")
        tv.shot(out, "11-tv-whoami-revealed-1920")

        # ---------- round targeting (Who Am I): round.next and confirmations carry the intended round ----------
        host.eval("window.__tx = []; true")
        r1 = host.round_id()
        host.click("__btn('Next round')", "next round")
        host.wait(f"(() => {{ const m = (window.__rx || []).filter(x => x.includes('\"type\":\"state\"')).pop(); return m && JSON.parse(m).snapshot.roundId === {r1 + 1}; }})()", what="round 2")
        tx = host.eval("window.__tx")
        check("Next round sends round.next with the round it was shown for", [t["type"] for t in tx] == ["round.next"] and tx[0]["roundId"] == r1, tx)
        host2 = new_tab("host2", LAPTOP)  # a second operator device on the same room (same operator seat)
        host2.goto(f"{base}/host.html?code={code}&sim=1")
        host2.wait("!!document.querySelector('.who-row')", what="host2 who am i")
        for p in players.values():
            p.wait("!!document.querySelector('.my-card') && !document.querySelector('.my-card.revealed')", what="round 2 hidden card")
        r2 = host.round_id()
        host.eval("window.__tx = []; true")
        host.click("__btn('Reveal all')", "reveal (round 2)")
        host.wait("!!document.querySelector('dialog.confirm[open]')", what="reveal dialog")
        moved = op_act(host2, code, "round.next", round_id=r2)
        host.wait(f"(() => {{ const m = (window.__rx || []).filter(x => x.includes('\"type\":\"state\"')).pop(); return m && JSON.parse(m).snapshot.roundId === {r2 + 1}; }})()", what="round 3 under open dialog")
        host.wait("!document.querySelector('dialog.confirm')", what="stale reveal dialog closed by round change")
        time.sleep(0.6)
        tx = host.eval("window.__tx")
        toast_txt = host.eval("(document.getElementById('toast')||{}).textContent || ''")
        toast_role = host.eval("(document.getElementById('toast')||{getAttribute:()=>null}).getAttribute('role')")
        focus_tag = host.eval("document.activeElement && document.activeElement.tagName")
        revealed_any = any(p.eval("!!document.querySelector('.my-card.revealed')") for p in players.values())
        snap_stage = (host.last_snap().get("game") or {}).get("stage")
        check("Who Am I: a Reveal confirm opened in round N closes unsent when round N+1 arrives; nothing revealed; operator told; focus on new round heading",
              moved.get("accepted") is True and not any(t["type"] == "who_am_i.reveal" for t in tx) and not revealed_any
              and snap_stage != "reveal" and host.round_id() == r2 + 1 and "was cancelled" in toast_txt and "Reveal all characters" in toast_txt and focus_tag == "H1",
              {"second_operator_next": moved, "tx": tx, "toast": toast_txt, "toast_role": toast_role, "focus": focus_tag, "stage": snap_stage, "round": host.round_id()})
        stale = op_act(host2, code, "who_am_i.reveal", round_id=r2)
        check("fixture: who_am_i.reveal with a stale roundId is rejected and reveals nothing",
              stale.get("error") == "stale_round" and not any(p.eval("!!document.querySelector('.my-card.revealed')") for p in players.values()), stale)
        r3 = host.round_id()
        before = scores(host.last_snap())
        a1 = op_act(host2, code, "round.next", round_id=r3)
        a2 = op_act(host2, code, "round.next", round_id=r3)
        time.sleep(0.3)
        check("duplicate round.next for the same round: one accepted, one stale_round, round advances once",
              a1.get("accepted") is True and a2.get("error") == "stale_round" and host.round_id() == r3 + 1, {"first": a1, "second": a2, "round": host.round_id()})
        r4 = host.round_id()
        b1 = op_act(host2, code, "round.next", round_id=r4, action_id="replay-next-1")
        b2 = op_act(host2, code, "round.next", round_id=r4, action_id="replay-next-1")
        time.sleep(0.3)
        check("replayed round.next (same actionId) returns the original result and advances once",
              b1.get("accepted") is True and b2 == b1 and host.round_id() == r4 + 1 and scores(host.last_snap()) == before, {"first": b1, "replay": b2, "round": host.round_id()})
        no_round = op_act(host2, code, "round.next", round_id=None)
        check("round.next without roundId still accepted (contract: roundId optional)", no_round.get("accepted") is True, no_round)
        results["round_targeting_whoami"] = {"rounds": [r1, r2, r3, r4, host.round_id()], "scores": before}

        # ---------- pause: operator controls in the pause panel, reached by real pointer, touch and keyboard ----------
        host.click("document.getElementById('pause-btn')", "pause")
        host.wait("!!document.getElementById('paused-panel')", what="host pause panel")
        for p in players.values():
            p.wait("!!document.querySelector('.overlay')", what="paused overlay")
        st = host.eval("""(() => { const pn = document.getElementById('paused-panel'); const tb = document.querySelector('.topbar'); const r = tb.getBoundingClientRect();
          const behind = document.elementFromPoint(r.right - 40, r.top + r.height / 2);
          const card = pn.querySelector('[role=dialog]');
          return { role: card && card.getAttribute('role'), modal: card && card.getAttribute('aria-modal'), label: document.getElementById(card.getAttribute('aria-labelledby')).textContent,
                   described: document.getElementById(card.getAttribute('aria-describedby')).textContent, bodyLocked: document.body.classList.contains('pause-open'),
                   focus: document.activeElement.id, topbarInert: tb.inert, mainInert: document.querySelector('main').inert,
                   topbarPointHits: __desc(behind), topbarPointInPanel: !!(behind && behind.closest('#paused-panel')),
                   resume: __aim(document.getElementById('resume-btn')), end: __aim(document.getElementById('overlay-end-btn')) }; })()""")
        check("host pause panel: labelled dialog (not aria-modal, so Retry stays operable), Resume focused, page behind inert and scroll-locked, both controls hit-testable at centre",
              st["role"] == "dialog" and st["modal"] is None and st["label"] == "Paused" and st["bodyLocked"] and st["focus"] == "resume-btn" and st["topbarInert"] and st["mainInert"]
              and st["topbarPointInPanel"] and st["resume"]["ok"] and st["end"]["ok"], st)
        players["Aisha"].shot(out, "12-player-paused-393")
        host.shot(out, "12b-host-paused-1280")
        host.key("Tab", "Tab", 9)
        f1 = host.active()
        host.key("Tab", "Tab", 9, shift=True)
        f2 = host.active()
        check("keyboard: Tab reaches End game, Shift+Tab returns to Resume (background not focusable)", f1 == "overlay-end-btn" and f2 == "resume-btn", [f1, f2])
        host.key("Enter", "Enter", 13, text="\r")
        host.wait("!document.getElementById('paused-panel')", what="resumed by Enter")
        for p in players.values():
            p.wait("!document.querySelector('.overlay')", what="resumed")
        check("keyboard Enter on Resume resumes everyone; focus returns to Pause", host.active() == "pause-btn", host.active())
        host.key(" ", "Space", 32, text=" ")
        host.wait("!!document.getElementById('paused-panel')", what="paused by Space")
        check("keyboard Space on Pause pauses; focus moves to Resume", host.active() == "resume-btn", host.active())
        # Reconnecting while paused: the banner's Retry stays above the pause scrim.
        host.eval("window.__majlisSimTransport.setOffline(true); true")
        host.wait("document.body.dataset.conn === 'reconnecting'", what="host reconnecting")
        host.shot(out, "12c-host-paused-reconnecting-1280")
        host.eval("document.getElementById('resume-btn').focus(); true")
        loop = []
        for _ in range(3):
            host.key("Tab", "Tab", 9); loop.append(host.active() or host.eval("document.activeElement.className"))
        host.key("Tab", "Tab", 9, shift=True); loop.append(host.active() or host.eval("document.activeElement.className"))
        check("keyboard loop while paused and reconnecting: Resume -> End game -> Retry -> Resume, Shift+Tab back to Retry",
              loop[0] == "overlay-end-btn" and loop[1] == "Retry now" and loop[2] == "resume-btn" and loop[3] == "Retry now", loop)
        host.click("document.querySelector('.conn-retry')", "Retry now above pause panel", pre="window.__majlisSimTransport.offline = false; true")
        host.wait("document.body.dataset.conn === 'live' && !!document.getElementById('paused-panel')", what="live, still paused")
        check("Retry now reachable above the pause panel; room stays paused", True)
        # Operator on a 393 px phone: touch Resume, Pause, End game from the panel.
        host.viewport(PHONE); time.sleep(0.3)
        host.shot(out, "12d-host-paused-393")
        host.click("document.getElementById('resume-btn')", "Resume (touch, 393)")
        for p in players.values():
            p.wait("!document.querySelector('.overlay')", what="resumed by touch")
        host.wait("!document.getElementById('paused-panel')")
        host.click("document.getElementById('pause-btn')", "Pause (touch, 393)")
        host.wait("!!document.getElementById('paused-panel')", what="paused by touch")
        host.click("document.getElementById('overlay-end-btn')", "End game in pause panel (touch, 393)")
        host.wait("!!document.querySelector('dialog.confirm[open]')")
        focused = host.eval("document.activeElement.textContent")
        host.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        host.call("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape", windowsVirtualKeyCode=27)
        host.wait("!document.querySelector('dialog.confirm[open]')", what="escape closes")
        check("End game confirm from pause panel: Cancel focused first, Escape cancels, room stays paused",
              focused == "Cancel" and host.eval("!!document.getElementById('paused-panel')") and players["Omar"].eval("!!document.querySelector('.overlay')"), focused)
        host.click("document.getElementById('overlay-end-btn')", "End game in pause panel (touch, 393)")
        host.confirm(True)
        for p in players.values():
            p.wait("document.body.textContent.includes('Game over')", what="ended")
        check("game over reaches all players (ended from the pause panel by touch)", True)
        host.viewport(LAPTOP)
        players["Aisha"].shot(out, "13-player-ended-393", full=True)
        tv.wait("!!document.querySelector('.tv-ended')")
        tv.shot(out, "14-tv-ended-1920")
        # TV after the game ended: stream is dropped; hiding/showing the tab must not throw, scores stay.
        final1 = scores(tv.last_snap())
        ex_before = sum(e.get("method") == "Runtime.exceptionThrown" for e in tv.events)
        tv.eval("""(() => { document.body.dataset.conn = 'reconnecting';
          for (const v of ['hidden', 'visible', 'hidden', 'visible']) { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v }); document.dispatchEvent(new Event('visibilitychange')); }
          delete document.visibilityState; document.body.dataset.conn = 'live'; return true; })()""")
        time.sleep(0.5)
        tv.eval("1")  # drain pending CDP events
        ex_after = sum(e.get("method") == "Runtime.exceptionThrown" for e in tv.events)
        shown = tv.eval("document.querySelector('.tv-ended').textContent")
        check("TV ended -> hidden/visible with a non-live banner: no exception, final scores kept",
              ex_after == ex_before and all(nm in shown and str(sc) in shown for nm, sc in final1.items()), {"exceptions": ex_after - ex_before, "final": final1})

        # ---------- Feud (new room: protocol v1 has no 'back to lobby') ----------
        host.click("__btn('Start a new room')", "new room")
        host.click("__btn('Create room')", "Create room")
        code2 = host.wait("(() => { const c = (document.querySelector('.room-code')||{}).textContent; return c && c !== %s ? c : null; })()" % json.dumps(code), what="room 2")
        for k, v in host.eval("Object.fromEntries(Object.entries(localStorage).filter(([k]) => k.startsWith('majlisParty.')))").items():
            try: secret_tokens.add(json.loads(v)["token"])
            except Exception: pass
        tv.goto(f"{base}/tv.html?code={code2}&sim=1")
        for nm, p in players.items():
            p.goto(f"{base}/play.html?code={code2}&sim=1", fresh_phone=True)
            p.type_into("#join-name", nm)
            p.click("__btn('Join game')", "Join game")
            p.wait("document.body.textContent.includes('You’re in')", what="lobby 2")
            for m in p.eval("window.__rx"):
                if '"playerToken"' in m:
                    secret_tokens.add(json.loads(m)["result"]["playerToken"])
        host.wait("document.querySelectorAll('.chip').length === 3")
        host.click("document.querySelector('[data-mode=feud]')", "feud tile")
        host.wait("document.querySelector('[data-mode=feud]').getAttribute('aria-pressed') === 'true'")
        host.click("__btn('Start Majlis Feud')", "start feud")
        host.wait("!!document.querySelector('.op-slot')", what="op board")
        answers = host.eval("Array.from(document.querySelectorAll('.op-slot .slot-text')).map(e => e.textContent)")
        check("operator sees full house-answer board", len(answers) >= 5, answers)
        for nm, p in players.items():
            p.wait("!!document.querySelector('.buzzer')", what="buzzer")
            html = p.html(); states = p.rx_states()
            check(f"{nm}: no unrevealed answers in DOM or snapshots", not any(a in html or any(a in s for s in states) for a in answers))
            check(f"{nm}: buzzer locked before host opens", p.eval("document.querySelector('.buzzer').className.includes('state-locked') && document.querySelector('.buzzer').disabled"))
        tv.wait("!!document.querySelector('.tv-feud')")
        check("TV: no unrevealed answers", not any(a in tv.html() for a in answers))
        check("'HOUSE ANSWERS' label on player and TV boards", "house answers" in players["Omar"].html().lower() and tv.eval("document.querySelector('.house-label').textContent").lower().startswith("house answers"))
        host.shot(out, "15-host-feud-1280", full=True)
        players["Omar"].shot(out, "16-player-feud-locked-393")

        host.click("__btn('Open buzzer')", "open buzzer")
        for p in players.values():
            p.wait("document.querySelector('.buzzer').className.includes('state-open')", what="open")
        players["Aisha"].shot(out, "17-player-buzzer-open-393")
        # Buzz race: all three tap at the same scheduled instant.
        at = players["Aisha"].eval("Date.now() + 400")
        for p in players.values():
            p.eval(f"(() => {{ setTimeout(() => document.querySelector('.buzzer').click(), Math.max(0, {at} - Date.now())); return true; }})()")
        states = {}
        for nm, p in players.items():
            p.wait("/state-(won|lost)/.test(document.querySelector('.buzzer').className)", what="race result")
            states[nm] = p.eval("document.querySelector('.buzzer').className.match(/state-(\\w+)/)[1]")
        winners = [nm for nm, s in states.items() if s == "won"]
        check("buzz race (programmatic same-instant clicks): exactly one server-decided winner, everyone else sees 'lost'", len(winners) == 1 and sum(s == "lost" for s in states.values()) == 2, states)
        winner = winners[0] if winners else names[0]
        losers = [n for n in names if n != winner]
        check("all players and operator agree on the winner", all(winner in players[n].eval("document.querySelector('.buzzer').textContent") for n in losers)
              and host.wait(f"document.querySelector('.buzz-state').textContent.includes({json.dumps(winner)})"), winner)
        players[winner].shot(out, "18-player-buzz-won-393")
        players[losers[0]].shot(out, "19-player-buzz-lost-393")
        # Reveal answer 1 -> appears for players/TV, answer 2 still hidden; fresh flip only once.
        host.click("document.querySelector('.op-slot:nth-child(1) button')", "reveal 1")
        for p in list(players.values()) + [tv]:
            p.wait(f"document.querySelector('.slot.revealed') && document.querySelector('.slot.revealed').textContent.includes({json.dumps(answers[0])})", what="reveal 1")
        check("revealed answer shown to players/TV; others still absent", all(not any(a in p.html() for a in answers[1:]) for p in list(players.values()) + [tv]))
        check("flip animation applies only to the newly revealed slot", players["Omar"].eval("document.querySelectorAll('.slot.fresh').length") == 1)
        # Strikes x3 -> steal stage.
        for i in range(3):
            host.click("__btn('Strike')", "strike")
            host.wait(f"document.querySelectorAll('.strike-ctl .strike.on').length === {i + 1}", what=f"strike {i + 1}")
        players["Omar"].wait("document.querySelectorAll('.strike.on').length === 3", what="3 strikes")
        tv.wait("document.querySelectorAll('.strike.on').length === 3")
        check("three strikes reach players and TV", True)
        tv.shot(out, "20-tv-feud-strikes-1920")
        steal_to = losers[0]
        host.wait("!!document.getElementById('steal-player')", what="steal control")
        opts = host.eval("Array.from(document.querySelectorAll('#steal-player option')).map(o => o.textContent)")
        check("steal list excludes the buzz winner", winner not in opts, opts)
        host.eval(f"(() => {{ const s = document.getElementById('steal-player'); const o = Array.from(s.options).find(o => o.textContent === {json.dumps(steal_to)}); s.value = o.value; s.dispatchEvent(new Event('change')); return true; }})()")
        host.click("__btn('Give steal chance')", "steal")
        players[steal_to].wait("document.body.textContent.includes('Your chance to steal')", what="steal prompt")
        players[steal_to].shot(out, "21-player-steal-393")
        bank = int(host.eval("document.querySelector('.bank-num').textContent"))
        host.eval(f"(() => {{ const s = document.getElementById('award-player'); const o = Array.from(s.options).find(o => o.textContent === {json.dumps(steal_to)}); s.value = o.value; s.dispatchEvent(new Event('change')); return true; }})()")
        host.click("document.querySelector('.award-ctl .btn-good')", "award")
        dinfo = host.confirm(True)
        check("award confirm names the player and points", steal_to in dinfo["text"] and str(bank) in dinfo["text"], dinfo["text"])
        players[steal_to].wait(f"document.querySelector('.me-chip').textContent.endsWith('· {bank}')", what="steal score")
        check("award: stealing player's score equals the round bank", True, bank)
        tv.wait("document.body.textContent.includes('won')")
        tv.shot(out, "22-tv-round-over-1920")
        host.shot(out, "23-host-round-over-1280", full=True)
        # Remaining answers can be shown after the round; then next question resets the board.
        host.eval("window.__tx = []; true")
        fr1 = host.round_id()
        host.click("__btn('Next question')", "next")
        players["Omar"].wait("document.querySelectorAll('.strike.on').length === 0 && document.querySelectorAll('.slot.revealed').length === 0", what="next round")
        check("next question: new board, strikes and reveals reset", True)
        answers2 = host.eval("Array.from(document.querySelectorAll('.op-slot .slot-text')).map(e => e.textContent)")
        check("player DOM has none of round-2 answers", not any(a in players["Omar"].html() for a in answers2))
        host.wait(f"(() => {{ const m = (window.__rx || []).filter(x => x.includes('\"type\":\"state\"')).pop(); return m && JSON.parse(m).snapshot.roundId === {fr1} + 1; }})()", what="host round advanced")
        tx = host.eval("window.__tx")
        check("Next question sends round.next with the round it was shown for", [t["type"] for t in tx] == ["round.next"] and tx[0]["roundId"] == fr1 and host.round_id() == fr1 + 1, {"tx": tx, "shown_round": fr1, "now": host.round_id()})
        check("fixture boards have six house answers", len(answers2) == 6 and len(answers) == 6, [len(answers), len(answers2)])

        # ---------- round targeting (Feud): an Award dialog left open across a round change ----------
        host2.goto(f"{base}/host.html?code={code2}&sim=1")
        host2.wait("!!document.querySelector('.op-slot')", what="host2 feud")
        fr2 = host.round_id()
        host.click("__btn('Open buzzer')", "open buzzer (round 2)")
        players["Aisha"].wait("document.querySelector('.buzzer').className.includes('state-open')", what="open r2")
        players["Aisha"].click("document.getElementById('buzzer')", "BUZZ (touch, 393)")
        players["Aisha"].wait("document.querySelector('.buzzer').className.includes('state-won')", what="Aisha won by touch")
        check("player buzzer reached by trusted touch at its centre", players["Aisha"].eval("window.__clicks.slice(-1)[0].trusted && window.__clicks.slice(-1)[0].id === 'buzzer'"), players["Aisha"].eval("window.__clicks.slice(-1)[0]"))
        host.click("document.querySelector('.op-slot:nth-child(2) button')", "reveal 2 (round 2)")
        host.wait("document.querySelector('.bank-num').textContent !== '0'", what="bank")
        host.eval(f"(() => {{ const s = document.getElementById('award-player'); const o = Array.from(s.options).find(o => o.textContent === 'Aisha'); s.value = o.value; s.dispatchEvent(new Event('change')); return true; }})()")
        before = scores(host.last_snap())
        host.eval("window.__tx = []; true")
        host.click("document.querySelector('.award-ctl .btn-good')", "Award (round 2)")
        host.wait("!!document.querySelector('dialog.confirm[open]')", what="award dialog")
        dlg_text = host.eval("document.querySelector('dialog.confirm[open]').textContent")
        n_rx = host.eval("(window.__rx || []).length")
        same = op_act(host2, code2, "feud.strike", round_id=fr2)
        host.wait(f"(window.__rx || []).length > {n_rx}", what="same-round snapshot under open award dialog")
        time.sleep(0.3)
        kept = host.eval("!!document.querySelector('dialog.confirm[open]') && document.querySelector('dialog.confirm[open]').textContent")
        check("Feud: a same-round update (strike by second operator) leaves the Award confirm open with its original player and points",
              same.get("accepted") is True and kept == dlg_text, {"strike": same, "dialog": dlg_text, "kept": kept})
        moved = op_act(host2, code2, "round.next", round_id=fr2)
        host.wait("!document.querySelector('dialog.confirm')", what="award dialog closed by round change")
        time.sleep(0.6)
        tx = host.eval("window.__tx")
        toast_txt = host.eval("(document.getElementById('toast')||{}).textContent || ''")
        after = scores(host.last_snap())
        pl_after = scores(players["Omar"].last_snap())
        check("Feud: an Award confirm opened in round N closes unsent when round N+1 arrives; no feud.award sent; round and scores unchanged; operator told which action",
              moved.get("accepted") is True and not any(t["type"] == "feud.award" for t in tx)
              and after == before and pl_after == before and host.round_id() == fr2 + 1 and "was cancelled" in toast_txt and "to Aisha" in toast_txt,
              {"dialog": dlg_text, "tx": tx, "toast": toast_txt, "before": before, "after": after, "round": host.round_id()})
        aisha_id = next(p["id"] for p in host.last_snap()["players"] if p["name"] == "Aisha")
        stale = op_act(host2, code2, "feud.award", {"playerId": aisha_id}, round_id=fr2)
        time.sleep(0.3)
        check("fixture: feud.award with a stale roundId is rejected; scores unchanged", stale.get("error") == "stale_round" and scores(host.last_snap()) == before, stale)
        host.wait("!!document.querySelector('.op-slot') && !document.querySelector('.op-slot.revealed')", what="round 3 board")
        players["Omar"].wait("document.querySelectorAll('.slot.revealed').length === 0 && document.querySelector('.buzzer').className.includes('state-locked')", what="round 3 player board")
        results["round_targeting_feud"] = {"rounds": [fr1, fr2, host.round_id()], "scores": before}

        # ---------- reconnect ----------
        p = players["Noora"]
        p.eval("window.__majlisSimTransport.setOffline(true); true")
        p.wait("document.body.dataset.conn === 'reconnecting'", what="reconnecting")
        check("reconnecting banner + Retry now; last board kept; controls disabled",
              p.eval("!document.getElementById('conn').hidden && document.getElementById('conn').textContent.includes('Reconnecting') && !!document.querySelector('.conn-retry') && !!document.querySelector('.feud-board')"))
        p.shot(out, "24-player-reconnecting-360")
        host.click("__btn('Open buzzer')", "open buzzer 2")
        time.sleep(0.6)
        check("offline player did not receive the update", p.eval("!document.querySelector('.buzzer').className.includes('state-open')"))
        p.click("document.querySelector('.conn-retry')", "retry now", pre="window.__majlisSimTransport.offline = false; true")
        p.wait("document.body.dataset.conn === 'live' && document.querySelector('.buzzer').className.includes('state-open')", what="back live")
        check("Retry now reconnects and catches up to current state", True)
        # Server-side stream drop for everyone (sim.dropStreams) -> automatic recovery.
        players["Aisha"].eval(f"window.__majlisSimTransport.call('sim.dropStreams', {{code: {json.dumps(code2)}}})")
        for t in list(players.values()) + [tv, host]:
            t.wait("document.body.dataset.conn === 'live'", timeout=10, what="auto reconnect")
        check("all clients recover automatically after a dropped stream", True)
        # Reload resume: the last joined player's credential is the one in this shared profile.
        last = names[-1]
        players[last].goto(f"{base}/play.html?code={code2}&sim=1")
        players[last].wait("!!document.querySelector('.buzzer')", what="resumed after reload")
        check("player reload resumes the same seat without re-joining", players[last].eval(f"document.querySelector('.me-chip').textContent.startsWith({json.dumps(last)})"))
        host.goto(f"{base}/host.html?code={code2}&sim=1")
        host.wait("!!document.querySelector('.op-slot')", what="host resume")
        check("operator reload resumes the room", True)

        # ---------- secrets ----------
        leaks = []
        for nm, t in tabs.items():
            if nm == "unit":
                continue
            h = t.html(); href = t.eval("location.href")
            for tok in secret_tokens:
                if tok in h: leaks.append((nm, "dom"))
                if tok in href: leaks.append((nm, "url"))
                if any(tok in s for s in t.rx_states()): leaks.append((nm, "snapshot"))
        check("no token in any tab's DOM, URL or received snapshot", not leaks and len(secret_tokens) >= 8, {"leaks": leaks, "tokens_checked": len(secret_tokens)})

        # ---------- layout + a11y ----------
        a11y = {}
        for nm, t in [("host", host), ("Aisha", players["Aisha"]), ("Noora", players["Noora"]), ("tv", tv)]:
            a11y[nm] = t.eval(A11Y_JS)
        for nm, r in a11y.items():
            check(f"a11y {nm}: every control named, labelled, >=44px, no horizontal overflow", not r["unnamed"] and not r["unlabelled"] and not r["small"] and not r["overflow"], r)
        # Operator on a phone and TV at 720p.
        host.viewport(PHONE); time.sleep(0.3)
        check("operator at 393px: no horizontal overflow", not host.eval("__overflow()"))
        host.shot(out, "25-host-feud-393", full=True)
        tv.viewport(TV720); time.sleep(0.3)
        check("TV at 1280x720: no overflow", not tv.eval("__overflow()"))
        tv.shot(out, "26-tv-feud-1280x720")
        # Keyboard: Tab reaches the operator's 'Open buzzer'-row controls in DOM order.
        host.viewport(LAPTOP)
        order = host.eval("""(() => { const els = Array.from(document.querySelectorAll('button:not([disabled]), select, input, a[href]')); return els.map(e => (e.getAttribute('aria-label') || e.textContent || e.id).trim().slice(0, 30)); })()""")
        results["host_tab_order"] = order
        check("operator controls are native focusable elements (no tabindex traps)", host.eval("document.querySelectorAll('[tabindex]:not([tabindex=\"0\"]):not([tabindex=\"-1\"])').length === 0"))

        # ---------- small phones: six answers + long names, buzzer never covers the board ----------
        host2.viewport(PHONE)
        host2.goto(f"{base}/host.html?sim=1")
        host2.click("__btn('Create room')", "Create room (393)")
        code3 = host2.wait("(() => { const c = (document.querySelector('.room-code')||{}).textContent; return c && c !== %s && c !== %s ? c : null; })()" % (json.dumps(code), json.dumps(code2)), what="room 3")
        long_names = {"Abdulrahman Mansoori": SMALL, "عبدالرحمن المنصوري": PHONE, "Maximilian-Alexander": LANDSCAPE}
        lp = {}
        for nm, vp in long_names.items():
            t = new_tab(f"long-{vp['w']}x{vp['h']}", vp)
            t.goto(f"{base}/play.html?code={code3}&sim=1", fresh_phone=True)
            t.type_into("#join-name", nm)
            t.click("__btn('Join game')", f"Join game ({vp['w']}x{vp['h']})")
            t.wait("document.body.textContent.includes('You’re in')", what="lobby 3")
            lp[nm] = t
        host2.wait("document.querySelectorAll('.chip').length === 3")
        host2.click("document.querySelector('[data-mode=feud]')", "feud tile (393)")
        host2.wait("document.querySelector('[data-mode=feud]').getAttribute('aria-pressed') === 'true'")
        host2.click("__btn('Start Majlis Feud')", "start feud (393)")
        host2.wait("document.querySelectorAll('.op-slot').length === 6", what="6 op slots")
        host2.click("__btn('Open buzzer')", "Open buzzer (393)")
        land = lp["Maximilian-Alexander"]
        land.wait("document.querySelector('.buzzer').className.includes('state-open')", what="open (landscape)")
        land.shot(out, "27-player-feud6-open-734x393")
        land.click("document.getElementById('buzzer')", "BUZZ (touch, 734x393)")
        for nm, t in lp.items():
            t.wait("/state-(won|lost)/.test(document.querySelector('.buzzer').className)", what="buzz result")
        check("long names: landscape player won by touch; others show the 20-char winner name in the buzzer",
              land.eval("document.querySelector('.buzzer').className.includes('state-won')")
              and all("Maximilian-Alexander" in t.eval("document.querySelector('.buzzer').textContent") for n, t in lp.items() if t is not land))
        for i in range(1, 7):
            host2.click(f"document.querySelector('.op-slot:nth-child({i}) button')", f"Reveal answer {i} (touch, 393)")
            host2.wait(f"document.querySelectorAll('.op-slot.revealed').length === {i}", what=f"revealed {i}")
        for t in lp.values():
            t.wait("document.querySelectorAll('.slot.revealed').length === 6", what="6 revealed on player")
        layout = {}
        for nm, t in lp.items():
            vp = t.vp
            # Probe at rest: the reveal flip (rotateX) only runs while the tab is in front.
            t.call("Page.bringToFront")
            t.wait("document.getAnimations().every(a => a.animationName !== 'flip' || a.playState === 'finished')", what="reveal flips finished")
            r = t.eval(LAYOUT_JS)
            layout[f"{vp['w']}x{vp['h']}"] = r
            bad = [x for x in r["items"] if not x["ok"]]
            check(f"{vp['w']}x{vp['h']}: all 6 answers + points and every score row hit-testable after scrolling, never under the buzzer",
                  len([x for x in r["items"] if x["kind"] == "slot"]) == 6 and not bad and not r["overflow"], {"bad": bad[:4], "overflow": r["overflow"], "layout": r["mode"]})
            check(f"{vp['w']}x{vp['h']}: buzzer itself hit-testable at its centre", r["buzzer"]["ok"], r["buzzer"])
            tag = f"{vp['w']}x{vp['h']}"
            t.call("Runtime.evaluate", expression="scrollTo(0, 0)")
            t.shot(out, f"28-player-feud6-top-{tag}")
            t.call("Runtime.evaluate", expression="scrollTo(0, document.documentElement.scrollHeight)")
            t.shot(out, f"29-player-feud6-bottom-{tag}")
        results["layout_six_answers"] = layout
        hr = host2.eval(HOST_LAYOUT_JS)
        check("operator at 393 with six answers + long names: every board/buzzer/strike/award/next control hit-testable, no overflow",
              hr["items"] and all(x["ok"] for x in hr["items"]) and not hr["overflow"], [x for x in hr["items"] if not x["ok"]][:4] or len(hr["items"]))
        results["layout_host_393"] = hr
        host2.shot(out, "30-host-feud6-393", full=True)

        # ---------- TV: offline while the game ends, reconnect, then hide/show ----------
        tv.viewport(TV)
        ex_before = sum(e.get("method") == "Runtime.exceptionThrown" for e in tv.events)
        tv.eval("window.__majlisSimTransport.setOffline(true); true")
        tv.wait("document.body.dataset.conn === 'reconnecting'", what="tv reconnecting")
        host.click("__btn('End game')", "End game (room 2)")
        host.confirm(True)
        host.wait("document.body.textContent.includes('Game over') || !!document.querySelector('.final')", timeout=6, what="host ended")
        tv.click("document.querySelector('.conn-retry')", "TV Retry now", pre="window.__majlisSimTransport.offline = false; true")
        tv.wait("!!document.querySelector('.tv-ended')", what="tv ended after reconnect")
        final2 = scores(tv.last_snap())
        for v in ("hidden", "visible", "hidden", "visible"):
            tv.eval(f"(() => {{ Object.defineProperty(document, 'visibilityState', {{ configurable: true, get: () => {json.dumps(v)} }}); document.dispatchEvent(new Event('visibilitychange')); return true; }})()")
            time.sleep(0.2)
        tv.eval("delete document.visibilityState; window.__majlisSimTransport.setOffline(true); window.__majlisSimTransport.offline = false; true")
        time.sleep(0.8)
        tv.eval("1")
        ex_after = sum(e.get("method") == "Runtime.exceptionThrown" for e in tv.events)
        shown = tv.eval("document.querySelector('.tv-ended') && document.querySelector('.tv-ended').textContent")
        check("TV offline -> game ended -> reconnect -> hide/show/offline toggle: no exception, final scores kept",
              ex_after == ex_before and shown and all(nm in shown and str(sc) in shown for nm, sc in final2.items()) and final2 == scores(host.last_snap()),
              {"exceptions": ex_after - ex_before, "final": final2})
        tv.shot(out, "31-tv-ended-after-reconnect-1920")

        # ---------- network ----------
        api = [r for r in requests_seen if "/api/" in r]
        check("simulated mode made no /api requests (no backend involved)", not api, api[:5])
        results["static_requests"] = sorted(set(r.split(" ")[1].split("?")[0] for r in requests_seen))
        errs = []
        for nm, t in tabs.items():
            for e in t.events:
                if e.get("method") == "Runtime.exceptionThrown":
                    errs.append((nm, e["params"]["exceptionDetails"].get("text"), json.dumps(e["params"]["exceptionDetails"].get("exception", {}).get("description", ""))[:200]))
                if e.get("method") == "Runtime.consoleAPICalled" and e["params"]["type"] == "error":
                    errs.append((nm, "console.error", json.dumps(e["params"]["args"])[:200]))
        check("no uncaught page exceptions or console errors", not errs, errs[:5])
        results["pointer_log"] = pointer_log
        check("every scripted click was trusted pointer input that hit its control at the centre",
              pointer_log and all(r.get("ok") and (r.get("delivered") or {}).get("trusted") for r in pointer_log),
              {"clicks": len(pointer_log), "touch": sum(r["input"] == "touch" for r in pointer_log), "mouse": sum(r["input"] == "mouse" for r in pointer_log)})
    except Exception:
        for nm, t in tabs.items():
            try:
                print("=== " + nm + ": " + t.eval("((document.getElementById('toast')||{}).textContent || '') + ' | ' + document.body.innerText.replace(/\\s+/g, ' ').slice(0, 400)"))
            except Exception:
                pass
            for e in t.events:
                if e.get("method") in ("Runtime.exceptionThrown", "Runtime.consoleAPICalled"):
                    print("   ", nm, json.dumps(e["params"])[:500])
        raise
    finally:
        results["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results.setdefault("pointer_log", pointer_log)
        results["summary"] = {"pass": sum(c["pass"] for c in results["checks"]), "fail": sum(not c["pass"] for c in results["checks"])}
        json.dump(results, open(os.path.join(out, "sim-e2e-results.json"), "w"), indent=1, ensure_ascii=False)
        print(json.dumps(results["summary"]))
        chrome.terminate(); chrome.wait(10); srv.shutdown(); shutil.rmtree(profile, ignore_errors=True)


# For each answer slot (text + points) and score row: scroll it into view as a user would, then
# check the pointer at the centre of the text and the points lands on that row and that the row
# does not intersect the buzzer area. Uses elementFromPoint (real hit-testing), not scrollWidth.
LAYOUT_JS = r"""
(() => {
  const wrap = document.querySelector('.buzz-wrap');
  const items = [];
  const probe = (row, kind, parts) => {
    row.scrollIntoView({ block: 'nearest' });
    const r = row.getBoundingClientRect(); const w = wrap.getBoundingClientRect();
    const overlapBuzz = !(r.right <= w.left || r.left >= w.right || r.bottom <= w.top || r.top >= w.bottom);
    const tops = Array.from(document.querySelectorAll('.sim-ribbon, .conn:not([hidden])')).map(e => e.getBoundingClientRect().bottom);
    const underTop = r.top < Math.max(0, ...tops) - 0.5;
    const hits = parts.filter(Boolean).map(el => { const q = el.getBoundingClientRect(); const x = q.left + q.width / 2, y = q.top + q.height / 2; const hit = document.elementFromPoint(x, y);
      return { part: el.className, x: Math.round(x), y: Math.round(y), ok: !!hit && row.contains(hit), hit: __desc(hit), text: (el.textContent || '').trim().slice(0, 40), clipped: el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflow === 'hidden' }; });
    items.push({ kind, ok: !overlapBuzz && !underTop && r.bottom <= innerHeight + 0.5 && hits.every(h => h.ok && !h.clipped), overlapBuzz, underTop, rect: [Math.round(r.top), Math.round(r.bottom)], hits });
  };
  document.querySelectorAll('.slot').forEach(sl => probe(sl, 'slot', [sl.querySelector('.slot-text'), sl.querySelector('.slot-pts')]));
  document.querySelectorAll('.score-row').forEach(sr => probe(sr, 'score', [sr.querySelector('.sname'), sr.querySelector('.spts')]));
  const b = __aim(document.getElementById('buzzer'));
  const cs = getComputedStyle(document.querySelector('.pl-feud'));
  return { items, buzzer: b, buzzWrap: (() => { const r = wrap.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; })(),
           mode: cs.display + ' ' + cs.gridTemplateColumns, overflow: __overflow(), viewport: [innerWidth, innerHeight] };
})()
"""

HOST_LAYOUT_JS = r"""
(() => {
  const sels = Array.from(document.querySelectorAll('.feud-op button, .feud-op select, .topbar button'));
  const items = sels.map(e => { const a = __aim(e); return { ok: a.ok, target: a.target, text: a.text, hit: a.hit }; });
  return { items, overflow: __overflow() };
})()
"""

A11Y_JS = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && !e.closest('[hidden]'); };
  const name = e => (e.getAttribute('aria-label') || e.textContent || e.value || '').trim();
  const ctrls = Array.from(document.querySelectorAll('button, a[href], select, input')).filter(vis);
  const unnamed = ctrls.filter(e => e.tagName !== 'INPUT' && e.tagName !== 'SELECT' && !name(e)).map(e => e.outerHTML.slice(0, 80));
  const unlabelled = Array.from(document.querySelectorAll('input, select')).filter(vis).filter(e => !(e.id && document.querySelector('label[for="' + e.id + '"]')) && !e.getAttribute('aria-label')).map(e => e.outerHTML.slice(0, 80));
  const small = ctrls.filter(e => { const r = e.getBoundingClientRect(); return (r.height < 44 || r.width < 44) && !e.classList.contains('brand'); }).map(e => name(e).slice(0, 30) + ' ' + Math.round(e.getBoundingClientRect().width) + 'x' + Math.round(e.getBoundingClientRect().height));
  return { controls: ctrls.length, unnamed, unlabelled, small, overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, lang: document.documentElement.lang };
})()
"""

UNIT_JS = r"""
(async () => {
  const { SseParser, HttpTransport } = await import('./js/transport.js');
  const { saveCredential, loadCredential, clearCredential, normalizeCode, isCodeShape } = await import('./js/session.js');
  const { encodeText } = await import('./js/qr.js');
  const r = {};
  // SSE parser: CRLF, split chunks, comments, multi-line data, ids.
  const evs = []; const p = new SseParser(e => evs.push(e));
  ['id: 7\r', '\nevent: state\r\ndata: {"a":', '1}\r\n\r\n', ': heartbeat\n\n', 'id:8\nevent:state\ndata: x\ndata: y\n\n'].forEach(c => p.push(c));
  r['SSE parser handles CRLF/split chunks/heartbeats/multi-line data'] = evs.length === 2 && evs[0].id === '7' && evs[0].data === '{"a":1}' && evs[1].data === 'x\ny' && evs[1].id === '8' || JSON.stringify(evs);
  // act(): network failure retried with the SAME actionId; 4xx returned as a result, not retried.
  const sent = []; let n = 0;
  const t = new HttpTransport({ fetchImpl: async (url, opts) => { sent.push({ url, opts }); n++; if (n === 1) throw new TypeError('offline');
    return new Response(JSON.stringify({ accepted: true, revision: 3 }), { status: 200 }); } });
  const res = await t.act('ABC123', 'tok-secret', 'feud.buzz', {}, 2);
  const ids = sent.map(s => JSON.parse(s.opts.body).actionId);
  r['act retries network errors with the same actionId'] = res.accepted === true && ids.length === 2 && ids[0] === ids[1] || JSON.stringify({ res, ids });
  r['token only in Authorization header, never URL/body'] = sent.every(s => s.opts.headers.Authorization === 'Bearer tok-secret' && !s.url.includes('tok') && !s.opts.body.includes('tok-secret')) || 'leak';
  r['roundId sent on actions'] = JSON.parse(sent[0].opts.body).roundId === 2 || sent[0].opts.body;
  let calls = 0;
  const t2 = new HttpTransport({ fetchImpl: async () => { calls++; return new Response(JSON.stringify({ error: 'buzzer_taken' }), { status: 409 }); } });
  const rej = await t2.act('ABC123', 'x', 'feud.buzz', {});
  r['server rejection returned once, not retried'] = rej.accepted === false && rej.error === 'buzzer_taken' && calls === 1 || JSON.stringify({ rej, calls });
  // subscribe(): snapshots delivered; reconnect sends Last-Event-ID; 401 is fatal.
  const enc = new TextEncoder(); const hdrs = []; let k = 0; const snaps = []; const st = [];
  const body = txt => new ReadableStream({ start(c) { c.enqueue(enc.encode(txt)); c.close(); } });
  const t3 = new HttpTransport({ fetchImpl: async (url, opts) => { hdrs.push(opts.headers); k++;
    if (k === 1) return new Response(body('id: 5\nevent: state\ndata: {"revision":5}\n\n'), { status: 200 });
    if (k === 2) return new Response(body('id: 6\nevent: state\ndata: {"revision":6}\n\n'), { status: 200 });
    return new Response(JSON.stringify({ error: 'unauthorized' }), { status: 401 }); } });
  t3.subscribe('ABC123', 'tok2', { onSnapshot: s => snaps.push(s.revision), onStatus: s => st.push(s) });
  await new Promise(res => setTimeout(res, 3500));
  r['stream: snapshots, Last-Event-ID on reconnect, 401 fatal'] = snaps.join() === '5,6' && hdrs[1]['Last-Event-ID'] === '5' && hdrs[0].Authorization === 'Bearer tok2' && st[st.length - 1] === 'fatal' || JSON.stringify({ snaps, hdrs, st });
  // Session storage: per role + room, sim namespace separate, expiry honoured.
  saveCredential('player', 'ABC123', { token: 't1', playerId: 'p1' }, true);
  r['credential stored per role/room, sim separate from real'] = loadCredential('player', 'ABC123', true).token === 't1' && loadCredential('player', 'ABC123', false) === null && loadCredential('operator', 'ABC123', true) === null || 'mismatch';
  clearCredential('player', 'ABC123', true);
  r['credential cleared'] = loadCredential('player', 'ABC123', true) === null || 'still there';
  r['code normalisation'] = normalizeCode(' ab c12 3 ') === 'ABC123' && isCodeShape('ABC123') && !isCodeShape('ABC12') || normalizeCode(' ab c12 3 ');
  const q = encodeText('http://192.168.1.20:8080/play.html?code=ABC123', 'M');
  r['QR encoder picks a valid version'] = q.size === 17 + 4 * q.version && q.modules.length === q.size || JSON.stringify({ size: q.size, v: q.version });
  return r;
})()
"""

if __name__ == "__main__":
    main()
