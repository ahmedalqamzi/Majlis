"""Majlis Party first-pair checks against the REAL party.server, over loopback HTTP + fetch-SSE.

This is REAL-NETWORK browser evidence on one Mac, not a physical phone and not a public host:
the in-repo server (python -m party.server --web-dir party/web --content-dir party/content) runs
on 127.0.0.1 and serves the pages itself, same-origin. Headless Chrome renders every role:

  * operator (host.html#code=ROOM) and TV (tv.html#code=ROOM) share one browser context, as a
    host laptop and the TV window it opened would;
  * each of the three players runs in its own isolated browser context (separate storage,
    cookies and cache), as three different phones would.

No SharedWorker, no ?sim=1, no mock adapter. The only instrumentation is a pass-through fetch tee
(installed before page scripts) that records the bytes each page received and the action bodies it
sent, plus CDP network events (Authorization redacted). Clicks are trusted CDP input aimed at the
control's centre after elementFromPoint confirms the control itself would receive the pointer.

  python real_e2e.py <repo root> <out dir> [qr_decoder_binary]

Env MAJLIS_SERVER_PY selects the interpreter for the server (default: this interpreter).
Needs Google Chrome and python websocket-client.
"""
import base64, json, os, re, secrets, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_6 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/26.6 Mobile/15E148 Safari/604.1")
PHONE = dict(w=393, h=852, dpr=3, mobile=True)
SMALL = dict(w=360, h=780, dpr=3, mobile=True)
LANDSCAPE = dict(w=734, h=393, dpr=3, mobile=True, insets=dict(top=0, bottom=21, left=59, right=59))
LAPTOP = dict(w=1280, h=800, dpr=1, mobile=False)
TV = dict(w=1920, h=1080, dpr=1, mobile=False)

# Pass-through fetch tee: the page gets an identical Response; the test keeps a copy of the bytes.
# Request bodies are recorded; headers are not (the Authorization value never enters this log).
NET_HOOK = r"""
(() => {
  const of = window.fetch.bind(window);
  window.__rx = []; window.__tx = [];
  window.fetch = async function (input, init = {}) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const method = (init.method || 'GET').toUpperCase();
    const hdr = init.headers || {};
    const t = { path: url.pathname, search: url.search, method, at: Math.round(performance.now()),
                auth: !!(hdr.Authorization || hdr.authorization), lastEventId: hdr['Last-Event-ID'] || null,
                body: typeof init.body === 'string' ? init.body : null };
    window.__tx.push(t);
    const res = await of(input, init);
    const rec = { path: url.pathname, method, status: res.status, ct: res.headers.get('content-type') || '', chunks: [], done: false, at: t.at };
    window.__rx.push(rec);
    if (!res.body || [101, 204, 205, 304].includes(res.status)) { rec.done = true; return res; }
    const [a, b] = res.body.tee();
    (async () => { const rd = b.getReader(); const dec = new TextDecoder();
      try { for (;;) { const { done, value } = await rd.read(); if (done) break; rec.chunks.push(dec.decode(value, { stream: true })); } }
      catch (e) { rec.error = String(e && e.name); } rec.done = true; })();
    return new Response(a, { status: res.status, statusText: res.statusText, headers: res.headers });
  };
  window.__touches = [];
  addEventListener('touchstart', ev => { const t = ev.target && ev.target.closest ? ev.target.closest('button') : null;
    window.__touches.push({ trusted: ev.isTrusted, id: t && t.id || null, disabled: !!(t && t.disabled), state: t && t.className || null }); }, true);
  window.__clicks = [];
  const logPress = ev => { const t = ev.target && ev.target.closest ? (ev.target.closest('button, a, [data-mode]') || ev.target) : ev.target;
    window.__clicks.push({ type: ev.type, trusted: ev.isTrusted, pointerType: ev.pointerType || null, id: t && t.id || null, text: ((t && t.textContent) || '').trim().slice(0, 40) }); };
  addEventListener('click', logPress, true);
  // The buzzer acts on press (it re-renders at once, so no click follows); log presses on it too.
  addEventListener('pointerdown', ev => { if (ev.target && ev.target.closest && ev.target.closest('#buzzer')) logPress(ev); }, true);
})();
"""

HELPERS = r"""
window.__btn = (text, root) => Array.from((root || document).querySelectorAll('button, a.btn')).find(b => (b.textContent || '').trim().includes(text) && !b.disabled);
window.__lbl = (label) => document.querySelector('[aria-label="' + label.replace(/"/g, '\\"') + '"]');
window.__overflow = () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
window.__desc = (e) => e ? (e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\s+/).join('.') : '')).slice(0, 90) : null;
window.__aim = (b, block) => {
  if (!b) return null;
  b.scrollIntoView({ block: block || 'nearest', inline: 'nearest' });
  const r = b.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  return { x, y, w: r.width, h: r.height, ok: !!hit && (hit === b || b.contains(hit)), target: __desc(b), hit: __desc(hit),
           hitIn: hit && hit.closest ? __desc(hit.closest('.overlay, dialog, .buzz-wrap, .slot, .score-row, .topbar, main, .conn')) : null,
           disabled: !!b.disabled, text: (b.textContent || '').trim().slice(0, 40), inViewport: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth };
};
// Sends one action as the page's own role, straight to the server (used for negative controls
// and for a "second operator device"); the token is read from this context's storage only.
window.__rawAct = async (role, code, body) => {
  const k = Object.keys(localStorage).find(k => k === 'majlisParty.v1.' + role + '.' + code);
  const tok = k ? JSON.parse(localStorage[k]).token : null;
  const res = await fetch('/api/rooms/' + code + '/actions', { method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', Authorization: 'Bearer ' + tok }, body: JSON.stringify(body) });
  let j = null; try { j = await res.json(); } catch (e) {}
  return { status: res.status, body: j, replayed: res.headers.get('X-Action-Replayed') };
};
true
"""

pointer_log = []
results = {"label": "REAL NETWORK (loopback HTTP + fetch-SSE to the in-repo party.server) - headless Chrome on one Mac; not a physical phone, not a public host",
           "checks": [], "screenshots": []}


def check(name, ok, detail=None):
    results["checks"].append({"name": name, "pass": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if detail is None else "  " + json.dumps(detail, ensure_ascii=False, default=str)[:300]), flush=True)
    return ok


def pace(s=0.15):
    time.sleep(s)


def sse_events(text):
    """Parses raw text/event-stream text into [(event, data)]."""
    out, ev, data = [], "", []
    for line in re.split(r"\r\n|\r|\n", text):
        if line == "":
            if data:
                out.append((ev or "message", "\n".join(data)))
            ev, data = "", []
        elif line.startswith(":"):
            continue
        else:
            f, _, v = line.partition(":")
            v = v[1:] if v.startswith(" ") else v
            if f == "data":
                data.append(v)
            elif f == "event":
                ev = v
    return out


def decoded_text(raw):
    """Raw bytes plus every JSON payload re-serialised unescaped, so a scan can't miss \\u escapes."""
    parts = [raw]
    for chunk in [raw] + [d for _, d in sse_events(raw)]:
        try:
            parts.append(json.dumps(json.loads(chunk), ensure_ascii=False))
        except Exception:
            pass
    return "\n".join(parts)


class Tab:
    def __init__(self, ws_url, name):
        self.name = name
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0
        self.events = []
        self.lock = threading.Lock()

    def call(self, method, **params):
        with self.lock:
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

    def wait(self, expr, timeout=10.0, what=None):
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
            txt = self.eval("document.body.innerText.slice(0, 700)")
        except Exception as e:
            txt = repr(e)
        raise TimeoutError(f"{self.name}: timed out waiting for {what or expr}\n--- page errors ---\n{self.page_errors()[-3:]}\n--- page text ---\n{txt}")

    def page_errors(self):
        out = []
        for e in self.events:
            if e.get("method") == "Runtime.exceptionThrown":
                out.append(e["params"].get("exceptionDetails", {}).get("exception", {}).get("description"))
            elif e.get("method") == "Runtime.consoleAPICalled" and e["params"]["type"] == "error":
                out.append(" ".join(str(a.get("value", a.get("description"))) for a in e["params"]["args"]))
        return out

    def console_text(self):
        return "\n".join(" ".join(str(a.get("value", a.get("description", ""))) for a in e["params"]["args"])
                         for e in self.events if e.get("method") == "Runtime.consoleAPICalled")

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

    def goto(self, url):
        self.call("Page.navigate", url=url)
        self.wait("document.readyState === 'complete'", what="load")
        self.eval(HELPERS)

    def reload(self):
        self.call("Page.reload", ignoreCache=False)
        time.sleep(0.3)
        self.wait("document.readyState === 'complete'", what="reload")
        self.eval(HELPERS)

    def shot(self, out, label, full=False):
        time.sleep(0.45)
        vp = self.vp
        if full:
            hgt = min(int(self.eval("document.documentElement.scrollHeight")), 4000)
            if hgt > vp["h"]:
                self.call("Emulation.setDeviceMetricsOverride", width=vp["w"], height=hgt, deviceScaleFactor=vp["dpr"], mobile=vp["mobile"])
                time.sleep(0.3)
        data = self.call("Page.captureScreenshot", format="png")["data"]
        if full:
            self.call("Emulation.setDeviceMetricsOverride", width=vp["w"], height=vp["h"], deviceScaleFactor=vp["dpr"], mobile=vp["mobile"])
        path = os.path.join(out, f"{label}.png")
        open(path, "wb").write(base64.b64decode(data))
        results["screenshots"].append(path)
        return path

    def html(self):
        return self.eval("document.documentElement.outerHTML")

    def text(self):
        return self.eval("document.body.innerText")

    def rx(self):
        return self.eval("window.__rx || []")

    def rx_raw(self, since=0):
        return "\n".join("".join(r["chunks"]) for r in self.rx()[since:])

    def rx_count(self):
        return self.eval("(window.__rx || []).length")

    def snaps(self):
        out = []
        for r in self.rx():
            if r["path"].endswith("/events"):
                for ev, data in sse_events("".join(r["chunks"])):
                    if ev == "state":
                        try:
                            out.append(json.loads(data))
                        except Exception:
                            pass
        return out

    def last_snap(self):
        s = self.snaps()
        return s[-1] if s else None

    def tx_actions(self):
        out = []
        for t in self.eval("window.__tx || []"):
            if t["path"].endswith("/actions") and t["body"]:
                b = json.loads(t["body"])
                out.append({"type": b.get("type"), "roundId": b.get("roundId"), "payload": b.get("payload"), "actionId": b.get("actionId")})
        return out

    def click(self, js_target, what, block="nearest"):
        """Trusted pointer input at the control's centre; refuses if anything else is on top of it."""
        self.call("Page.bringToFront")
        time.sleep(0.05)
        info = self.eval(f"(() => __aim({js_target}, {json.dumps(block)}))()")
        if not info:
            raise RuntimeError(f"{self.name}: could not find {what}")
        before = self.eval("window.__clicks.length")
        kind = "touch" if self.vp["mobile"] else "mouse"
        rec = dict(tab=self.name, what=what, viewport=f"{self.vp['w']}x{self.vp['h']}", input=kind,
                   **{k: info[k] for k in ("x", "y", "w", "h", "target", "hit", "hitIn", "ok", "disabled", "inViewport")})
        pointer_log.append(rec)
        if info["disabled"]:
            raise RuntimeError(f"{self.name}: {what} is disabled")
        if not info["ok"]:
            raise RuntimeError(f"{self.name}: {what} is covered at its centre ({info['x']:.0f},{info['y']:.0f}): pointer would hit {info['hit']} in {info['hitIn']}")
        self._press(info["x"], info["y"], kind)
        got = self.wait(f"window.__clicks.length > {before} && window.__clicks[window.__clicks.length - 1]", timeout=4, what=f"trusted click on {what}")
        rec["delivered"] = got
        if not got.get("trusted"):
            raise RuntimeError(f"{self.name}: click on {what} was not trusted: {got}")
        pace(0.12)
        return rec

    def _press(self, x, y, kind):
        if kind == "touch":
            self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[dict(x=x, y=y, radiusX=4, radiusY=4, force=1)])
            self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        else:
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
            self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)

    def type_into(self, sel, text):
        self.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); e.focus(); e.select && e.select(); return true; }})()")
        if text:
            self.call("Input.insertText", text=text)

    def confirm(self, accept=True):
        self.wait("!!document.querySelector('dialog.confirm[open]')", what="confirm dialog")
        info = self.eval("(() => { const d = document.querySelector('dialog.confirm[open]'); return { focused: document.activeElement && document.activeElement.textContent, text: d.textContent }; })()")
        self.click("document.querySelector('dialog.confirm[open] .confirm-actions .btn:last-child')" if accept else "document.querySelector('dialog.confirm[open] .confirm-actions .btn:first-child')", "dialog button")
        self.wait("!document.querySelector('dialog.confirm[open]')", what="dialog closed")
        return info

    def raw_act(self, role, code, type_, payload=None, round_id=None, action_id=None, omit_round=False):
        body = {"actionId": action_id or secrets.token_hex(8), "type": type_, "payload": payload or {}}
        if not omit_round and round_id is not None:
            body["roundId"] = round_id
        r = self.eval(f"__rawAct({json.dumps(role)}, {json.dumps(code)}, {json.dumps(body)})")
        pace(0.12)
        return r

    def storage(self):
        return self.eval("Object.fromEntries(Object.entries(localStorage))")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def main():
    repo, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    decoder = sys.argv[3] if len(sys.argv) > 3 else None
    os.makedirs(out, exist_ok=True)
    server_py = os.environ.get("MAJLIS_SERVER_PY") or sys.executable
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server_log = open(os.path.join(out, "server-stderr.log"), "w")
    server_cmd = [server_py, "-m", "party.server", "--port", str(port), "--web-dir", "party/web", "--content-dir", "party/content", "--access-log", "--exit-on-stdin-eof"]
    results["server_command"] = " ".join(server_cmd[1:])
    srv = subprocess.Popen(server_cmd, cwd=repo, stdin=subprocess.PIPE, stdout=server_log, stderr=server_log)
    results["server_pid"] = srv.pid
    results["base"] = base
    profile = tempfile.mkdtemp(prefix="majlis-party-real-")
    dport = free_port()
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={dport}", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                               "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                               "--window-size=1280,800", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results["started_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tabs = {}
    net_log = []  # CDP request metadata from every tab, Authorization redacted
    secret_tokens = {}
    try:
        for _ in range(100):
            try:
                st, _, body = http_get(base + "/api/health")
                if st == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        results["health"] = json.loads(body)
        check("real server answers /api/health on loopback", st == 200 and results["health"].get("ok") is True, results["health"])
        for _ in range(100):
            try:
                ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json/version")); break
            except Exception:
                time.sleep(0.2)
        results["chrome"] = ver.get("Browser")
        browser = Tab(ver["webSocketDebuggerUrl"], "browser")

        # ---------- server-side locator rules, raw HTTP ----------
        raw = {}
        for path in ["/host.html", "/host.html?code=ABCDEF", "/tv.html?code=ABCDEF", "/play.html?code=ABCDEF", "/play.html?code=ABCDEF&sim=1",
                     "/js/sim-transport.js", "/sim/sim-worker.js", "/tests/real_e2e.py", "/js/", "/css/party.css", "/js/qr.js"]:
            st, hd, bd = http_get(base + path)
            raw[path] = {"status": st, "csp": hd.get("Content-Security-Policy"), "body": bd[:120] if st >= 400 else None}
        results["static_probe"] = raw
        check("host/TV refuse a query string; player accepts ?code only; sim flag refused",
              raw["/host.html"]["status"] == 200 and raw["/host.html?code=ABCDEF"]["status"] == 400 and raw["/tv.html?code=ABCDEF"]["status"] == 400
              and raw["/play.html?code=ABCDEF"]["status"] == 200 and raw["/play.html?code=ABCDEF&sim=1"]["status"] == 400,
              {k: v["status"] for k, v in raw.items()})
        check("sim adapter, sim worker, tests and directory listing are not served",
              all(raw[p]["status"] == 404 for p in ["/js/sim-transport.js", "/sim/sim-worker.js", "/tests/real_e2e.py", "/js/"]),
              {p: raw[p]["status"] for p in ["/js/sim-transport.js", "/sim/sim-worker.js", "/tests/real_e2e.py", "/js/"]})
        check("pages are served with the server's CSP (script-src 'self', connect-src 'self')",
              "script-src 'self'" in (raw["/host.html"]["csp"] or "") and "connect-src 'self'" in (raw["/host.html"]["csp"] or ""), raw["/host.html"]["csp"])

        def new_tab(name, vp, isolated):
            if isolated:
                ctx = browser.call("Target.createBrowserContext", disposeOnDetach=False)["browserContextId"]
                tid = browser.call("Target.createTarget", url="about:blank", browserContextId=ctx, newWindow=True)["targetId"]
            else:
                ctx = None
                tid = browser.call("Target.createTarget", url="about:blank")["targetId"]
            t = Tab(f"ws://127.0.0.1:{dport}/devtools/page/{tid}", name)
            t.context = ctx or "default"
            t.call("Page.enable"); t.call("Runtime.enable"); t.call("Network.enable")
            t.call("Page.addScriptToEvaluateOnNewDocument", source=NET_HOOK)
            t.call("Emulation.setFocusEmulationEnabled", enabled=True)  # each phone is its own focused screen
            t.viewport(vp)
            tabs[name] = t
            return t

        def collect_net():
            for t in tabs.values():
                for e in t.events:
                    if e.get("method") == "Network.requestWillBeSent":
                        rq = e["params"]["request"]
                        hd = {k: ("<redacted bearer>" if k.lower() == "authorization" else v) for k, v in rq.get("headers", {}).items()}
                        net_log.append({"tab": t.name, "method": rq["method"], "url": rq["url"], "headers": hd,
                                        "hadAuthorization": any(k.lower() == "authorization" for k in rq.get("headers", {}))})
                t.events = [e for e in t.events if e.get("method") != "Network.requestWillBeSent"]

        # ---------- operator creates a room ----------
        host = new_tab("host", LAPTOP, isolated=False)
        host.goto(base + "/host.html")
        check("no sim ribbon and no sim transport on the real server", host.eval("!document.querySelector('.sim-ribbon') && !window.__majlisSimTransport"))
        host.shot(out, "01-host-start-1280")
        host.click("__btn('Create room')", "Create room")
        code = host.wait("(document.querySelector('.room-code')||{}).textContent", what="room code")
        check("room created by the server with a 6-char code", re.fullmatch(r"[A-Z0-9]{6}", code or "") is not None, code)
        loc = host.eval("({ href: location.href, search: location.search, hash: location.hash })")
        check("host locator is host.html#code=ROOM (fragment, no query)", loc["search"] == "" and loc["hash"] == "#code=" + code, loc)
        st = host.storage()
        for role in ("operator", "display"):
            secret_tokens[role + ":" + code] = json.loads(st[f"majlisParty.v1.{role}.{code}"])["token"]
        join_url = host.eval("document.querySelector('[data-join-url]').getAttribute('data-join-url')")
        check("QR join URL is exactly play.html?code=ROOM", join_url == f"{base}/play.html?code={code}", join_url)

        def decode_qr(t, label, expected):
            rect = t.eval("(() => { const r = document.querySelector('.qr').getBoundingClientRect(); return {x:r.x, y:r.y + scrollY, width:r.width, height:r.height}; })()")
            png = os.path.join(out, f"qr-{label}.png")
            open(png, "wb").write(base64.b64decode(t.call("Page.captureScreenshot", format="png", captureBeyondViewport=True, clip=dict(rect, scale=2))["data"]))
            got = None
            if decoder:
                r = subprocess.run([decoder, png], capture_output=True, text=True, timeout=60)
                msgs = json.loads(r.stdout.strip().splitlines()[-1])["messages"] if r.stdout.strip() else []
                got = msgs[0] if msgs else None
                check(f"QR ({label}) decodes with CoreImage to the join link only", msgs == [expected], msgs)
            for attempt in range(4):
              bd = t.eval("""(async () => { if (!('BarcodeDetector' in window)) return 'unsupported';
              const svg = document.querySelector('.qr svg'); const img = new Image();
              img.src = 'data:image/svg+xml;base64,' + btoa(new XMLSerializer().serializeToString(svg));
              await img.decode(); const c = document.createElement('canvas'); c.width = c.height = 600;
              const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0,0,600,600); x.drawImage(img, 0, 0, 600, 600);
              const r = await new BarcodeDetector({formats:['qr_code']}).detect(c); return r.map(v => v.rawValue); })()""")
              if bd:
                  break
              time.sleep(0.5)
            results.setdefault("barcode_detector_attempts", {})[label] = attempt + 1
            results.setdefault("barcode_detector", {})[label] = bd
            if bd != "unsupported":
                check(f"QR ({label}) decodes with Chrome BarcodeDetector to the join link only", bd == [expected], bd)
            return got or expected

        decoded = decode_qr(host, "room1", join_url)

        # ---------- TV: same browser context as the operator ----------
        tv = new_tab("tv", TV, isolated=False)
        tv.goto(f"{base}/tv.html#code={code}")
        tv.wait("!!document.querySelector('.tv-lobby')", what="tv lobby")
        check("TV authenticates with the display credential kept in the operator's browser", True)

        # ---------- players, each in its own isolated browser context ----------
        names = ["Aisha", "Omar", "Noora"]
        vps = {"Aisha": PHONE, "Omar": PHONE, "Noora": SMALL}
        players = {}
        for nm in names:
            p = new_tab(nm, vps[nm], isolated=True)
            if nm == "Omar":  # types the code instead of scanning
                p.goto(base + "/play.html")
                p.type_into("#join-code", code.lower())
            else:
                p.goto(decoded)
                check(f"{nm}: join form code prefilled from the decoded QR link", p.eval("document.getElementById('join-code').value") == code)
            if nm == "Aisha":
                p.shot(out, "02-player-join-393")
            p.type_into("#join-name", nm)
            p.click("__btn('Join game')", "Join game")
            p.wait("document.body.textContent.includes('You’re in')", what="lobby")
            players[nm] = p
            pace(0.3)
        pid = {}
        for nm, p in players.items():
            stg = p.storage()
            keys = [k for k in stg if k.startswith("majlisParty.")]
            rec = json.loads(stg[f"majlisParty.v1.player.{code}"])
            secret_tokens[f"player:{nm}:{code}"] = rec["token"]
            pid[nm] = rec["playerId"]
            check(f"{nm}: isolated storage holds only this phone's player seat", sorted(keys) == sorted([f"majlisParty.v1.player.{code}", "majlisParty.v1.last.player"]), keys)
        check("Omar joined by typing the code (lower case normalised)", players["Omar"].eval("location.search") == f"?code={code}")
        check("three distinct player tokens and ids issued by the server", len({secret_tokens[f'player:{n}:{code}'] for n in names}) == 3 and len(set(pid.values())) == 3, pid)
        check("operator context holds no player seat", not any(".player." in k for k in host.storage()), sorted(host.storage()))
        players["Aisha"].shot(out, "03-player-lobby-393")
        name_of = {v: k for k, v in pid.items()}

        # Join errors through the UI (an extra, isolated phone).
        x = new_tab("extra", PHONE, isolated=True)
        x.goto(join_url)
        x.type_into("#join-name", "aisha")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="name taken")
        check("name taken (case-insensitive) shown, code kept", "already in this room" in err and x.eval("document.getElementById('join-code').value") == code, err)
        x.shot(out, "04-player-error-name-taken")
        x.goto(f"{base}/play.html?code=ZZZZ22")
        x.type_into("#join-name", "Salman")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="not found")
        check("wrong code: server room_not_found shown in plain words", "no longer exists" in err, err)
        st_ab = http_get(f"{base}/play.html?code=AB")[0]
        check("server refuses a malformed ?code= query (400)", st_ab == 400, st_ab)
        x.goto(f"{base}/play.html")
        x.type_into("#join-code", "AB")
        x.type_into("#join-name", "Shorty")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="code shape")
        check("malformed code rejected client-side", "6 letters or numbers" in err, err)
        full = x.eval("""(async () => { const H = { 'Content-Type': 'application/json', Accept: 'application/json' };
          const r = await (await fetch('/api/rooms', { method: 'POST', headers: H, body: '{}' })).json();
          const codes = []; for (let i = 0; i < 12; i++) { const j = await fetch('/api/rooms/' + r.code + '/join', { method: 'POST', headers: H, body: JSON.stringify({ name: 'Seat ' + i }) }); codes.push(j.status); }
          return { code: r.code, statuses: codes }; })()""")
        x.goto(f"{base}/play.html?code={full['code']}")
        x.type_into("#join-name", "Late")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="room full")
        check("room full (12 seats taken) shown", "full" in err and full["statuses"] == [201] * 12, {"err": err, "statuses": full["statuses"]})
        x.shot(out, "04b-player-error-full")

        tv.wait("document.querySelectorAll('.tv-lobby .chip').length === 3", what="tv sees 3 players")
        tv.shot(out, "05-tv-lobby-1920")
        host.wait("document.querySelectorAll('.chip').length === 3", what="host sees players")
        host.shot(out, "06-host-lobby-1280", full=True)

        # ================= Who Am I =================
        host.click("document.querySelector('[data-mode=who_am_i]')", "who_am_i tile")
        host.wait("document.querySelector('[data-mode=who_am_i]').getAttribute('aria-pressed') === 'true'", what="mode chosen")
        host.click("__btn('Start Who Am I')", "Start")
        host.wait("document.querySelectorAll('.who-row').length === 3", what="host who am i")
        hs = host.last_snap()
        r1 = hs["roundId"]
        assign = hs["game"]["assignments"]
        check("server started Who Am I round g1r1 with an assignment per player id", r1 == "g1r1" and sorted(assign) == sorted(pid.values()), {"roundId": r1, "keys": sorted(assign)})
        labels = {name_of[k]: v for k, v in assign.items()}

        def secrets_of(a):
            return [s for s in (a.get("label"), a.get("ar")) if s]

        def who_privacy(stage):
            for nm, p in players.items():
                raw_all = decoded_text(p.rx_raw())
                dom = p.html()
                own = secrets_of(labels[nm])
                you = (p.last_snap()["game"] or {}).get("you", {})
                if not you.get("revealed"):
                    check(f"[{stage}] {nm}: own character absent from every received byte and the DOM", not any(s in raw_all or s in dom for s in own), own)
            hraw = decoded_text(host.rx_raw())
            check(f"[{stage}] positive control: the scan finds every character in the operator's bytes", all(s in hraw for a in labels.values() for s in secrets_of(a)))

        for nm, p in players.items():
            p.wait("!!document.querySelector('.my-card')", what="who am i")
            others = [labels[o]["label"] for o in names if o != nm]
            check(f"{nm}: sees the other two characters", all(o in p.html() for o in others), others)
        tv.wait("!!document.querySelector('.tv-who')", what="tv who")
        traw = decoded_text(tv.rx_raw()) + tv.html()
        check("TV received and shows no character before any reveal", not any(s in traw for a in labels.values() for s in secrets_of(a)))
        who_privacy("asking")
        players["Aisha"].shot(out, "07-player-whoami-393", full=True)
        players["Noora"].shot(out, "07b-player-whoami-360")
        tv.shot(out, "08-tv-whoami-1920")

        # Planted guesses: unique strings that must reach only the operator and their owner.
        plant = {k: f"PLANT-{k}-{secrets.token_hex(4)}" for k in ("A1", "A2", "N1", "O1")}
        # Empty guess is refused client-side and sends nothing.
        n = players["Noora"]
        before = len(n.tx_actions())
        n.type_into("#guess", "   ")
        n.click("__btn('Send guess')", "empty guess")
        err = n.wait("(document.getElementById('guess-error')||{}).textContent", what="empty guess error")
        check("empty guess: clear message, no request sent", "Type who you think you are first" in err and len(n.tx_actions()) == before, err)
        a = players["Aisha"]
        a.type_into("#guess", plant["A1"])
        a.click("__btn('Send guess')", "guess A1")
        a.wait(f"document.body.textContent.includes({json.dumps(plant['A1'])}) && document.body.textContent.includes('is with the host')", what="guess pending")
        host.wait(f"document.body.textContent.includes({json.dumps(plant['A1'])})", what="host sees guess")
        tx = a.tx_actions()[-1]
        check("guess sent as whoami.guess with the round it was typed in", tx["type"] == "whoami.guess" and tx["roundId"] == r1 and tx["payload"] == {"text": plant["A1"]}, tx)
        host.shot(out, "09-host-whoami-guess-1280", full=True)
        # Ready is a separate control and sends no guess.
        o = players["Omar"]
        o.click("__btn('I’m ready')", "ready")
        host.wait(f"!!document.querySelector('.who-row[data-player=\"{pid['Omar']}\"] .badge-warn')", what="host sees Omar ready")
        tx = o.tx_actions()[-1]
        check("Ready sends whoami.ready only (no guess)", tx["type"] == "whoami.ready" and tx["payload"] == {"ready": True} and "Omar" not in json.dumps(host.last_snap()["game"]["guesses"]) and pid["Omar"] not in host.last_snap()["game"]["guesses"], tx)
        # Dismiss (wrong guess) is private; Aisha is told and can resubmit.
        host.click(f"__lbl('Not it: clear Aisha’s guess')", "dismiss")
        a.wait("(document.getElementById('guess-error')||{}).textContent.includes('Not quite')", what="dismiss notice")
        check("dismiss: Aisha told privately, pending guess gone from her snapshot", a.last_snap()["game"]["you"]["guess"] is None)
        a.shot(out, "10-player-guess-dismissed-393")
        a.type_into("#guess", plant["A2"])
        a.click("__btn('Send guess')", "guess A2")
        host.wait(f"document.body.textContent.includes({json.dumps(plant['A2'])})", what="host sees second guess")
        n.type_into("#guess", plant["N1"])
        n.click("__btn('Send guess')", "guess N1")
        host.wait(f"document.body.textContent.includes({json.dumps(plant['N1'])})", what="host sees Noora guess")
        # Correct: award reveals that one player only.
        host.click("__lbl('Correct: give Aisha 1 point')", "award Aisha")
        a.wait(f"document.querySelector('.my-card.revealed') && document.querySelector('.my-card').textContent.includes({json.dumps(labels['Aisha']['label'])})", what="Aisha own reveal")
        a.wait("document.querySelector('.me-chip').textContent.endsWith('· 1')", what="Aisha score 1")
        check("award: Aisha's score 1 read from players[] in her snapshot", [p["score"] for p in a.last_snap()["players"] if p["id"] == pid["Aisha"]] == [1])
        tv.wait(f"document.body.textContent.includes({json.dumps(labels['Aisha']['label'])})", what="tv shows Aisha")
        tvt = decoded_text(tv.rx_raw())
        check("TV shows only the revealed player's character", not any(s in tvt for nm in ("Omar", "Noora") for s in secrets_of(labels[nm])))
        who_privacy("after individual award")
        # Individual reveal (confirm) for Omar.
        host.click("__lbl('Reveal Omar’s character to everyone')", "reveal Omar")
        host.confirm(True)
        o.wait(f"document.querySelector('.my-card.revealed') && document.querySelector('.my-card').textContent.includes({json.dumps(labels['Omar']['label'])})", what="Omar own reveal")
        tx = host.tx_actions()[-1]
        check("individual reveal sends whoami.reveal {playerId} bound to the round", tx["type"] == "whoami.reveal" and tx["payload"] == {"playerId": pid["Omar"]} and tx["roundId"] == r1, tx)
        check("Noora still cannot see her own character", labels["Noora"]["label"] not in decoded_text(n.rx_raw()) and n.eval("!document.querySelector('.my-card.revealed')"))
        who_privacy("after individual reveal")

        # Negative controls on the wire: wrong round, missing round, wrong role.
        bad = a.raw_act("player", code, "whoami.guess", {"text": "stale"}, round_id="g1r99")
        check("wrong-round player action refused 409 stale_round", bad["status"] == 409 and bad["body"]["error"]["code"] == "stale_round", bad)
        bad = n.raw_act("player", code, "whoami.guess", {"text": "no round"}, omit_round=True)
        check("action without roundId refused 400 round_required", bad["status"] == 400 and bad["body"]["error"]["code"] == "round_required", bad)
        bad = o.raw_act("player", code, "whoami.award", {"playerId": pid["Omar"]}, round_id=r1)
        check("player cannot send an operator action (403 forbidden)", bad["status"] == 403 and bad["body"]["error"]["code"] == "forbidden", bad)
        bad = host.raw_act("operator", code, "whoami.dismiss", {"playerId": pid["Omar"]}, round_id=r1)
        check("dismiss with no pending guess refused 409 no_pending_guess", bad["status"] == 409 and bad["body"]["error"]["code"] == "no_pending_guess", bad)
        scores_now = {p["id"]: p["score"] for p in host.last_snap()["players"]}
        check("negative controls changed no score", scores_now == {pid["Aisha"]: 1, pid["Omar"]: 0, pid["Noora"]: 0}, scores_now)

        # Global reveal.
        host.click("__btn('Reveal all')", "reveal all")
        host.confirm(True)
        for nm, p in players.items():
            p.wait(f"document.querySelector('.my-card.revealed') && document.querySelector('.my-card').textContent.includes({json.dumps(labels[nm]['label'])})", what=f"{nm} own reveal")
        check("global reveal: every player now sees their own character", True)
        hs = host.last_snap()
        check("server roundPhase=revealed and guesses are kept for the operator after reveal",
              hs["game"]["roundPhase"] == "revealed" and hs["game"]["guesses"].get(pid["Noora"]) == plant["N1"], {"phase": hs["game"]["roundPhase"], "guesses": list(hs["game"]["guesses"])})
        tv.wait(f"document.body.textContent.includes({json.dumps(labels['Noora']['label'])})", what="tv full reveal")
        players["Noora"].shot(out, "11-player-whoami-revealed-360")
        tv.shot(out, "12-tv-whoami-revealed-1920")
        host.shot(out, "13-host-whoami-revealed-1280", full=True)

        # Guess plaintext privacy, after reveal too: only the operator and the owner ever got it.
        owners = {"A1": "Aisha", "A2": "Aisha", "N1": "Noora"}
        for k, owner in owners.items():
            for nm, p in players.items():
                seen = plant[k] in decoded_text(p.rx_raw()) or plant[k] in p.html()
                if nm == owner:
                    check(f"guess {k}: owner {nm} received it", seen)
                else:
                    check(f"guess {k}: {nm} never received it (bytes + DOM, after reveal)", not seen)
            check(f"guess {k}: TV never received it", plant[k] not in decoded_text(tv.rx_raw()) and plant[k] not in tv.html())
            check(f"guess {k}: positive control - operator bytes contain it", plant[k] in decoded_text(host.rx_raw()))

        # Next round, then a stale confirmation that must be cancelled unsent.
        host.eval("window.__tx = []; true")
        host.click("__btn('Next round')", "next round")
        host.wait("(() => { const s = document.querySelector('.op-play'); return s && document.querySelector('.pill') && !document.body.textContent.includes('Everyone’s characters'); })()", what="round 2")
        hs = host.last_snap()
        r2 = hs["roundId"]
        tx = host.tx_actions()
        check("Next round sends round.next with the round it was shown for; server moves to g1r2",
              [t["type"] for t in tx] == ["round.next"] and tx[0]["roundId"] == r1 and r2 == "g1r2", {"tx": tx, "r2": r2})
        check("new round clears guesses", hs["game"]["guesses"] == {} and hs["game"]["pendingGuesses"] == [], hs["game"]["guesses"])
        labels = {name_of[k]: v for k, v in hs["game"]["assignments"].items()}
        marks = {nm: p.rx_count() for nm, p in players.items()}
        for nm, p in players.items():
            p.wait("!document.querySelector('.my-card.revealed')", what=f"{nm} round 2 hidden")
        dup = host.raw_act("operator", code, "round.next", {}, round_id=r1)
        check("duplicate round.next for the old round refused 409 stale_round", dup["status"] == 409 and dup["body"]["error"]["code"] == "stale_round", dup)
        host.eval("window.__tx = []; true")
        host.click("__btn('Reveal all')", "reveal all (to be cancelled)")
        host.wait("!!document.querySelector('dialog.confirm[open]')", what="confirm open")
        moved = host.raw_act("operator", code, "round.next", {}, round_id=r2)  # a second operator device moves on
        host.wait("!document.querySelector('dialog.confirm[open]')", what="stale dialog cancelled")
        toast_txt = host.wait("(document.getElementById('toast')||{}).textContent", what="toast")
        tx = host.tx_actions()
        check("stale confirm: dialog cancelled unsent when the round changed",
              moved["status"] == 200 and moved["body"]["accepted"] is True and not any(t["type"] == "whoami.reveal" for t in tx) and "Round changed." in toast_txt and "was cancelled" in toast_txt,
              {"moved": moved, "tx": tx, "toast": toast_txt})
        hs = host.wait("(() => true)()") and host.last_snap()
        r3 = hs["roundId"]
        check("server is on g1r3 with nothing revealed", r3 == "g1r3" and hs["game"]["revealed"] == [], {"r": r3, "revealed": hs["game"]["revealed"]})
        labels = {name_of[k]: v for k, v in hs["game"]["assignments"].items()}
        for nm, p in players.items():
            p.wait(f"(() => {{ const t = document.querySelector('.mode-heading, h1, h2'); return document.body.textContent.includes('Round 3'); }})()", what=f"{nm} round 3")
            later = decoded_text(p.rx_raw(marks[nm]))
            check(f"[round 3] {nm}: own new character absent from bytes since round 2 began", not any(s in later for s in secrets_of(labels[nm])), labels[nm]["label"])

        # Pause / resume.
        host.click("document.getElementById('pause-btn')", "pause")
        for nm, p in players.items():
            p.wait("!!document.querySelector('.overlay, #paused-panel, .paused')", what=f"{nm} paused")
        tv.wait("!!document.querySelector('.overlay, #paused-panel, .paused')", what="tv paused")
        bad = o.raw_act("player", code, "whoami.guess", {"text": "while paused"}, round_id=r3)
        check("action while paused refused 409 paused", bad["status"] == 409 and bad["body"]["error"]["code"] == "paused", bad)
        players["Omar"].shot(out, "14-player-paused-393")
        host.shot(out, "15-host-paused-1280")
        host.click("document.getElementById('resume-btn')", "resume")
        for nm, p in players.items():
            p.wait("!document.querySelector('.overlay, #paused-panel, .paused')", what=f"{nm} resumed")
        check("resume clears the pause on every phone", True)

        # Join attempt while a game is in progress.
        x.goto(join_url)
        x.type_into("#join-name", "Latecomer")
        x.click("__btn('Join game')", "Join game")
        err = x.wait("(document.getElementById('join-error')||{}).textContent", what="in progress")
        check("join during a game: 'A game is in progress' shown", "game is in progress" in err, err)

        # Refresh in every role.
        a.reload()
        a.wait("!!document.querySelector('.my-card')", what="Aisha after refresh")
        check("player refresh resumes the same seat (same player id, same score)",
              a.last_snap()["self"]["id"] == pid["Aisha"] and a.eval("document.querySelector('.me-chip').textContent.endsWith('· 1')"), a.last_snap()["self"])
        host.reload()
        host.wait("document.querySelectorAll('.who-row').length === 3", what="host after refresh")
        check("operator refresh resumes the room from host.html#code=ROOM", host.eval("location.hash") == "#code=" + code and host.last_snap()["roundId"] == r3)
        tv.reload()
        tv.wait("!!document.querySelector('.tv-who')", what="tv after refresh")
        check("TV refresh resumes from tv.html#code=ROOM", tv.last_snap()["role"] == "display")

        # Offline, reconnecting banner with last update, Retry.
        o.call("Network.emulateNetworkConditions", offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        o.wait("document.body.dataset.conn === 'reconnecting'", timeout=15, what="reconnecting")
        o.wait("!!document.querySelector('#conn .conn-retry')", what="retry button")
        check("offline: reconnecting banner, last update still shown", "Reconnecting" in o.eval("document.getElementById('conn').textContent") and o.eval("!!document.querySelector('.my-card')"))
        o.shot(out, "16-player-offline-393")
        n_try = sum(1 for t in o.eval("window.__tx") if t["path"].endswith("/events"))
        o.click("document.querySelector('#conn .conn-retry')", "Retry now (still offline)")
        o.wait(f"window.__tx.filter(t => t.path.endsWith('/events')).length > {n_try}", what="retry attempt")
        time.sleep(0.4)
        check("Retry while still offline makes an immediate attempt and stays on 'Reconnecting'", o.eval("document.body.dataset.conn") == "reconnecting")
        # A second operator device scores while Omar is offline; he must catch up on reconnect.
        host.raw_act("operator", code, "whoami.award", {"playerId": pid["Noora"]}, round_id=r3)
        o.call("Network.emulateNetworkConditions", offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        o.wait("document.body.dataset.conn === 'live'", timeout=15, what="live again")
        last_ids = [t["lastEventId"] for t in o.eval("window.__tx") if t["path"].endswith("/events")]
        o.wait("[...document.querySelectorAll('.score-row')].some(r => r.textContent.includes('Noora') && r.textContent.trim().endsWith('1'))", what="Omar caught up")
        check("back online: reconnects by itself, resumes with Last-Event-ID and shows what changed meanwhile", o.eval("document.body.dataset.conn") == "live" and any(last_ids), last_ids[-3:])

        # End game.
        host.click("__btn('End game')", "end game")
        host.confirm(True)
        host.wait("document.body.textContent.includes('Game over')", what="host ended")
        for nm, p in players.items():
            p.wait("document.body.textContent.includes('Game over')", what=f"{nm} ended")
        tv.wait("!!document.querySelector('.tv-ended')", what="tv ended")
        final = {p["name"]: p["score"] for p in host.last_snap()["players"]}
        check("final Who Am I scores agree on every client and match the one award", all({q["name"]: q["score"] for q in t.last_snap()["players"]} == final for t in list(players.values()) + [tv]) and final == {"Aisha": 1, "Omar": 0, "Noora": 1}, final)
        players["Aisha"].shot(out, "17-player-ended-393", full=True)
        host.shot(out, "18-host-ended-1280")
        # Hide/show and network changes after the end must not reconnect.
        time.sleep(0.5)
        nev = {nm: sum(1 for t in p.eval("window.__tx") if t["path"].endswith("/events")) for nm, p in players.items()}
        for p in players.values():
            p.call("Emulation.setFocusEmulationEnabled", enabled=True)
            p.eval("Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' }); document.dispatchEvent(new Event('visibilitychange'));"
                   "Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' }); document.dispatchEvent(new Event('visibilitychange')); true")
        o.call("Network.emulateNetworkConditions", offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        time.sleep(1.0)
        o.call("Network.emulateNetworkConditions", offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        time.sleep(2.0)
        nev2 = {nm: sum(1 for t in p.eval("window.__tx") if t["path"].endswith("/events")) for nm, p in players.items()}
        check("after the game ends, hide/show and offline/online open no new stream", nev == nev2 and all(p.eval("document.body.textContent.includes('Game over')") for p in players.values()), {"before": nev, "after": nev2})

        # "Start a new room" closes this room on the server.
        host.click("__btn('Start a new room')", "start a new room")
        host.wait("!!__btn('Create room')", what="host start page")
        tx = host.tx_actions()[-1]
        check("Start a new room sends room.close and forgets the room on this device",
              tx["type"] == "room.close" and not any(k.endswith("." + code) for k in host.storage()) and host.eval("location.hash") == "", tx)
        # Aisha kept her seat (Done keeps it); going back to the room shows it was closed.
        a.click("__btn('Done')", "Done")
        a.click(f"__btn('Back to room {code}')", "back to room")
        a.wait("document.body.textContent.includes('Room closed')", what="closed card")
        check("player returning to a closed room is told the host closed it, and the seat is dropped",
              f"majlisParty.v1.player.{code}" not in a.storage())
        a.shot(out, "19-player-room-closed-393")
        tv.wait("document.body.textContent.includes('closed') || document.body.textContent.includes('no longer exists') || !!document.querySelector('.tv-ended')", what="tv after close")
        collect_net()

        # ================= Feud (new room: changing game means a new room in this build) =================
        host.click("__btn('Create room')", "Create room 2")
        code2 = host.wait(f"(() => {{ const c = (document.querySelector('.room-code')||{{}}).textContent; return c && c !== {json.dumps(code)} && c; }})()", what="room 2 code")
        st = host.storage()
        for role in ("operator", "display"):
            secret_tokens[role + ":" + code2] = json.loads(st[f"majlisParty.v1.{role}.{code2}"])["token"]
        join2 = f"{base}/play.html?code={code2}"
        decoded2 = decode_qr(host, "room2", join2)
        tv.eval(f"location.hash = 'code={code2}'; true")
        time.sleep(0.5)
        tv.wait("document.readyState === 'complete'", what="tv reload")
        tv.eval(HELPERS)
        tv.wait("!!document.querySelector('.tv-lobby')", what="tv lobby 2")
        check("TV follows a new #code= in place (hashchange)", tv.eval("location.hash") == "#code=" + code2)
        a.click("__btn('Join another room')", "join another room")
        for nm in names:
            p = players[nm]
            if nm != "Aisha":
                p.goto(decoded2)
            else:
                p.type_into("#join-code", code2)
            p.type_into("#join-name", nm)
            p.click("__btn('Join game')", "Join game")
            p.wait("document.body.textContent.includes('You’re in')", what=f"{nm} lobby 2")
            pace(0.3)
        for nm, p in players.items():
            rec = json.loads(p.storage()[f"majlisParty.v1.player.{code2}"])
            secret_tokens[f"player:{nm}:{code2}"] = rec["token"]
            pid[nm] = rec["playerId"]
        name_of = {v: k for k, v in pid.items()}
        host.wait("document.querySelectorAll('.chip').length === 3", what="host sees 3 in room 2")
        host.click("document.querySelector('[data-mode=feud]')", "feud tile")
        host.wait("document.querySelector('[data-mode=feud]').getAttribute('aria-pressed') === 'true'", what="feud chosen")
        host.click("__btn('Start Majlis Feud')", "Start Feud")
        host.wait("!!document.querySelector('.feud-op')", what="host feud")
        hs = host.last_snap()
        b1 = hs["roundId"]
        board = hs["game"]["answers"]
        check("server started Feud board g1b1 with 6 house answers (operator projection)", b1 == "g1b1" and len(board) == 6 and hs["game"]["label"] == "House answers", {"roundId": b1, "n": len(board)})
        for nm, p in players.items():
            p.wait("!!document.querySelector('.feud-board')", what=f"{nm} feud")
        tv.wait("!!document.querySelector('.tv-feud')", what="tv feud")
        ps = players["Noora"].last_snap()["game"]
        check("player projection: 6 zero-based hidden slots, no answers[] list", [s["index"] for s in ps["slots"]] == list(range(6)) and not any(s["revealed"] for s in ps["slots"]) and "answers" not in ps and "boardId" not in ps, ps["slots"])

        def feud_privacy(stage, board):
            shown = {s["id"] for s in host.last_snap()["game"]["slots"] if s["revealed"]}
            hraw = decoded_text(host.rx_raw())
            for t in list(players.values()) + [tv]:
                raw_t = decoded_text(t.rx_raw())
                dom = t.eval("document.body.innerText")
                hidden_leak = [a["text"] for a in board if a["id"] not in shown and (json.dumps(a["text"], ensure_ascii=False) in raw_t or a["text"] in dom)]
                alias_leak = [al for a in board for al in a["aliases"] if json.dumps(al, ensure_ascii=False) in raw_t]
                check(f"[{stage}] {t.name}: no hidden answer and no alias received or shown", not hidden_leak and not alias_leak, {"hidden": hidden_leak, "aliases": alias_leak})
            check(f"[{stage}] positive control: operator bytes contain every answer and alias",
                  all(json.dumps(a["text"], ensure_ascii=False) in hraw and all(json.dumps(al, ensure_ascii=False) in hraw for al in a["aliases"]) for a in board))

        feud_privacy("board 1 closed", board)
        players["Aisha"].shot(out, "20-player-feud-393")
        host.shot(out, "21-host-feud-1280", full=True)
        tv.shot(out, "22-tv-feud-1920")

        # Open buzzer, trusted-pointer buzz by Omar.
        host.click("__btn('Open buzzer')", "open buzzer")
        o.wait("document.getElementById('buzzer').classList.contains('state-open')", what="buzz open")
        o.shot(out, "23-player-buzz-open-393")
        o.click("document.getElementById('buzzer')", "buzz (trusted touch)", block="center")
        o.wait("document.getElementById('buzzer').classList.contains('state-won')", what="Omar won")
        for nm in ("Aisha", "Noora"):
            players[nm].wait("document.getElementById('buzzer').classList.contains('state-lost') && document.body.textContent.includes('Omar')", what=f"{nm} sees Omar")
        hs = host.last_snap()
        check("trusted buzz: server winner = Omar, control = Omar, phase answering",
              hs["game"]["buzzer"]["winner"] == pid["Omar"] and hs["game"]["control"] == pid["Omar"] and hs["game"]["roundPhase"] == "answering", hs["game"]["buzzer"])
        check("buzz was a trusted touch", pointer_log[-1]["delivered"]["trusted"] and pointer_log[-1]["delivered"]["pointerType"] == "touch", pointer_log[-1]["delivered"])
        o.shot(out, "24-player-buzz-won-393")
        # Reveal answer 1, three strikes, steal to Noora.
        host.click(f"__lbl('Reveal answer 1: ' + {json.dumps(board[0]['text'])})", "reveal 1")
        for t in list(players.values()) + [tv]:
            t.wait(f"document.body.innerText.includes({json.dumps(board[0]['text'])})", what=f"{t.name} sees answer 1")
        check("revealed answer 1 reaches every screen with its points; roundPoints from the server",
              host.last_snap()["game"]["roundPoints"] == board[0]["points"] and players["Noora"].last_snap()["game"]["slots"][0] == {"index": 0, "revealed": True, "id": board[0]["id"], "text": board[0]["text"], "points": board[0]["points"]},
              players["Noora"].last_snap()["game"]["slots"][0])
        for i in range(3):
            host.click("__btn('Strike')", f"strike {i + 1}")
            host.wait(f"(() => {{ const m = (window.__rx||[]).length; return true; }})()")
            pace(0.25)
        host.wait("!!document.getElementById('steal-player')", what="steal control")
        check("three strikes recorded by the server", host.last_snap()["game"]["strikes"] == 3, host.last_snap()["game"]["strikes"])
        check("steal list excludes the player in control", pid["Omar"] not in host.eval("Array.from(document.querySelectorAll('#steal-player option')).map(o => o.value)"))
        host.eval(f"(() => {{ const s = document.getElementById('steal-player'); s.value = {json.dumps(pid['Noora'])}; s.dispatchEvent(new Event('change')); return true; }})()")
        host.click("__btn('Give steal chance')", "steal")
        players["Noora"].wait("document.body.textContent.includes('Your chance to steal')", what="Noora steal")
        faces = {nm: p.eval("document.getElementById('buzzer').textContent + ' | ' + document.getElementById('buzz-sub').textContent") for nm, p in players.items()}
        check("steal: Noora's buzzer says Steal!, the others name Noora as stealing", faces["Noora"].startswith("Steal!") and all(faces[o].startswith("Noora | is trying to steal") for o in ("Aisha", "Omar")), faces)
        hs = host.last_snap()
        check("steal: server control = Noora, stealFrom = Omar, phase steal", hs["game"]["control"] == pid["Noora"] and hs["game"]["stealFrom"] == pid["Omar"] and hs["game"]["roundPhase"] == "steal", {k: hs["game"][k] for k in ("control", "stealFrom", "roundPhase")})
        players["Noora"].shot(out, "25-player-steal-360")
        host.click(f"__lbl('Reveal answer 3: ' + {json.dumps(board[2]['text'])})", "reveal 3")
        host.wait(f"document.querySelector('.bank-num').textContent === '{board[0]['points'] + board[2]['points']}'", what="bank 1+3")
        # Stale Award confirm: the bank changes while the dialog is open.
        host.eval("window.__tx = []; true")
        host.click(f"__lbl('Award the current {board[0]['points'] + board[2]['points']} points to Noora')", "award (to be cancelled)")
        dtext = host.wait("(document.querySelector('dialog.confirm[open]')||{}).textContent", what="award dialog")
        other = host.raw_act("operator", code2, "feud.reveal", {"answerId": board[1]["id"]}, round_id=b1)  # second operator device
        host.wait("!document.querySelector('dialog.confirm[open]')", what="award dialog cancelled")
        toast_txt = host.wait("(document.getElementById('toast')||{}).textContent", what="toast")
        check("stale Award confirm (bank changed while open) is cancelled unsent",
              "current " + str(board[0]["points"] + board[2]["points"]) + " points" in dtext and other["status"] == 200 and "Round points changed." in toast_txt
              and not any(t["type"] == "feud.award" for t in host.tx_actions()), {"dialog": dtext, "toast": toast_txt})
        expect_pts = board[0]["points"] + board[1]["points"] + board[2]["points"]
        host.wait(f"document.querySelector('.bank-num').textContent === '{expect_pts}'", what="bank updated")
        host.click(f"__lbl('Award the current {expect_pts} points to Noora')", "award Noora")
        info = host.confirm(True)
        players["Noora"].wait(f"document.querySelector('.me-chip').textContent.endsWith('· {expect_pts}')", what="Noora score")
        hs = host.last_snap()
        award_tx = [t for t in host.tx_actions() if t["type"] == "feud.award"]
        check("award: server gives the revealed sum to Noora, phase awarded; client sent only playerId",
              hs["game"]["awardedTo"] == pid["Noora"] and hs["game"]["awardedPoints"] == expect_pts and hs["game"]["roundPhase"] == "awarded"
              and award_tx and award_tx[-1]["payload"] == {"playerId": pid["Noora"]} and award_tx[-1]["roundId"] == b1, {"awarded": hs["game"]["awardedPoints"], "tx": award_tx})
        # Duplicate award: replaying the same actionId returns the original result; a new one is refused.
        rep = host.raw_act("operator", code2, "feud.award", {"playerId": pid["Noora"]}, round_id=b1, action_id=award_tx[-1]["actionId"])
        again = host.raw_act("operator", code2, "feud.award", {"playerId": pid["Aisha"]}, round_id=b1)
        sc = {p["id"]: p["score"] for p in host.last_snap()["players"]}
        check("duplicate award: same actionId replayed (X-Action-Replayed: 1), a fresh award refused round_awarded, scores unchanged",
              rep["status"] == 200 and rep["replayed"] == "1" and again["status"] == 409 and again["body"]["error"]["code"] == "round_awarded"
              and sc == {pid["Noora"]: expect_pts, pid["Omar"]: 0, pid["Aisha"]: 0}, {"replay": rep, "again": again, "scores": sc})
        bad = a.raw_act("player", code2, "feud.buzz", {}, round_id="g1b9")
        check("wrong-round buzz refused 409 stale_round", bad["status"] == 409 and bad["body"]["error"]["code"] == "stale_round", bad)
        host.click(f"__lbl('Show answer 4: ' + {json.dumps(board[3]['text'])} + ' (no points)')", "show 4 after award")
        players["Aisha"].wait(f"document.body.innerText.includes({json.dumps(board[3]['text'])})", what="answer 4 shown")
        check("showing an answer after the award adds no points", {p["id"]: p["score"] for p in host.last_snap()["players"]} == sc)
        feud_privacy("board 1 awarded", board)
        host.shot(out, "26-host-feud-awarded-1280", full=True)
        tv.shot(out, "27-tv-feud-awarded-1920")

        # Next question, then near-simultaneous buzz races: exactly one winner each time.
        host.click("__btn('Next question')", "next question")
        host.wait("document.querySelector('.bank-num').textContent === '0'", what="board 2")
        hs = host.last_snap()
        b2 = hs["roundId"]
        board2 = hs["game"]["answers"]
        check("next question: server board g1b2, strikes 0, buzzer closed", b2 == "g1b2" and hs["game"]["strikes"] == 0 and hs["game"]["roundPhase"] == "closed", {"r": b2})
        feud_privacy("board 2 closed", board2)
        races = []
        for race in range(3):
            host.click("__btn('Open buzzer')", f"open buzzer race {race + 1}")
            for p in players.values():
                p.wait("document.getElementById('buzzer').classList.contains('state-open')", what="open")
            for p in players.values():
                p.eval("window.__rxMark = window.__rx.length; window.__clickMark = window.__clicks.length; window.__touchMark = window.__touches.length; true")
            barrier = threading.Barrier(3)
            stamps = {}

            def fire(nm, p):
                info = p.eval("(() => __aim(document.getElementById('buzzer'), 'center'))()")
                barrier.wait()
                stamps[nm] = time.time()
                p._press(info["x"], info["y"], "touch")
            th = [threading.Thread(target=fire, args=(nm, p)) for nm, p in players.items()]
            [t.start() for t in th]
            [t.join() for t in th]
            for p in players.values():
                p.wait("!document.getElementById('buzzer').classList.contains('state-open') && !document.getElementById('buzzer').classList.contains('state-sending')", what="race settled")
            time.sleep(0.3)
            winner = host.last_snap()["game"]["buzzer"]["winner"]
            outcomes = {}
            for nm, p in players.items():
                acts = [r for r in p.rx()[p.eval("window.__rxMark"):] if r["path"].endswith("/actions")]
                outcomes[nm] = [(r["status"], json.loads("".join(r["chunks"]) or "{}").get("accepted"), (json.loads("".join(r["chunks"]) or "{}").get("error") or {}).get("code")) for r in acts]
            taps = {nm: p.eval("window.__clicks.slice(window.__clickMark).map(c => c.type + '/' + c.trusted + '/' + c.pointerType + '/' + c.id)") for nm, p in players.items()}
            touches = {nm: p.eval("window.__touches.slice(window.__touchMark)") for nm, p in players.items()}
            views = {nm: p.last_snap()["game"]["buzzer"]["winner"] for nm, p in players.items()}
            loser_toasts = {nm: p.eval("(() => { const t = document.getElementById('toast'); return t && t.classList.contains('show') ? t.textContent : null; })()") for nm, p in players.items()}
            won = [nm for nm, p in players.items() if p.eval("document.getElementById('buzzer').classList.contains('state-won')")]
            spread = max(stamps.values()) - min(stamps.values())
            races.append({"winner": name_of.get(winner), "outcomes": outcomes, "views": {k: name_of.get(v) for k, v in views.items()}, "won_ui": won, "taps": taps, "touches": touches, "toasts": loser_toasts, "dispatch_spread_s": round(spread, 4)})
            accepted = [nm for nm, oc in outcomes.items() if any(o[1] is True for o in oc)]
            # Every phone received a trusted touch on its buzzer. A phone whose screen had already
            # locked (winner shown) before its touch landed sends nothing - that is correct UI, not a
            # lost tap; every phone that did send got exactly one answer from the server.
            sent = [nm for nm, oc in outcomes.items() if oc]
            check(f"race {race + 1}: three trusted touches within {spread * 1000:.1f} ms, all three buzzes reached the server, exactly one accepted, every screen agrees",
                  winner is not None and accepted == [name_of[winner]] and len(won) == 1 and won == [name_of[winner]] and set(views.values()) == {winner}
                  and all(t[:1] == ["pointerdown/true/touch/buzzer"] for t in taps.values())
                  and not any(loser_toasts.values())
                  and all(len(oc) == 1 for oc in outcomes.values())
                  and all(oc[0][0] == 409 and oc[0][2] in ("buzzer_taken", "buzzer_closed") for nm, oc in outcomes.items() if nm != name_of[winner])
                  and tv.last_snap()["game"]["buzzer"]["winner"] == winner, races[-1])
            if race < 2:
                host.click("__btn('Reset buzzer')", "reset buzzer")
                host.confirm(True)
                host.wait("!!__btn('Open buzzer')", what="buzzer reset (Open buzzer enabled again)")
                pace(0.3)
        results["races"] = races
        contested = sum(1 for r in races if sum(1 for oc in r["outcomes"].values() if oc) == 3)
        check("every buzz race was contested at the server (3 buzzes, 1 winner)", contested == len(races), {"contested_races": contested, "of": len(races)})
        players["Noora"].shot(out, "28-player-race-result-360")

        # Stale round-bound confirm (Reset buzzer) when the operator's other device moves on.
        host.click("__btn('Reset buzzer')", "reset (to be cancelled)")
        host.wait("!!document.querySelector('dialog.confirm[open]')", what="reset dialog")
        win2 = host.last_snap()["game"]["buzzer"]["winner"]
        aw = host.raw_act("operator", code2, "feud.award", {}, round_id=b2)  # awards control (0 points) from another device
        nx = host.raw_act("operator", code2, "round.next", {}, round_id=b2)
        host.wait("!document.querySelector('dialog.confirm[open]')", what="reset dialog cancelled")
        toast_txt = host.wait("(document.getElementById('toast')||{}).textContent", what="toast")
        check("stale Reset confirm cancelled unsent after the board changed", aw["status"] == 200 and nx["status"] == 200 and "was cancelled" in toast_txt
              and not any(t["type"] == "feud.reset_buzzer" and t["roundId"] == b2 for t in host.tx_actions()[-3:]), {"toast": toast_txt})
        hs = host.last_snap()
        check("server on g1b3 after award(control, 0 pts) + round.next", hs["roundId"] == "g1b3" and {p["id"]: p["score"] for p in hs["players"]}[win2] == sc.get(win2, 0), hs["roundId"])

        # Hit-tests: landscape 734x393 phone; 360 and 393 were exercised above with trusted touches.
        n.viewport(LANDSCAPE)
        time.sleep(0.5)
        aim_l = n.eval("(() => __aim(document.getElementById('buzzer'), 'center'))()")
        check("734x393 landscape: buzzer reachable at its centre, no horizontal overflow", aim_l["ok"] and not n.eval("__overflow()"), aim_l)
        host.click("__btn('Open buzzer')", "open buzzer (landscape)")
        n.wait("document.getElementById('buzzer').classList.contains('state-open')", what="open (landscape)")
        n.click("document.getElementById('buzzer')", "buzz at 734x393", block="center")
        n.wait("document.getElementById('buzzer').classList.contains('state-won')", what="Noora won landscape")
        n.shot(out, "29-player-feud-734x393")
        n.reload()
        n.wait("!!document.querySelector('.feud-board')", what="Noora after refresh")
        check("player refresh mid-board keeps seat, score and server state", n.last_snap()["self"]["id"] == pid["Noora"] and n.last_snap()["game"]["buzzer"]["winner"] == pid["Noora"])
        n.viewport(SMALL)
        for nm, p in players.items():
            check(f"{nm}: no horizontal overflow at {p.vp['w']}px", not p.eval("__overflow()"))

        # Last board: content_exhausted is a server code with clear wording.
        hs = host.last_snap()
        rid = hs["roundId"]
        while hs["game"]["boardsRemaining"] > 0:
            r = host.raw_act("operator", code2, "round.next", {}, round_id=rid)
            pace(0.15)
            hs = host.wait(f"(() => true)()") and host.last_snap()
            if hs["roundId"] == rid:
                time.sleep(0.3); hs = host.last_snap()
            rid = hs["roundId"]
        host.wait("document.body.textContent.includes('Question 12 of 12')", what="last board")
        ex = host.raw_act("operator", code2, "round.next", {}, round_id=rid)
        check("round.next on the last board refused 409 content_exhausted", ex["status"] == 409 and ex["body"]["error"]["code"] == "content_exhausted", ex)
        host.click("__btn('Open buzzer')", "open buzzer last")
        a.wait("document.getElementById('buzzer').classList.contains('state-open')", what="open last")
        # Keyboard activation (Enter on the focused buzzer) - the path assistive tech and hardware keyboards use.
        a.call("Page.bringToFront")
        a.eval("document.getElementById('buzzer').focus(); window.__txMark = window.__tx.length; true")
        a.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r")
        a.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)
        host.wait("document.body.textContent.includes('Aisha buzzed first')", what="Aisha buzzed")
        kb = [json.loads(t["body"])["type"] for t in a.eval("window.__tx.slice(window.__txMark)") if t["body"]]
        check("buzzer works from the keyboard (Enter), sending exactly one feud.buzz", kb == ["feud.buzz"], kb)
        lb = host.last_snap()["game"]["answers"]
        host.click(f"__lbl('Reveal answer 2: ' + {json.dumps(lb[1]['text'])})", "reveal last 2")
        host.wait(f"document.querySelector('.bank-num').textContent === '{lb[1]['points']}'", what="bank last")
        host.click(f"__lbl('Award the current {lb[1]['points']} points to Aisha')", "award Aisha last")
        host.confirm(True)
        host.wait("document.body.textContent.includes('That was the last question')", what="last-question note")
        check("last board awarded: no Next button, clear note", host.eval("!__btn('Next question')"))

        # End game: final scores from the server agree everywhere and match the awards made.
        host.click("__btn('End game')", "end feud")
        host.confirm(True)
        for t in [host, tv] + list(players.values()):
            t.wait("document.body.textContent.includes('Game over') || !!document.querySelector('.tv-ended')", what=f"{t.name} ended")
        final = {p["name"]: p["score"] for p in host.last_snap()["players"]}
        expected = {"Noora": expect_pts, "Omar": 0, "Aisha": lb[1]["points"]}
        check("final Feud scores: server totals equal the awards made and agree on every client",
              final == expected and all({q["name"]: q["score"] for q in t.last_snap()["players"]} == final for t in list(players.values()) + [tv]), {"final": final, "expected": expected})
        players["Omar"].shot(out, "30-player-feud-ended-393", full=True)
        tv.shot(out, "31-tv-feud-ended-1920")
        host.click("__btn('Start a new room')", "start a new room 2")
        host.wait("!!__btn('Create room')", what="host start page 2")

        # ================= Lobby close: players are told, nothing reconnects =================
        host.click("__btn('Create room')", "Create room 3")
        code3 = host.wait(f"(() => {{ const c = (document.querySelector('.room-code')||{{}}).textContent; return c && c !== {json.dumps(code2)} && c; }})()", what="room 3 code")
        st = host.storage()
        for role in ("operator", "display"):
            secret_tokens[role + ":" + code3] = json.loads(st[f"majlisParty.v1.{role}.{code3}"])["token"]
        tv.eval(f"location.hash = 'code={code3}'; true")
        time.sleep(0.5)
        tv.wait("document.readyState === 'complete' && !!document.querySelector('.tv-lobby')", what="tv lobby 3")
        o.goto(f"{base}/play.html?code={code3}")
        o.type_into("#join-name", "Omar")
        o.click("__btn('Join game')", "Join game 3")
        o.wait("document.body.textContent.includes('You’re in')", what="Omar lobby 3")
        secret_tokens[f"player:Omar:{code3}"] = json.loads(o.storage()[f"majlisParty.v1.player.{code3}"])["token"]
        host.wait("document.querySelectorAll('.chip').length === 1", what="host sees Omar")
        host.click("__btn('Close room')", "close room")
        info = host.confirm(True)
        o.wait("document.body.textContent.includes('Room closed')", what="Omar told")
        tv.wait("document.body.textContent.includes('closed room')", what="tv told")
        n_ev = sum(1 for t in o.eval("window.__tx") if t["path"].endswith("/events"))
        time.sleep(2.5)
        n_ev2 = sum(1 for t in o.eval("window.__tx") if t["path"].endswith("/events"))
        check("lobby close: room.close sent; player and TV told; credentials dropped; no reconnect",
              any(t["type"] == "room.close" for t in host.tx_actions()) and f"majlisParty.v1.player.{code3}" not in o.storage()
              and f"majlisParty.v1.display.{code3}" not in host.storage() and n_ev == n_ev2, {"events_before": n_ev, "after": n_ev2})
        o.shot(out, "32-player-lobby-closed-393")
        tv.shot(out, "33-tv-closed-1920")
        post = o.raw_act("player", code3, "room.pause", {})
        check("closed room refuses further use (credential gone, server unauthorized/closed)", post["status"] in (401, 403, 404, 409), post)

        # ================= Token leak scan =================
        collect_net()
        server_log.flush()
        slog = open(os.path.join(out, "server-stderr.log")).read()
        toks = list(secret_tokens.values())
        leaks = {}
        for nm, t in tabs.items():
            surface = t.html() + "\n" + t.eval("location.href") + "\n" + t.console_text()
            hits = [k for k, v in secret_tokens.items() if v in surface]
            if hits:
                leaks[nm] = hits
        url_hits = [r["url"] for r in net_log if any(tok in r["url"] for tok in toks)]
        hdr_hits = [r for r in net_log if any(tok in json.dumps(r["headers"]) for tok in toks)]
        log_hits = [k for k, v in secret_tokens.items() if v in slog]
        check("no capability token in any DOM, page URL or console log", not leaks, leaks)
        check("no capability token in any request URL (CDP network log)", not url_hits, url_hits[:3])
        check("tokens only ever travelled in the Authorization header (redacted in this log)", not hdr_hits and any(r["hadAuthorization"] for r in net_log))
        check("no capability token in the server's access log", not log_hits and len(slog) > 0, log_hits)
        check("server access log has route templates only (no room codes in paths)", code not in slog and code2 not in slog, slog.splitlines()[:3])
        check("no page script errors in any role", not any(t.page_errors() for t in tabs.values()), {k: t.page_errors()[:2] for k, t in tabs.items() if t.page_errors()})

        # Redacted wire evidence: every byte each role received, tokens replaced.
        def redact(s):
            for k, v in secret_tokens.items():
                s = s.replace(v, f"<token {k.split(':')[0]}>")
            return s
        wire = {}
        for nm, t in tabs.items():
            wire[nm] = [{"path": r["path"].replace(code, "ROOM1").replace(code2, "ROOM2").replace(code3, "ROOM3"), "method": r["method"], "status": r["status"],
                         "bytes": redact("".join(r["chunks"]))} for r in t.rx()]
        json.dump(wire, open(os.path.join(out, "wire-received-redacted.json"), "w"), ensure_ascii=False, indent=1)
        json.dump(net_log, open(os.path.join(out, "cdp-requests-redacted.json"), "w"), ensure_ascii=False, indent=1)
        json.dump(pointer_log, open(os.path.join(out, "pointer-log.json"), "w"), ensure_ascii=False, indent=1)
        results["pointer_checks"] = {"total": len(pointer_log), "covered": sum(1 for p in pointer_log if not p["ok"]),
                                     "viewports": sorted({p["viewport"] for p in pointer_log}), "inputs": sorted({p["input"] for p in pointer_log})}
        check("every trusted click hit its own control (no covered control)", all(p["ok"] for p in pointer_log), results["pointer_checks"])
    except Exception as e:
        import traceback
        check("run completed without an exception", False, traceback.format_exc()[-1500:])
    finally:
        results["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        results["summary"] = {"pass": sum(1 for c in results["checks"] if c["pass"]), "fail": sum(1 for c in results["checks"] if not c["pass"]), "total": len(results["checks"])}
        json.dump(results, open(os.path.join(out, "real-results.json"), "w"), ensure_ascii=False, indent=1)
        print(json.dumps(results["summary"]))
        chrome.terminate()
        try:
            srv.stdin.close()
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        results_rc = 0 if results["summary"]["fail"] == 0 else 1
    sys.exit(results_rc)


if __name__ == "__main__":
    main()
