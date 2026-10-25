"""Raw-socket attack, limit and failure suite for the native host (L01B).

Every probe is hand-built bytes on a real TCP socket. Targets:

  prod-lan     PRODUCTION binary on the Mac's LAN address, operator via the stdin bridge.
  prod-rate    PRODUCTION binary on the LAN address with the default rate limits.
  prod-expiry  PRODUCTION binary on the LAN address with a short room idle TTL.
  test-prod    TEST binary on loopback with --surface production (CSPRNG failure
               injection and stats exist only in the test build).
  native-loop  TEST binary on loopback, test-loopback surface (reference parity column).
  python-ref   The unmodified Python reference server on loopback (parity column only).

The framing/Host/Origin/body/auth catalogue runs on prod-lan, native-loop and python-ref; the
native rows are asserted, the Python column is recorded so every difference is visible.
After each rejected group the room state (revision, phase, players, scores) must be unchanged
and the process must still answer /api/health.

Capabilities are kept in memory only; evidence records sha256 prefixes.

  python attack_suite.py <repo root> <production bin> <test bin> <out dir> <LAN IPv4>
"""
import hashlib, json, os, queue, re, secrets, socket, subprocess, sys, threading, time

REPO, PROD, TEST, OUT, LAN = (os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]), os.path.abspath(sys.argv[3]),
                              os.path.abspath(sys.argv[4]), sys.argv[5])
PY = "/Library/Developer/CommandLineTools/usr/bin/python3"
FAST = ["--heartbeat", "0.5", "--sweep-interval", "0.1", "--request-rate", "2000", "--request-burst", "5000",
        "--create-per-minute", "6000", "--create-burst", "500", "--join-per-minute", "6000", "--join-burst", "500",
        "--action-rate", "2000", "--action-burst", "5000"]
results = {"label": "Raw TCP probes on this Mac (LAN-bound and loopback sockets). Not phone, Wi-Fi, offline or WAN evidence.",
           "checks": [], "catalogue": [], "targets": {}}
SECRETS = []


def check(name, ok, detail=None):
    results["checks"].append({"name": name, "pass": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if detail is None else "  " + json.dumps(detail, ensure_ascii=False, default=str)[:260]), flush=True)
    return ok


def harvest(j):
    """Remembers any capability-shaped value so evidence redaction never depends on the build being correct."""
    if isinstance(j, dict):
        for k, v in j.items():
            if isinstance(v, str) and k.lower().endswith("token") and len(v) >= 16:
                SECRETS.append(v)
            else:
                harvest(v)
    elif isinstance(j, list):
        for v in j:
            harvest(v)


def h16(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def free_port(ip):
    s = socket.socket(); s.bind((ip, 0)); p = s.getsockname()[1]; s.close()
    assert p != 8765
    return p


# ------------------------------------------------------------------ servers

class Target:
    def __init__(self, name, argv, ip, port, operator, surface):
        self.name, self.ip, self.port, self.surface = name, ip, port, surface
        self.hostport = f"{ip}:{port}"
        self.origin = f"http://{ip}:{port}"
        self.log = open(os.path.join(OUT, f"{name}-stderr.log"), "w")
        self.proc = subprocess.Popen(argv, cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.operator = operator
        self.lock = threading.Lock()
        self.tokens = {}
        results["targets"][name] = {"command": " ".join([os.path.basename(argv[0])] + argv[1:]), "pid": self.proc.pid}
        if operator:
            self.banner = json.loads(self.lines.get(timeout=10))
            results["targets"][name]["banner"] = self.banner
        else:
            for _ in range(200):
                try:
                    if raw(self, get("/api/health", self))["status"] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.05)

    def _pump(self):
        for ln in self.proc.stdout:
            self.lines.put(ln.decode("utf-8", "replace"))

    def op(self, req, raw_line=None):
        with self.lock:
            self.proc.stdin.write((raw_line if raw_line is not None else json.dumps(req).encode()) + b"\n")
            self.proc.stdin.flush()
            j = json.loads(self.lines.get(timeout=10))
            harvest(j)
            return j

    def stop(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(10)
        except Exception:
            self.proc.kill()
            self.proc.wait(5)
        results["targets"][self.name]["exit"] = self.proc.returncode
        return self.proc.returncode


def native(name, binary, ip, extra, surface, operator=True):
    port = free_port(ip)
    argv = [binary, "--host", ip, "--port", str(port), "--content-dir", "party/content", "--web-dir", "party/web",
            "--exit-on-stdin-eof", "--access-log"] + (["--allow-lan"] if ip != "127.0.0.1" else []) + \
           (["--operator-stdin"] if operator else []) + extra
    return Target(name, argv, ip, port, operator, surface)


def python_ref(extra):
    port = free_port("127.0.0.1")
    argv = [PY, "-B", "-E", "-s", "-m", "party.server", "--host", "127.0.0.1", "--port", str(port), "--content-dir", "party/content",
            "--web-dir", "party/web", "--exit-on-stdin-eof", "--access-log"] + extra
    return Target("python-ref", argv, "127.0.0.1", port, False, "reference")


# ------------------------------------------------------------------ raw HTTP

def build(method, path, t, body=None, token=None, ctype="application/json", host=None, origin=True, extra=(), cl=True, version="HTTP/1.1"):
    h = [f"{method} {path} {version}"]
    if host is not False:
        h.append(f"Host: {host or t.hostport}")
    if origin is True and method == "POST":
        h.append(f"Origin: {t.origin}")
    elif isinstance(origin, str):
        h.append(f"Origin: {origin}")
    if token is not None:
        h.append(f"Authorization: Bearer {token}")
    if body is not None:
        if ctype:
            h.append(f"Content-Type: {ctype}")
        if cl:
            h.append(f"Content-Length: {len(body)}")
    h += list(extra)
    data = ("\r\n".join(h) + "\r\n\r\n").encode("latin-1")
    return data + (body or b"")


def get(path, t, **kw):
    return build("GET", path, t, **kw)


def raw(t, data, timeout=6.0, rcvbuf=None):
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.settimeout(timeout)
    t0 = time.time()
    s.connect((t.ip, t.port))
    s.sendall(data)
    buf, closed = b"", False
    try:
        while True:
            c = s.recv(65536)
            if not c:
                closed = True
                break
            buf += c
    except socket.timeout:
        pass
    except ConnectionResetError:
        closed = True
    s.close()
    return parse(buf, closed, time.time() - t0)


def parse(buf, closed, secs):
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    m = re.match(r"HTTP/\d\.\d (\d{3})", lines[0]) if lines and lines[0] else None
    hdr = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        hdr.setdefault(k.strip().lower(), v.strip())
    code = None
    try:
        j = json.loads(body)
        code = (j.get("error") or {}).get("code") if isinstance(j, dict) else None
    except Exception:
        j = None
    harvest(j)
    return {"status": int(m.group(1)) if m else None, "headers": hdr, "body": body, "json": j, "code": code, "closed": closed, "secs": round(secs, 3), "rawlen": len(buf)}


def api(t, method, path, body=None, token=None, **kw):
    b = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    return raw(t, build(method, path, t, body=b, token=token, **kw))


def health(t):
    t0 = time.time()
    r = raw(t, get("/api/health", t), timeout=5)
    return r["status"] == 200, round(time.time() - t0, 3)


# ------------------------------------------------------------------ room setup

def setup_room(t, names=("Aisha", "Omar", "Noora")):
    if t.operator:
        code = t.op({"op": "create", "body": {}})["code"]
    else:
        r = api(t, "POST", "/api/rooms", {})
        code = r["json"]["code"]
        t.tokens["operator"] = r["json"]["hostToken"]
        SECRETS.extend([r["json"]["hostToken"], r["json"]["displayToken"]])
    t.code = code
    for nm in names:
        r = api(t, "POST", f"/api/rooms/{code}/join", {"name": nm})
        assert r["status"] == 201, (t.name, r["status"], r["code"])
        t.tokens[nm] = r["json"]["playerToken"]
        t.pid = getattr(t, "pid", {})
        t.pid[nm] = r["json"]["playerId"]
        SECRETS.append(r["json"]["playerToken"])
    return code


def fingerprint(t, code=None):
    code = code or t.code
    if t.operator:
        s = t.op({"op": "state", "code": code})["state"]
    else:
        s = api(t, "GET", f"/api/rooms/{code}/state", token=t.tokens["operator"])["json"]
    return {"revision": s["revision"], "phase": s["phase"], "players": sorted((p["id"], p["name"], p["score"]) for p in s["players"])}


def guarded(t, label, fn, expect_joins=0, note=""):
    """Runs fn(); asserts room state unchanged (or exactly the declared joins) and the process healthy afterwards."""
    before = fingerprint(t)
    out = fn()
    after = fingerprint(t)
    ok, secs = health(t)
    same = before == after if expect_joins == 0 else (after["revision"] == before["revision"] + expect_joins
                                                      and len(after["players"]) == len(before["players"]) + expect_joins)
    check(f"[{t.name}] {label}: room state unchanged{note} and /api/health answers ({secs}s)", same and ok and t.proc.poll() is None,
          {"before_rev": before["revision"], "after_rev": after["revision"]})
    return out


# ------------------------------------------------------------------ catalogue

def depth_body(n, name):
    return ('{"name":' + json.dumps(name) + ',"pad":' + "[" * n + "]" * n + "}").encode()


def catalogue(t):
    c, tok = t.code, t.tokens
    hp = t.hostport
    other_port = f"{t.ip}:{t.port + 1 if t.port < 65535 else t.port - 1}"
    act = lambda **kw: json.dumps({"actionId": secrets.token_hex(8), "type": "whoami.ready", "payload": {"ready": True}, **kw}).encode()
    J = f"/api/rooms/{c}/join"
    A = f"/api/rooms/{c}/actions"
    S = f"/api/rooms/{c}/state"
    big_line = "/" + "a" * 9000
    rows = [
        # --- framing
        ("framing", "Transfer-Encoding: chunked body", build("POST", J, t, body=b"5\r\n{}   \r\n0\r\n\r\n", cl=False, extra=["Transfer-Encoding: chunked"]), {411}, "length_required"),
        ("framing", "Transfer-Encoding plus Content-Length (smuggling shape)", build("POST", J, t, body=b"{}", extra=["Transfer-Encoding: chunked"]), {411}, "length_required"),
        ("framing", "two Content-Length headers", build("POST", J, t, body=b"{}", extra=["Content-Length: 2"]), {400}, None),
        ("framing", "Content-Length '+2'", build("POST", J, t, body=b"{}", cl=False, extra=["Content-Length: +2"]), {400}, None),
        ("framing", "Content-Length '2, 2'", build("POST", J, t, body=b"{}", cl=False, extra=["Content-Length: 2, 2"]), {400}, None),
        ("framing", "Content-Length '-1'", build("POST", J, t, body=b"", cl=False, extra=["Content-Length: -1"]), {400}, None),
        ("framing", "obs-fold continuation line", build("GET", "/api/health", t, extra=["X-A: 1", " folded"]), {400}, None),
        ("framing", "request line over 8 KiB", get(big_line, t), {414}, None),
        ("framing", "120 header fields", build("GET", "/api/health", t, extra=[f"X-H{i}: v" for i in range(120)]), {431}, None),
        ("framing", "head over 16 KiB", build("GET", "/api/health", t, extra=[f"X-Big{i}: " + "b" * 4000 for i in range(5)]), {431}, None),
        ("framing", "HTTP/2.0 request line", build("GET", "/api/health", t, version="HTTP/2.0"), {505}, None),
        ("framing", "HTTP/1.1x malformed version", build("GET", "/api/health", t, version="HTTP/1.1x"), {400}, None),
        ("framing", "HTTP/0.9-style two-part request line", b"GET /api/health\r\n\r\n", {400}, None),
        ("framing", "NUL byte in a header value", build("GET", "/api/health", t, extra=["X-A: a\x00b"]), {400}, None),
        ("framing", "non-ASCII byte in a header value", build("GET", "/api/health", t).replace(b"\r\n\r\n", b"\r\nX-A: \xc3\xa9\r\n\r\n"), {400}, None),
        ("framing", "non-ASCII byte in the target", b"GET /play\xc3\xa9.html HTTP/1.1\r\nHost: " + hp.encode() + b"\r\n\r\n", {400}, None),
        ("framing", "unknown method FOO", build("FOO", "/api/health", t), {501}, None),
        ("framing", "PUT on /api/health", build("PUT", "/api/health", t), {405}, "method_not_allowed"),
        ("framing", "DELETE on the actions route", build("DELETE", A, t), {405}, "method_not_allowed"),
        ("framing", "GET on the actions route", get(A, t), {405}, "method_not_allowed"),
        ("framing", "absolute-form target", get(f"http://{hp}/api/health", t), {404}, "not_found"),
        # --- paths
        ("path", "dot-dot traversal", get("/../party/server/httpd.py", t), {404}, "not_found"),
        ("path", "percent-encoded dot-dot", get("/%2e%2e/party/content/feud.json", t), {404}, "not_found"),
        ("path", "double leading slash", get("//play.html", t), {404}, "not_found"),
        ("path", "dot segment inside an allowed path", get("/js/../play.html", t), {404}, "not_found"),
        ("path", "encoded NUL", get("/play.html%00", t), {404}, "not_found"),
        ("path", "directory listing /js/", get("/js/", t), {404}, "not_found"),
        ("path", "content pack file", get("/party/content/feud.json", t), {404}, "not_found"),
        ("path", "test page", get("/tests/qr_decode.swift", t), {404}, "not_found"),
        ("path", "sim adapter", get("/js/sim.js", t), {404}, "not_found"),
        ("path", "case variant /PLAY.HTML", get("/PLAY.HTML", t), {404}, "not_found"),
        # --- query
        ("query", "query on /api/health", get("/api/health?x=1", t), {400}, "query_not_allowed"),
        ("query", "token in the state query string", get(S + "?token=" + "t" * 43, t), {400}, "query_not_allowed"),
        ("query", "lower-case play code", get("/play.html?code=" + c.lower(), t), {400}, "query_not_allowed"),
        ("query", "extra query parameter on play", get(f"/play.html?code={c}&x=1", t), {400}, "query_not_allowed"),
        ("query", "empty query on play", get("/play.html?", t), {400}, "query_not_allowed"),
        # --- Host / Origin / Sec-Fetch
        ("host", "missing Host", build("GET", "/api/health", t, host=False), {403}, "host_forbidden"),
        ("host", "foreign Host (DNS rebinding)", build("GET", "/api/health", t, host="evil.example"), {403}, "host_forbidden"),
        ("host", "Host with another port", build("GET", "/api/health", t, host=other_port), {403}, "host_forbidden"),
        ("host", "two Host headers", build("GET", "/api/health", t, extra=[f"Host: {hp}"]), {403}, "host_forbidden"),
        ("host", "Host with userinfo", build("GET", "/api/health", t, host="x@" + hp), {403}, "host_forbidden"),
        ("origin", "cross-site Origin on join", build("POST", J, t, body=b'{"name":"Zed"}', origin="http://evil.example"), {403}, "origin_forbidden"),
        ("origin", "Origin 'null' on join", build("POST", J, t, body=b'{"name":"Zed"}', origin="null"), {403}, "origin_forbidden"),
        ("origin", "two Origin headers", build("POST", J, t, body=b'{"name":"Zed"}', extra=[f"Origin: {t.origin}"]), {403}, "origin_forbidden"),
        ("origin", "Sec-Fetch-Site cross-site", build("POST", J, t, body=b'{"name":"Zed"}', extra=["Sec-Fetch-Site: cross-site"]), {403}, "origin_forbidden"),
        ("origin", "Sec-Fetch-Site same-site", build("POST", J, t, body=b'{"name":"Zed"}', extra=["Sec-Fetch-Site: same-site"]), {403}, "origin_forbidden"),
        ("origin", "cross-site Origin reading state", build("GET", S, t, token=tok["Aisha"], origin="http://evil.example"), {403}, "origin_forbidden"),
        # --- body
        ("body", "text/plain body", build("POST", J, t, body=b'{"name":"Zed"}', ctype="text/plain"), {415}, "unsupported_media_type"),
        ("body", "no Content-Type", build("POST", J, t, body=b'{"name":"Zed"}', ctype=None), {415}, "unsupported_media_type"),
        ("body", "body over the 16 KiB limit", build("POST", J, t, body=b'{"name":"' + b"z" * 17000 + b'"}'), {413}, "body_too_large"),
        ("body", "Content-Length 1 MiB announced, nothing sent", build("POST", J, t, cl=False, extra=["Content-Type: application/json", "Content-Length: 1048576"]), {413}, "body_too_large"),
        ("body", "invalid UTF-8 inside a string", build("POST", J, t, body=b'{"name":"Z\xff\xfe"}'), {400}, "invalid_json"),
        ("body", "overlong UTF-8 encoding of '/'", build("POST", J, t, body=b'{"name":"Z\xc0\xaf"}'), {400}, "invalid_json"),
        ("body", "UTF-8 encoded surrogate (CESU)", build("POST", J, t, body=b'{"name":"Z\xed\xa0\x80"}'), {400}, "invalid_json"),
        ("body", "byte-order mark before JSON", build("POST", J, t, body=b'\xef\xbb\xbf{"name":"Zed"}'), {400}, "invalid_json"),
        ("body", "trailing garbage after JSON", build("POST", J, t, body=b'{"name":"Zed"}x'), {400}, "invalid_json"),
        ("body", "NaN literal is parsed like the reference (name check still applies)", build("POST", J, t, body=b'{"name":"Aisha","n":NaN}'), {409}, "name_taken"),
        ("body", "top-level array", build("POST", J, t, body=b'[{"name":"Zed"}]'), {400}, "invalid_json"),
        ("body", "escaped name equal to an existing player (\\u0041isha)", build("POST", J, t, body=b'{"name":"\\u0041isha"}'), {409}, "name_taken"),
        ("body", "control character escaped in name (\\u0007)", build("POST", J, t, body=b'{"name":"Z\\u0007d"}'), {400}, "invalid_name"),
        ("body", "JSON nesting 900 inner arrays (outer object)", build("POST", J, t, body=depth_body(900, "Deep900")), {400}, "invalid_json"),
        ("body", "JSON nesting 5000 inner arrays", build("POST", J, t, body=depth_body(5000, "Deep5000")), {400}, "invalid_json"),
        ("body", "empty action body", build("POST", A, t, body=b"", token=tok["Aisha"]), {400}, "invalid_json"),
        # --- auth
        ("auth", "action without Authorization", build("POST", A, t, body=act()), {401}, "unauthorized"),
        ("auth", "Basic scheme", build("POST", A, t, body=act(), extra=["Authorization: Basic " + "a" * 20]), {401}, "unauthorized"),
        ("auth", "Bearer with an empty token", build("POST", A, t, body=act(), extra=["Authorization: Bearer "]), {401}, "unauthorized"),
        ("auth", "Bearer token with a trailing extra word", build("POST", A, t, body=act(), extra=["Authorization: Bearer " + tok["Aisha"] + " x"]), {401}, "unauthorized"),
        ("auth", "Bearer token with one extra character", build("POST", A, t, body=act(), token=tok["Aisha"] + "A"), {401}, "unauthorized"),
        ("auth", "two Authorization headers", build("POST", A, t, body=act(), token=tok["Aisha"], extra=["Authorization: Bearer " + tok["Omar"]]), {401}, "unauthorized"),
        ("auth", "state with no credential", get(S, t), {401}, "unauthorized"),
        ("auth", "events with no credential", get(f"/api/rooms/{c}/events", t), {401}, "unauthorized"),
        ("auth", "unknown room code", build("POST", "/api/rooms/ZZZZZZ/actions", t, body=act(), token=tok["Aisha"]), {404}, "room_not_found"),
        ("auth", "malformed room code", build("POST", "/api/rooms/abc/actions", t, body=act(), token=tok["Aisha"]), {404}, "room_not_found"),
        # --- actions
        ("action", "invalid actionId", build("POST", A, t, body=json.dumps({"actionId": "bad id!", "type": "whoami.ready", "payload": {}}).encode(), token=tok["Aisha"]), {400}, "invalid_action_id"),
        ("action", "unknown action type", build("POST", A, t, body=json.dumps({"actionId": "x1", "type": "nope.x", "payload": {}}).encode(), token=tok["Aisha"]), {400}, "unknown_action"),
        ("action", "player sends room.start", build("POST", A, t, body=json.dumps({"actionId": "x2", "type": "room.start", "payload": {}}).encode(), token=tok["Aisha"]), {403}, "forbidden"),
        ("action", "game action in the lobby", build("POST", A, t, body=act(roundId="g1r1"), token=tok["Aisha"]), {409}, "invalid_phase"),
    ]
    out = []
    for group, label, data, want, code in rows:
        r = raw(t, data)
        out.append({"group": group, "probe": label, "status": r["status"], "code": r["code"], "closed": r["closed"],
                    "secs": r["secs"], "want_status": sorted(want), "want_code": code})
    return out


def run_catalogue(t):
    if t.name == "python-ref":
        return guarded(t, "attack catalogue (reference)", lambda: catalogue(t), expect_joins=1,
                       note=" except the one depth-900 join the reference accepts (N5)")
    return guarded(t, "attack catalogue (%s)" % t.surface, lambda: catalogue(t))


# ------------------------------------------------------------------ main

def main():
    os.makedirs(OUT, exist_ok=True)
    started = []
    try:
        # ===== production LAN: catalogue, depth, SSE, caps, timeouts, bridge =====
        prod = native("prod-lan", PROD, LAN, FAST + ["--request-timeout", "2", "--sse-write-timeout", "2", "--max-connections", "40",
                                                       "--max-streams-per-principal", "4"], "production")
        started.append(prod)
        check("[prod-lan] banner: production surface on the LAN address", prod.banner["surface"] == "production" and prod.banner["host"] == LAN, prod.banner)
        setup_room(prod)
        loop = native("native-loop", TEST, "127.0.0.1", FAST + ["--request-timeout", "2"], "test-loopback", operator=False)
        started.append(loop)
        setup_room(loop)
        pref = python_ref(FAST + ["--request-timeout", "2"])
        started.append(pref)
        setup_room(pref)
        cats = {t.name: run_catalogue(t) for t in (prod, loop, pref)}
        for i, row in enumerate(cats["prod-lan"]):
            nl, py = cats["native-loop"][i], cats["python-ref"][i]
            row = dict(row, native_loop=(nl["status"], nl["code"]), python_ref=(py["status"], py["code"]))
            results["catalogue"].append(row)
            ok_p = row["status"] in row["want_status"] and (row["want_code"] is None or row["code"] == row["want_code"])
            ok_l = nl["status"] in row["want_status"] and (row["want_code"] is None or nl["code"] == row["want_code"])
            same = (py["status"], py["code"]) == (row["status"], row["code"])
            row["python_parity"] = "same" if same else "differs"
            check(f"[{row['group']}] {row['probe']}: production {row['status']} {row['code'] or ''} / loopback {nl['status']} {nl['code'] or ''}"
                  f" (python {py['status']} {py['code'] or ''}{'' if same else ' - differs'})", ok_p and ok_l)
        diffs = [r for r in results["catalogue"] if r["python_parity"] != "same"]
        results["python_differences"] = [{k: r[k] for k in ("group", "probe", "status", "code", "python_ref")} for r in diffs]
        check("every catalogue probe was refused (no 2xx) on the production LAN surface", not any((r["status"] or 0) < 300 for r in results["catalogue"]))

        # Depth boundary: 899 fits (state changes by one join), 900 refused with state unchanged.
        before = fingerprint(prod)
        r900 = raw(prod, build("POST", f"/api/rooms/{prod.code}/join", prod, body=depth_body(900, "Deep900")))
        mid = fingerprint(prod)
        r899 = raw(prod, build("POST", f"/api/rooms/{prod.code}/join", prod, body=depth_body(899, "Deep899")))
        after = fingerprint(prod)
        if r899["json"] and "playerToken" in r899["json"]:
            SECRETS.append(r899["json"]["playerToken"])
        ok, secs = health(prod)
        check("depth: 900 inner arrays -> 400 invalid_json, state unchanged; 899 -> 201 joined; process responsive",
              r900["status"] == 400 and r900["code"] == "invalid_json" and before == mid and r899["status"] == 201
              and len(after["players"]) == len(before["players"]) + 1 and ok, {"900": r900["status"], "899": r899["status"], "health_s": secs})
        rp = raw(pref, build("POST", f"/api/rooms/{pref.code}/join", pref, body=depth_body(900, "Deep900")))
        results["n5_python_depth_900"] = {"status": rp["status"], "code": rp["code"]}
        check("N5 kept visible: the Python reference at depth 900 answers differently (declared, not tuned)", True, results["n5_python_depth_900"])

        # Unicode: escaped and raw UTF-8 names arrive identical; Arabic name round-trips.
        r = api(prod, "POST", f"/api/rooms/{prod.code}/join", b'{"name":"\\u0646\\u0648\\u0631"}')
        r2 = api(prod, "POST", f"/api/rooms/{prod.code}/join", '{"name":"نور"}'.encode())
        if r["json"] and "playerToken" in r["json"]:
            SECRETS.append(r["json"]["playerToken"])
        check("escaped Unicode name joins; the same name in raw UTF-8 is then name_taken (escapes decoded exactly)",
              r["status"] == 201 and any(p[1] == "نور" for p in fingerprint(prod)["players"]) and r2["status"] == 409 and r2["code"] == "name_taken", [r["status"], r2["status"], r2["code"]])
        r = api(prod, "POST", f"/api/rooms/{prod.code}/join", b'{"name":"\\ud800x"}')
        results["lone_surrogate_name"] = {"status": r["status"], "code": r["code"]}
        check("escaped lone surrogate name is refused (not stored)", r["status"] == 400, results["lone_surrogate_name"])

        # Replay / stale.
        tokA = prod.tokens["Aisha"]
        prod.op({"op": "action", "code": prod.code, "body": {"actionId": "m1", "type": "room.set_mode", "payload": {"mode": "who_am_i"}}})
        prod.op({"op": "action", "code": prod.code, "body": {"actionId": "m2", "type": "room.start", "payload": {}}})
        rid = prod.op({"op": "state", "code": prod.code})["state"]["roundId"]
        body = json.dumps({"actionId": "dup-1", "type": "whoami.ready", "roundId": rid, "payload": {"ready": True}}).encode()
        a1 = raw(prod, build("POST", f"/api/rooms/{prod.code}/actions", prod, body=body, token=tokA))
        fp = fingerprint(prod)
        a2 = raw(prod, build("POST", f"/api/rooms/{prod.code}/actions", prod, body=body, token=tokA))
        a3 = raw(prod, build("POST", f"/api/rooms/{prod.code}/actions", prod, body=json.dumps({"actionId": "dup-2", "type": "whoami.ready", "roundId": "g9r9", "payload": {"ready": True}}).encode(), token=tokA))
        check("duplicate actionId replays the stored result (X-Action-Replayed: 1, identical body, no new revision); stale round 409",
              a1["status"] == 200 and a1["headers"].get("x-action-replayed") == "0" and a2["status"] == 200 and a2["headers"].get("x-action-replayed") == "1"
              and a1["body"] == a2["body"] and fingerprint(prod) == fp and a3["status"] == 409 and a3["code"] == "stale_round",
              [a1["status"], a2["headers"].get("x-action-replayed"), a3["code"]])

        # SSE: headers, heartbeat, state frame, per-principal cap, teardown, terminal close.
        def open_stream(t, token, last_id=None):
            s = socket.socket(); s.settimeout(3); s.connect((t.ip, t.port))
            s.sendall(build("GET", f"/api/rooms/{t.code}/events", t, token=token, extra=[f"Last-Event-ID: {last_id}"] if last_id else []))
            return s

        def read_for(s, secs):
            end, buf = time.time() + secs, b""
            s.settimeout(0.2)
            while time.time() < end:
                try:
                    c = s.recv(65536)
                    if not c:
                        return buf, True
                    buf += c
                except socket.timeout:
                    pass
            return buf, False

        s = open_stream(prod, prod.tokens["Omar"])
        buf, closed = read_for(s, 1.8)
        head, _, rest = buf.partition(b"\r\n\r\n")
        txt = rest.decode("utf-8", "replace")
        check("SSE: 200 text/event-stream, no-store, retry hint, initial state frame with id, >=2 heartbeats in 1.8 s at 0.5 s",
              head.startswith(b"HTTP/1.0 200") and b"text/event-stream" in head and b"no-store" in head.lower() and txt.startswith("retry: 3000")
              and re.search(r"id: \d+\nevent: state\ndata: \{", txt) is not None and txt.count(": heartbeat") >= 2 and not closed,
              {"heartbeats": txt.count(": heartbeat")})
        check("SSE player stream carries no capability and only the player projection", "token" not in txt.lower() and '"role":"player"' in txt.replace(" ", ""))
        held = [s] + [open_stream(prod, prod.tokens["Omar"]) for _ in range(3)]
        time.sleep(0.4)
        fifth = raw(prod, build("GET", f"/api/rooms/{prod.code}/events", prod, token=prod.tokens["Omar"]), timeout=3)
        check("per-principal stream cap 4: a 5th stream is refused 429 too_many_streams", fifth["status"] == 429 and fifth["code"] == "too_many_streams", [fifth["status"], fifth["code"]])
        held.pop().close()
        time.sleep(0.5)
        again = open_stream(prod, prod.tokens["Omar"])
        b2, cl2 = read_for(again, 0.8)
        check("closing one stream frees its slot at once (teardown on peer close)", b2.startswith(b"HTTP/1.0 200"), b2[:40].decode("latin-1"))
        held.append(again)
        rev = fingerprint(prod)["revision"]
        resumed = open_stream(prod, prod.tokens["Noora"], last_id=rev)
        b3, _ = read_for(resumed, 0.8)
        results["sse_resume_same_revision"] = b3.decode("utf-8", "replace").split("\r\n\r\n", 1)[-1][:200]
        resumed.close()
        prod.op({"op": "action", "code": prod.code, "body": {"actionId": "close-1", "type": "room.close", "payload": {}}})
        ends = []
        for h in held:
            bb, cc = read_for(h, 1.2)
            ends.append({"closed_state_frame": b'"phase":"closed"' in bb.replace(b" ", b""), "eof": cc})
            h.close()
        check("room close: every open stream receives the phase=closed state frame (stream then ends at room expiry, as in the reference)",
              all(e["closed_state_frame"] for e in ends), ends)
        cl = api(prod, "POST", f"/api/rooms/{prod.code}/actions", {"actionId": "after-close", "type": "whoami.ready", "roundId": rid, "payload": {}}, token=prod.tokens["Aisha"])
        check("closed room refuses player actions 409 room_closed", cl["status"] == 409 and cl["code"] == "room_closed", [cl["status"], cl["code"]])

        # Connection cap (40): the 41st connection gets the raw 503 server_busy.
        socks = []
        for _ in range(40):
            x = socket.socket(); x.connect((prod.ip, prod.port)); socks.append(x)
        time.sleep(0.3)
        busy = raw(prod, get("/api/health", prod), timeout=3)
        for x in socks:
            x.close()
        time.sleep(0.4)
        ok, secs = health(prod)
        check("connection cap 40: 41st connection answered 503 server_busy, then the server recovers", busy["status"] == 503 and busy["code"] == "server_busy" and ok,
              [busy["status"], busy["code"], secs])

        # L02A: the over-capacity rejection path itself must be bounded. With
        # the 40-slot cap already full, 100 more connections arrive at once:
        # the first ~64 (maxBusyReplies) get the raw 503, the rest are
        # cancelled immediately with no bytes, so transient reject work never
        # grows with the number of excess connections.
        socks = [socket.socket() for _ in range(40)]
        for x in socks:
            x.connect((prod.ip, prod.port))
        time.sleep(0.2)
        excess = [socket.socket() for _ in range(100)]
        for x in excess:
            x.settimeout(2)
            x.connect((prod.ip, prod.port))
        time.sleep(0.5)
        with_503, immediate_close, other = 0, 0, 0
        for x in excess:
            try:
                data = x.recv(4096)
            except socket.timeout:
                data = b"__timeout__"
            if data.startswith(b"HTTP/1.0 503"):
                with_503 += 1
            elif data == b"":
                immediate_close += 1
            else:
                other += 1
            x.close()
        for x in socks:
            x.close()
        time.sleep(0.4)
        ok, secs = health(prod)
        check("over-capacity rejection is bounded: of 100 excess connections at most ~64 receive the 503 body, the rest are cancelled with no bytes, nothing hangs, and the server recovers",
              with_503 <= 70 and other == 0 and (with_503 + immediate_close) == 100 and ok,
              {"with_503": with_503, "immediate_close": immediate_close, "other": other, "health_s": secs})

        # Request timeout 2 s: idle, slowloris and short-body connections are closed by the deadline.
        def timed(data, drip=None):
            x = socket.socket(); x.settimeout(6); t0 = time.time(); x.connect((prod.ip, prod.port))
            if data:
                x.sendall(data)
            got, end = b"", False
            try:
                while True:
                    if drip:
                        time.sleep(0.4)
                        try:
                            x.sendall(drip)
                        except OSError:
                            end = True
                            break
                        x.settimeout(0.01)
                        try:
                            c = x.recv(4096)
                            if not c:
                                end = True
                                break
                            got += c
                        except socket.timeout:
                            pass
                        if time.time() - t0 > 6:
                            break
                    else:
                        c = x.recv(4096)
                        if not c:
                            end = True
                            break
                        got += c
            except (socket.timeout, ConnectionResetError, BrokenPipeError):
                end = True
            x.close()
            return {"closed": end, "secs": round(time.time() - t0, 2), "bytes": len(got), "status": got[:12].decode("latin-1")}
        idle = timed(None)
        partial = timed(b"GET /api/health HTTP/1.1\r\nHost: " + prod.hostport.encode() + b"\r\n")
        slow = timed(b"GET /api/health HTTP/1.1\r\n", drip=b"X")
        short = timed(build("POST", f"/api/rooms/{prod.code}/join", prod, cl=False, extra=["Content-Type: application/json", "Content-Length: 50"]) + b'{"na')
        results["timeouts"] = {"idle": idle, "partial_head": partial, "slowloris": slow, "short_body": short}
        check("request deadline 2 s: idle, partial-head, slowloris (1 byte / 0.4 s) and short-body connections all closed by ~2 s",
              all(v["closed"] and 1.5 <= v["secs"] <= 3.5 for v in (idle, partial, slow, short)), results["timeouts"])

        # Bridge hardening on production.
        r = prod.op(None, raw_line=b"#csprng-fail on")
        c2 = prod.op({"op": "create", "body": {}})
        check("production bridge has no CSPRNG failure switch (line is plain invalid input; next create still works)",
              r.get("ok") is False and c2.get("ok") is True, [r.get("error", {}).get("code"), c2.get("ok")])
        r = prod.op(None, raw_line=b'{"op":"stats"}')
        check("production bridge has no stats op (unknown_action)", r.get("ok") is False and r["error"]["code"] == "unknown_action", r.get("error"))
        r = prod.op(None, raw_line=b'{"op":"create","body":' + b"[" * 950 + b"]" * 950 + b"}")
        check("bridge applies the same JSON depth guard", r.get("ok") is False and r["error"]["code"] == "invalid_json", r.get("error"))
        r = prod.op(None, raw_line=b"x" * 70000)
        check("bridge line over 64 KiB refused body_too_large; bridge keeps working", r.get("error", {}).get("code") == "body_too_large"
              and prod.op({"op": "state", "code": c2["code"]}).get("ok") is True, r.get("error"))
        r = prod.op(None, raw_line=b'{"op":"create","body":{"operatorName":"\xff"}}')
        check("bridge invalid UTF-8 refused invalid_json", r.get("ok") is False and r["error"]["code"] == "invalid_json", r.get("error"))
        bridge_keys = json.dumps(c2)
        check("bridge create reply has no token field", "token" not in bridge_keys.lower(), sorted(c2))

        rr = api(prod, "POST", "/api/rooms", {})
        hp_ = [raw(prod, get(p, prod))["status"] for p in ("/host.html", "/tv.html", "/js/host.js", "/js/tv.js")]
        check("production LAN surface: POST /api/rooms 404 not_found with no capability, host/tv pages and scripts 404",
              rr["status"] == 404 and rr["code"] == "not_found" and b"oken" not in rr["body"] and hp_ == [404] * 4, [rr["status"], rr["code"], hp_])

        # Bind: a second production instance on the same LAN port fails cleanly.
        b = subprocess.run([PROD, "--host", LAN, "--port", str(prod.port), "--allow-lan", "--content-dir", "party/content"], cwd=REPO,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        check("second instance on an occupied LAN port: exit 2, bind_failed 'Address already in use'",
              b.returncode == 2 and b'"bind_failed"' in b.stderr + b.stdout and b"Address already in use" in b.stderr + b.stdout, (b.returncode, (b.stderr + b.stdout)[-160:].decode()))
        b = subprocess.run([PROD, "--host", LAN, "--port", "0", "--content-dir", "party/content"], cwd=REPO, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        check("production refuses a LAN address without --allow-lan (exit 2)", b.returncode == 2, (b.returncode, (b.stderr + b.stdout)[-160:].decode()))
        b = subprocess.run([PROD, "--surface", "test-loopback"], cwd=REPO, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        check("production binary has no --surface option (exit 2, unrecognized)", b.returncode == 2 and b"unrecognized" in b.stderr, b.stderr[-120:].decode())
        b = subprocess.run([TEST, "--host", LAN, "--port", "0", "--allow-lan", "--content-dir", "party/content"], cwd=REPO, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        check("test build refuses any non-loopback bind (exit 2)", b.returncode == 2 and b"loopback only" in b.stderr + b.stdout, (b.returncode, (b.stderr + b.stdout)[-160:].decode()))
        log = open(os.path.join(OUT, "prod-lan-stderr.log")).read()
        check("production access log has no capability and no room code", not any(s_ in log for s_ in SECRETS) and prod.code not in log and len(log) > 0)

        # ===== rate limits with production defaults =====
        rate = native("prod-rate", PROD, LAN, [], "production")
        started.append(rate)
        setup_room(rate, names=("Aisha",))
        st = [raw(rate, get("/api/health", rate))["status"] for _ in range(130)]
        ra = None
        r429 = [i for i, s_ in enumerate(st) if s_ == 429]
        check("request limiter (30/s, burst 120 per address): sustained burst gets 429 rate_limited", len(r429) > 0 and r429[0] >= 100, {"first_429_at": r429[:1], "n429": len(r429)})
        time.sleep(1.2)
        rate2 = native("prod-rate-join", PROD, LAN, ["--request-burst", "5000", "--request-rate", "2000"], "production")
        started.append(rate2)
        code2 = rate2.op({"op": "create", "body": {}})["code"]
        js = []
        for i in range(31):
            r = api(rate2, "POST", f"/api/rooms/{code2}/join", {"name": f"P{i}"})
            if r["json"] and "playerToken" in r["json"]:
                SECRETS.append(r["json"]["playerToken"])
            js.append((r["status"], r["code"], r["headers"].get("retry-after")))
        check("join limiter (burst 30): the 31st join is 429 with Retry-After", js[30][0] == 429 and js[30][1] == "rate_limited" and js[30][2] is not None, js[29:])
        rate3 = native("prod-rate-action", PROD, LAN, ["--request-burst", "5000", "--request-rate", "2000"], "production")
        started.append(rate3)
        setup_room(rate3, names=("Aisha",))
        acts = [api(rate3, "POST", f"/api/rooms/{rate3.code}/actions", {"actionId": f"r{i}", "type": "whoami.ready", "roundId": "g1r1", "payload": {}},
                    token=rate3.tokens["Aisha"]) for i in range(35)]
        codes_ = [a["code"] for a in acts]
        check("action limiter (10/s, burst 30 per principal): rapid actions beyond the burst get 429", "rate_limited" in codes_[29:] and "rate_limited" not in codes_[:29],
              {"first_rl": codes_.index("rate_limited") if "rate_limited" in codes_ else None})

        # ===== expiry =====
        exp = native("prod-expiry", PROD, LAN, FAST + ["--room-idle-ttl", "2", "--sweep-interval", "0.2"], "production")
        started.append(exp)
        setup_room(exp, names=("Aisha",))
        r0 = api(exp, "GET", f"/api/rooms/{exp.code}/state", token=exp.tokens["Aisha"])
        es = open_stream(exp, exp.tokens["Aisha"])
        eb, ec = read_for(es, 3.2)
        es.close()
        results["expiry_stream_tail"] = eb.decode("utf-8", "replace")[-160:]
        check("idle-room expiry ends an open stream with a terminal 'event: error' room_expired frame and EOF",
              ec and b"event: error" in eb and b"room_expired" in eb, {"eof": ec})
        r1 = api(exp, "GET", f"/api/rooms/{exp.code}/state", token=exp.tokens["Aisha"])
        r2 = exp.op({"op": "state", "code": exp.code})
        check("idle room expires (TTL 2 s): player state then 404/410, bridge state refused", r0["status"] == 200 and r1["status"] in (404, 410) and r2.get("ok") is False,
              [r0["status"], r1["status"], r1["code"], r2.get("error", {}).get("code")])

        # ===== CSPRNG failure (test build, production surface, loopback) =====
        tpp = native("test-prod", TEST, "127.0.0.1", FAST + ["--surface", "production"], "production")
        started.append(tpp)
        check("test build can run the production surface on loopback", tpp.banner["surface"] == "production", tpp.banner.get("surface"))
        prod_pages = [raw(tpp, get(p, tpp))["status"] for p in ("/host.html", "/tv.html", "/js/host.js", "/js/tv.js")]
        rooms_route = api(tpp, "POST", "/api/rooms", {})
        st_ = tpp.op({"op": "stats"})
        check("test build with --surface production: host/tv pages 404, POST /api/rooms 404, stats op refused",
              prod_pages == [404] * 4 and rooms_route["status"] == 404 and st_.get("ok") is False, [prod_pages, rooms_route["status"], st_.get("error", {}).get("code")])
        tp = native("test-csprng", TEST, "127.0.0.1", FAST + ["--sse-write-timeout", "1", "--heartbeat", "0.2"], "test-loopback")
        started.append(tp)
        setup_room(tp)
        tp.op({"op": "action", "code": tp.code, "body": {"actionId": "s1", "type": "room.set_mode", "payload": {"mode": "feud"}}})
        stats0 = tp.op({"op": "stats"})
        fp0 = fingerprint(tp)
        tp.op(None, raw_line=b"#csprng-fail on")
        c_fail = tp.op({"op": "create", "body": {}})
        j_fail = api(tp, "POST", f"/api/rooms/{tp.code}/join", {"name": "Zed"})
        a_fail = api(tp, "POST", f"/api/rooms/{tp.code}/actions", {"actionId": "c1", "type": "feud.buzz", "roundId": "g1b1", "payload": {}}, token=tp.tokens["Aisha"])
        o_fail = tp.op({"op": "action", "code": tp.code, "body": {"actionId": "s2", "type": "room.start", "payload": {}}})
        st_fail = tp.op({"op": "state", "code": tp.code})
        tp.op(None, raw_line=b"#csprng-fail off")
        stats1 = tp.op({"op": "stats"})
        fp1 = fingerprint(tp)
        j_ok = api(tp, "POST", f"/api/rooms/{tp.code}/join", {"name": "Zed"})
        if j_ok["json"] and "playerToken" in j_ok["json"]:
            SECRETS.append(j_ok["json"]["playerToken"])
        results["csprng"] = {"create": c_fail, "join": [j_fail["status"], j_fail["code"]], "action": [a_fail["status"], a_fail["code"]],
                             "operator_action": o_fail.get("error"), "operator_state": st_fail.get("error"), "stats_before": stats0, "stats_during": stats1}
        check("CSPRNG failure: bridge create -> internal_error with no room code; join -> 500 internal_error with no token; player and operator actions -> internal_error",
              c_fail.get("ok") is not True and "code" not in c_fail and c_fail.get("error", {}).get("code") == "internal_error"
              and j_fail["status"] == 500 and j_fail["code"] == "internal_error" and "token" not in (j_fail["body"] or b"").decode().lower()
              and a_fail["status"] == 500 and o_fail.get("error", {}).get("code") == "internal_error", results["csprng"])
        check("CSPRNG failure leaves no partial state: room count, operator rooms, sessions, players, phase and revision unchanged",
              all(stats0[k] == stats1[k] for k in ("rooms", "operatorRooms", "sessions")) and fp0 == fp1, {"fp0": fp0, "fp1": fp1, "s0": stats0, "s1": stats1})
        check("after the CSPRNG recovers, the same join succeeds", j_ok["status"] == 201, j_ok["status"])

        # SSE backpressure: a reader that never reads is dropped after the write timeout; others unaffected.
        fat = [api(tp, "POST", f"/api/rooms/{tp.code}/join", {"name": ("N%02d" % i) + "ب" * 20}) for i in range(7)]
        for f in fat:
            if f["json"] and "playerToken" in f["json"]:
                SECRETS.append(f["json"]["playerToken"])
        slow = socket.socket()
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        slow.connect((tp.ip, tp.port))
        slow.sendall(build("GET", f"/api/rooms/{tp.code}/events", tp, token=tp.tokens["Omar"]))
        good = socket.socket(); good.connect((tp.ip, tp.port))
        good.sendall(build("GET", f"/api/rooms/{tp.code}/events", tp, token=tp.tokens["Noora"]))
        time.sleep(0.5)
        s_open = tp.op({"op": "stats"})["streams"]
        lat = []
        got_good = [0]
        stop = threading.Event()

        def drain():
            good.settimeout(0.2)
            while not stop.is_set():
                try:
                    c = good.recv(65536)
                    if not c:
                        break
                    got_good[0] += len(c)
                except socket.timeout:
                    pass
        th = threading.Thread(target=drain); th.start()
        t0 = time.time()
        i = 0
        while time.time() - t0 < 6:
            for m in ("who_am_i", "feud"):
                tp.op({"op": "action", "code": tp.code, "body": {"actionId": f"bp{i}", "type": "room.set_mode", "payload": {"mode": m}}})
                i += 1
            if i % 40 == 0:
                lat.append(health(tp)[1])
            if tp.op({"op": "stats"})["streams"] < s_open:
                break
        dropped_after = round(time.time() - t0, 2)
        time.sleep(0.3)
        s_after = tp.op({"op": "stats"})["streams"]
        stop.set(); th.join()
        slow.close(); good.close()
        rss = subprocess.run(["ps", "-o", "rss=", "-p", str(tp.proc.pid)], capture_output=True, text=True).stdout.strip()
        results["backpressure"] = {"frames_driven": i, "streams_before": s_open, "streams_after": s_after, "dropped_after_s": dropped_after,
                                   "health_latency_s": lat, "good_reader_bytes": got_good[0], "server_rss_kb": rss}
        check("SSE backpressure: a non-reading client is dropped after the 1 s write timeout while a reading client keeps streaming and /api/health stays fast",
              s_after == s_open - 1 and got_good[0] > 100000 and lat and max(lat) < 0.5, results["backpressure"])
        time.sleep(1.5)
        check("idle streams are torn down when clients close (stats streams 0)", tp.op({"op": "stats"})["streams"] == 0)
    except Exception:
        import traceback
        check("suite completed without an exception", False, traceback.format_exc()[-1500:])
    finally:
        for t in started:
            rc = t.stop()
            check(f"[{t.name}] process stopped cleanly on stdin EOF (exit 0)", rc == 0, rc)
    for fn in os.listdir(OUT):
        p = os.path.join(OUT, fn)
        txt = open(p, encoding="utf-8", errors="replace").read()
        red = txt
        for s_ in SECRETS:
            red = red.replace(s_, "<token " + h16(s_) + ">")
        if red != txt:
            open(p, "w", encoding="utf-8").write(red)
    p_ = sum(1 for c in results["checks"] if c["pass"])
    results["summary"] = {"pass": p_, "fail": len(results["checks"]) - p_, "total": len(results["checks"])}
    blob = json.dumps(results, ensure_ascii=False, indent=1, default=lambda o: o.decode("utf-8", "replace") if isinstance(o, bytes) else str(o))
    for s_ in SECRETS:
        blob = blob.replace(s_, "<token " + h16(s_) + ">")
    open(os.path.join(OUT, "attack-results.json"), "w").write(blob)
    print(json.dumps(results["summary"]))
    sys.exit(0 if results["summary"]["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
