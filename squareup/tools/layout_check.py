"""Layout regression check for a clip (YouTube) question on phones and TVs.

Usage: python3 tools/layout_check.py [dist_dir] [out_dir] [label]
Needs Google Chrome and `pip install websocket-client`. Run python3 build.py first; dist_dir defaults to dist/.

Serves dist_dir on 127.0.0.1 and drives a headless Chrome with a throwaway profile over the DevTools protocol.
iPhone cases use mobile emulation (DPR 3, the iOS app's user agent) with emulated safe-area insets; this is a
desktop simulation, not a test on an iPhone, in WKWebView, over AirPlay or of YouTube playback.
Fails (exit 1) unless, in every case: the page uses device width, the clip player is 16:9 (+/- 0.01) and inside
its panel, nothing scrolls sideways, the turn chip, timer, video, question, category tag, points, Back and Show answer
are inside the safe viewport without scrolling, and the category tag does not cover the player. After Show answer the
player must still be 16:9 inside its panel with nothing scrolling sideways, and the board and home screens must not
scroll sideways either. Each step saves a screenshot.
"""
import base64, functools, http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.request
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# iPhone WKWebView-style UA: no "Safari/" token, so the game takes its IOS_APP path like the real wrapper.
IOS_APP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_6 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Mobile/15E148")

# iPhone 16 Pro / 17 Pro class (393x852 pt). The SwiftUI wrapper keeps the top (portrait) and side (landscape)
# safe areas outside the web view and lets only the bottom run under the home indicator, so "app" scenarios
# use the web view's real frame; "full" scenarios are the raw screen with every inset inside the page.
SCENARIOS = [
    dict(name="portrait-393x852-full", w=393, h=852, dpr=3, mobile=True, ua=IOS_APP_UA, sa=dict(top=59, bottom=34, left=0, right=0)),
    dict(name="portrait-393x793-app", w=393, h=793, dpr=3, mobile=True, ua=IOS_APP_UA, sa=dict(top=0, bottom=34, left=0, right=0)),
    dict(name="landscape-852x393-full", w=852, h=393, dpr=3, mobile=True, ua=IOS_APP_UA, sa=dict(top=0, bottom=21, left=59, right=59)),
    dict(name="landscape-734x393-app", w=734, h=393, dpr=3, mobile=True, ua=IOS_APP_UA, sa=dict(top=0, bottom=21, left=0, right=0)),
    # Pro Max class in Safari landscape: wide enough (932) to also match the TV breakpoint.
    dict(name="landscape-932x430-full", w=932, h=430, dpr=3, mobile=True, ua=IOS_APP_UA, sa=dict(top=0, bottom=21, left=62, right=62)),
    dict(name="wide-1920x1080", w=1920, h=1080, dpr=1, mobile=False, ua=None, sa=None),
]

SETUP_JS = r"""
(() => {
  const id = "movies";
  const c = CAT[id];
  // Longest clip question in the category, to stress wrapping.
  const yt = c.questions.map((q, i) => ({q, i})).filter(o => o.q.yt).sort((a, b) => b.q.q.length - a.q.q.length)[0];
  const qa = Object.keys(ABM).filter(k => ABM[k].when === "q");
  game = {teams: [0, 1, 2].map(i => ({name: "Team " + (i + 1), av: i, score: 0, abil: qa.slice(i * 2, i * 2 + 3), used: {}, sabo: false})),
          cats: [{id, tiles: [yt].concat(c.questions.map((q, i) => ({q, i})).filter(o => o.i !== yt.i).slice(0, 5)).map(o => ({p: o.q.p, qi: o.i, by: null, done: false}))}], turn: 0, timer: 60, over: false, hist: []};
  go("board"); openQ(0, 0); clearInterval(tick);
  return {qi: yt.i, q: yt.q.q, yt: yt.q.yt, abilities: qa.length};
})()
"""

MEASURE_JS = r"""
(() => {
  const vw = innerWidth, vh = innerHeight;
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;visibility:hidden;padding:env(safe-area-inset-top,0px) env(safe-area-inset-right,0px) env(safe-area-inset-bottom,0px) env(safe-area-inset-left,0px)";
  document.body.appendChild(probe);
  const cs = getComputedStyle(probe);
  const sa = {top: parseFloat(cs.paddingTop), right: parseFloat(cs.paddingRight), bottom: parseFloat(cs.paddingBottom), left: parseFloat(cs.paddingLeft)};
  probe.remove();
  const r = sel => { const e = document.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect();
    return {x: +b.left.toFixed(1), y: +b.top.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1), right: +b.right.toFixed(1), bottom: +b.bottom.toFixed(1)}; };
  const inView = b => !!b && b.y >= sa.top - 0.5 && b.bottom <= vh - sa.bottom + 0.5 && b.x >= sa.left - 0.5 && b.right <= vw - sa.right + 0.5;
  const qv = document.querySelector("#qv");
  const iframe = r("#ytf"), media = r("#qmedia"), text = r(".qcard .q-text"), back = r(".q-acts .btn-soft"), show = r(".q-acts .btn-pri"), turn = r("#qhead .turn"), ring = r("#qhead .ring"),
        tag = r("#qmedia .qc-tag"), pts = r("#qmedia .qc-pts");
  const overlap = (a, b) => !!a && !!b && Math.min(a.right, b.right) - Math.max(a.x, b.x) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > 0.5;
  const vp = document.querySelector('meta[name=viewport]');
  return {
    viewport: {innerWidth: vw, innerHeight: vh, clientWidth: document.documentElement.clientWidth, dpr: devicePixelRatio,
               visualScale: visualViewport ? +visualViewport.scale.toFixed(3) : null, metaViewport: vp ? vp.content : null},
    safeArea: sa,
    qv: qv ? {scrollWidth: qv.scrollWidth, clientWidth: qv.clientWidth, scrollHeight: qv.scrollHeight, clientHeight: qv.clientHeight} : null,
    docScrollWidth: document.documentElement.scrollWidth,
    horizontalOverflow: qv ? qv.scrollWidth > qv.clientWidth + 1 || document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 : null,
    iframe, iframeRatio: iframe && iframe.h ? +(iframe.w / iframe.h).toFixed(4) : null,
    iframeInsideMedia: !!(iframe && media && iframe.x >= media.x - 0.5 && iframe.right <= media.right + 0.5 && iframe.y >= media.y - 0.5 && iframe.bottom <= media.bottom + 0.5),
    media, text, back, show, turn, ring, tag, pts, tagCoversPlayer: overlap(tag, iframe),
    visibleWithoutScroll: {iframe: inView(iframe), questionText: inView(text), categoryTag: inView(tag), points: inView(pts), back: inView(back), showAnswer: inView(show), turn: inView(turn), timer: inView(ring)},
    layout: qv ? getComputedStyle(document.querySelector(".q-in")).display : null,
    view: document.body.dataset.view
  };
})()
"""


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr):
        res = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in res:
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:800])
        return res["result"].get("value")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def shoot(cdp, out, name):
    png = os.path.join(out, name + ".png")
    open(png, "wb").write(base64.b64decode(cdp.call("Page.captureScreenshot", format="png")["data"]))
    return png


def check(m, sc):
    """Failures for one measured case."""
    bad = []
    if m["viewport"]["clientWidth"] != sc["w"]:
        bad.append(f"laid out at {m['viewport']['clientWidth']} CSS px, not the {sc['w']} px device width")
    if m["iframeRatio"] is None or abs(m["iframeRatio"] - 16 / 9) > 0.01:
        bad.append(f"clip player ratio {m['iframeRatio']} is not 16:9")
    if not m["iframeInsideMedia"]:
        bad.append("clip player spills out of its panel")
    if m["horizontalOverflow"]:
        bad.append("horizontal overflow")
    for k, v in m["visibleWithoutScroll"].items():
        if not v:
            bad.append(f"{k} not fully inside the safe viewport")
    if m["tagCoversPlayer"]:
        bad.append("category tag covers the clip player")
    return bad


def check_revealed(m, award):
    bad = []
    if not award:
        bad.append("after Show answer, no scoring buttons found")
    for a in award:
        if not a["reachable"]:
            bad.append(f"after Show answer, scoring button {a['label']!r} cannot be scrolled into the safe viewport")
    if m["iframeRatio"] is None or abs(m["iframeRatio"] - 16 / 9) > 0.01:
        bad.append(f"after Show answer, clip player ratio {m['iframeRatio']} is not 16:9")
    if not m["iframeInsideMedia"]:
        bad.append("after Show answer, clip player spills out of its panel")
    if m["horizontalOverflow"]:
        bad.append("after Show answer, horizontal overflow")
    return bad


# After Show answer the host must be able to reach every "Who got it right?" button, scrolling if needed.
AWARD_JS = r"""
(() => {
  const vw = innerWidth, vh = innerHeight;
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;visibility:hidden;padding:env(safe-area-inset-top,0px) env(safe-area-inset-right,0px) env(safe-area-inset-bottom,0px) env(safe-area-inset-left,0px)";
  document.body.appendChild(probe);
  const cs = getComputedStyle(probe);
  const sa = {top: parseFloat(cs.paddingTop), right: parseFloat(cs.paddingRight), bottom: parseFloat(cs.paddingBottom), left: parseFloat(cs.paddingLeft)};
  probe.remove();
  return [...document.querySelectorAll("#qfoot .award button")].map(b => {
    b.scrollIntoView({block: "nearest", inline: "nearest"});
    const r = b.getBoundingClientRect();
    return {label: b.firstChild.textContent.trim(), reachable: r.top >= sa.top - 0.5 && r.bottom <= vh - sa.bottom + 0.5 && r.left >= sa.left - 0.5 && r.right <= vw - sa.right + 0.5};
  });
})()
"""

SIDEWAYS_JS = "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dist = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(here, "dist")
    out = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(tempfile.gettempdir(), "majlis-layout-check")
    label = sys.argv[3] if len(sys.argv) > 3 else "layout"
    os.makedirs(out, exist_ok=True)
    port = free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=dist)
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="majlis-m01-chrome-")
    dport = free_port()
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={dport}", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                               "--window-size=1920,1080", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = {"label": label, "dist": dist, "url": f"http://127.0.0.1:{port}/index.html", "chrome_pid": chrome.pid,
               "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scenarios": []}
    try:
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json"))
                page = next(t for t in tabs if t["type"] == "page"); break
            except Exception:
                time.sleep(0.2)
        cdp = CDP(page["webSocketDebuggerUrl"])
        results["chrome_version"] = cdp.call("Browser.getVersion").get("product")
        cdp.call("Page.enable"); cdp.call("Runtime.enable")
        for sc in SCENARIOS:
            cdp.call("Emulation.setDeviceMetricsOverride", width=sc["w"], height=sc["h"], deviceScaleFactor=sc["dpr"], mobile=sc["mobile"],
                     screenOrientation={"type": "portraitPrimary" if sc["h"] > sc["w"] else "landscapePrimary", "angle": 0 if sc["h"] > sc["w"] else 90})
            cdp.call("Emulation.setTouchEmulationEnabled", enabled=sc["mobile"], maxTouchPoints=5 if sc["mobile"] else 1)
            cdp.call("Emulation.setUserAgentOverride", userAgent=sc["ua"] or cdp.eval("navigator.userAgent").replace("HeadlessChrome", "Chrome"),
                     platform="iPhone" if sc["ua"] else "MacIntel")
            sa_note = "none"
            if sc["sa"]:
                try:
                    cdp.call("Emulation.setSafeAreaInsetsOverride", insets=sc["sa"]); sa_note = "Emulation.setSafeAreaInsetsOverride"
                except RuntimeError as e:
                    sa_note = f"unsupported: {e}"
            else:
                try: cdp.call("Emulation.setSafeAreaInsetsOverride", insets={})
                except RuntimeError: pass
            cdp.eval("try{localStorage.clear()}catch(e){}")
            cdp.call("Page.navigate", url=results["url"] + f"?probe={sc['name']}")
            for _ in range(150):
                time.sleep(0.2)
                if cdp.eval("document.readyState==='complete' && typeof openQ==='function' && typeof CAT==='object'"):
                    break
            setup = cdp.eval(SETUP_JS)
            time.sleep(2.5)  # fonts, card-in animation (.45s), text rise (.9s)
            m = cdp.eval(MEASURE_JS)
            png = shoot(cdp, out, f"{label}-{sc['name']}")
            bad = check(m, sc)
            cdp.eval("reveal(); clearInterval(tick)"); time.sleep(1.2)
            mr = cdp.eval(MEASURE_JS)
            shoot(cdp, out, f"{label}-{sc['name']}-revealed")
            award = cdp.eval(AWARD_JS)
            mr["awardButtons"] = award
            bad += check_revealed(mr, award)
            screens = {}
            for view, js in (("board", "closeQ(false)"), ("home", "go('home')")):
                cdp.eval(js); time.sleep(1.2)
                screens[view] = {"sideways": cdp.eval(SIDEWAYS_JS), "screenshot": shoot(cdp, out, f"{label}-{sc['name']}-{view}")}
                if screens[view]["sideways"]:
                    bad.append(f"{view} screen scrolls sideways")
            results["scenarios"].append({"scenario": sc, "safe_area_route": sa_note, "question": setup, "measure": m, "screenshot": png,
                                         "revealed": mr, "screens": screens, "failures": bad})
            print(("FAIL " if bad else "ok   ") + sc["name"], f"ratio={m['iframeRatio']} width={m['viewport']['clientWidth']} safe={m['safeArea']}", "; ".join(bad))
    finally:
        chrome.terminate(); chrome.wait(10); srv.shutdown(); shutil.rmtree(profile, ignore_errors=True)
    results["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(results, open(os.path.join(out, f"{label}-measurements.json"), "w"), indent=1)
    failed = sum(1 for s in results["scenarios"] if s["failures"])
    print(f"{len(results['scenarios']) - failed}/{len(results['scenarios'])} layout cases passed; screenshots and measurements in {out}")
    sys.exit(1 if failed or len(results["scenarios"]) != len(SCENARIOS) else 0)


if __name__ == "__main__":
    main()
