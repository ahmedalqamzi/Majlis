// L01B native adapter: the only script the Swift host calls into.
//
// Loaded after unicode13.generated.js and majlis-core.js in one bare
// JSContext (no injected natives). The host calls a fixed set of functions
// on the global `MajlisNative` with data-only arguments (strings, numbers,
// null, Uint8Array). Nothing here evaluates request text: bodies go through
// MajlisCore.json.parseRequestBody, and everything returned is a plain object
// whose `body` is the exact UTF-8 text to put on the wire.
//
// Surfaces
//   "production"     LAN-capable. No network room creation; only player
//                    principals are accepted from the network. Rooms are
//                    created and driven through the private operator bridge
//                    (operatorCall), whose host tokens never leave this file.
//   "test-loopback"  The full Python wire API (POST /api/rooms, host/display
//                    tokens over HTTP) for reusing the reference wire suites.
//                    The Swift host only selects it in a build compiled with
//                    MAJLIS_TEST_LOOPBACK, and that build binds loopback only.

var MajlisNative = (function (C) {
  "use strict";

  var PROTOCOL = 1;
  var PartyError = C.errors.PartyError, errorBody = C.errors.errorBody, statusFor = C.errors.statusFor;
  var PyFloat = C.json.PyFloat, PyBigInt = C.json.PyBigInt;
  var hasOwn = Object.prototype.hasOwnProperty;

  var S = {
    surface: null, manager: null, contents: null,
    sessions: new Map(), nextStream: 1,
    operatorTokens: new Map(),   // code -> host token (production bridge only; never returned)
    diagnostics: []              // class names only, bounded
  };

  // ------------------------------------------------------------ json.dumps
  // json.dumps(obj, ensure_ascii=False, separators=(",", ":")) for the value
  // shapes the core produces. A lone surrogate cannot be UTF-8 encoded; the
  // reference raises UnicodeEncodeError there, which the transport turns into
  // internal_error, so this throws EncodeError.
  function EncodeError(msg) { this.message = msg; }
  function hex4(c) { return ("000" + c.toString(16)).slice(-4); }
  function dumpStr(s, ascii) {
    var out = "\"";
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c === 0x22) out += "\\\"";
      else if (c === 0x5C) out += "\\\\";
      else if (c < 0x20) {
        if (c === 0x0A) out += "\\n";
        else if (c === 0x0D) out += "\\r";
        else if (c === 0x09) out += "\\t";
        else if (c === 0x08) out += "\\b";
        else if (c === 0x0C) out += "\\f";
        else out += "\\u" + hex4(c);
      } else if (c >= 0xD800 && c <= 0xDFFF) {
        var d = i + 1 < s.length ? s.charCodeAt(i + 1) : 0;
        if (c <= 0xDBFF && d >= 0xDC00 && d <= 0xDFFF) {
          out += ascii ? "\\u" + hex4(c) + "\\u" + hex4(d) : s.substr(i, 2);
          i++;
        } else if (ascii) {
          out += "\\u" + hex4(c);
        } else {
          throw new EncodeError("lone surrogate");
        }
      } else if (ascii && c > 0x7E) {
        out += "\\u" + hex4(c);
      } else {
        out += s[i];
      }
    }
    return out + "\"";
  }
  // float.__repr__: shortest round-trip digits; exponent form when the
  // decimal exponent is < -4 or >= 16; at least two exponent digits.
  function floatRepr(v) {
    if (v !== v) return "NaN";
    if (v === Infinity) return "Infinity";
    if (v === -Infinity) return "-Infinity";
    if (v === 0) return Object.is(v, -0) ? "-0.0" : "0.0";
    var sign = v < 0 ? "-" : "";
    var m = /^(\d)(?:\.(\d+))?e([+-]\d+)$/.exec(Math.abs(v).toExponential());
    var digits = m[1] + (m[2] || ""), exp = parseInt(m[3], 10);
    digits = digits.replace(/0+$/, "") || "0";
    if (exp < -4 || exp >= 16) {
      var mant = digits.length > 1 ? digits[0] + "." + digits.slice(1) : digits;
      var e = Math.abs(exp), es = (e < 10 ? "0" : "") + e;
      return sign + mant + "e" + (exp < 0 ? "-" : "+") + es;
    }
    var point = exp + 1;
    if (point <= 0) return sign + "0." + new Array(1 - point).join("0") + digits;
    if (point >= digits.length) return sign + digits + new Array(point - digits.length + 1).join("0") + ".0";
    return sign + digits.slice(0, point) + "." + digits.slice(point);
  }
  function dumpNum(v) {
    if (Number.isInteger(v)) return Object.is(v, -0) ? "0" : String(v);
    return floatRepr(v); // json.dumps(float)
  }
  function dumps(v) {
    if (v === null) return "null";
    if (v === true) return "true";
    if (v === false) return "false";
    if (typeof v === "string") return dumpStr(v, false);
    if (typeof v === "number") return dumpNum(v);
    if (v instanceof PyFloat) return floatRepr(v.value);
    if (v instanceof PyBigInt) return v.digits;
    if (Array.isArray(v)) {
      var parts = [];
      for (var i = 0; i < v.length; i++) parts.push(dumps(v[i]));
      return "[" + parts.join(",") + "]";
    }
    if (typeof v === "object") {
      var keys = Object.keys(v), items = [];
      for (var k = 0; k < keys.length; k++) items.push(dumpStr(keys[k], false) + ":" + dumps(v[keys[k]]));
      return "{" + items.join(",") + "}";
    }
    throw new EncodeError("unsupported value");
  }
  // json.dumps(obj, sort_keys=True): ensure_ascii, ", " and ": " separators.
  // Used for the CLI's banner, content report and stop lines.
  function dumpsSorted(v) {
    if (v === null || typeof v !== "object" || v instanceof PyFloat || v instanceof PyBigInt) {
      return typeof v === "string" ? dumpStr(v, true) : dumps(v);
    }
    if (Array.isArray(v)) return "[" + v.map(dumpsSorted).join(", ") + "]";
    var keys = Object.keys(v).sort();
    return "{" + keys.map(function (k) { return dumpStr(k, true) + ": " + dumpsSorted(v[k]); }).join(", ") + "}";
  }

  // ------------------------------------------------------------- helpers
  function note(e) {
    var name = "Error";
    if (e instanceof EncodeError) name = "EncodeError";
    else if (e && e.constructor && typeof e.constructor.name === "string") name = e.constructor.name;
    name = name.replace(/[^A-Za-z]/g, "").slice(0, 40) || "Error";
    if (S.diagnostics.length < 64) S.diagnostics.push(name);
  }
  function resp(status, payload, extra) {
    var r = { status: status, body: dumps(payload), replayed: -1 };
    if (extra) for (var k in extra) if (hasOwn.call(extra, k)) r[k] = extra[k];
    return r;
  }
  function errorResp(code) {
    return { status: statusFor(code), body: dumps({ ok: false, error: errorBody(code) }), replayed: -1, code: code };
  }
  function guarded(fn) {
    try { return fn(); }
    catch (e) {
      if (e instanceof PartyError) return errorResp(e.code);
      note(e);
      return errorResp("internal_error");
    }
  }
  function requireReady() { if (S.manager === null) throw new PartyError("internal_error"); }
  function isTest() { return S.surface === "test-loopback"; }
  function requireTest() { if (!isTest()) throw new PartyError("not_found"); }
  // _read_json after the transport has handled framing, length and media
  // type: null or zero bytes means an allowed empty body.
  function readBody(bytes, allowEmpty) {
    if (bytes === null || bytes === undefined || bytes.length === 0) {
      if (allowEmpty) return Object.create(null);
      throw new PartyError("invalid_json");
    }
    return C.json.parseRequestBody(bytes);
  }
  function wipe(bytes) { if (bytes && typeof bytes.fill === "function") bytes.fill(0); }
  // Network authentication. Production accepts player principals only: an
  // operator or display capability is unusable from the LAN even if it were
  // somehow presented.
  function authNetwork(room, token, now) {
    var principal = S.manager.authenticate(room, token, now);
    if (!isTest() && principal.role !== "player") throw new PartyError("unauthorized");
    return principal;
  }

  // ------------------------------------------------------------- lifecycle
  // config: snake_case ServerConfig overrides; packs: {mode: Uint8Array|null}.
  function init(surface, config, packs) {
    if (surface !== "production" && surface !== "test-loopback") throw new Error("surface");
    var cfg;
    try { cfg = new C.ServerConfig(config); }
    catch (e) { return { ok: false, detail: String(e.message).replace(/^config: /, "") }; }
    S.surface = surface;
    S.contents = C.content.loadAll(packs || {});
    S.manager = new C.RoomManager(cfg, S.contents);
    return { ok: true };
  }
  // ContentStatus per implemented mode, for banner and --check-content.
  function contentReport() {
    var contents = S.contents || C.content.loadAll({});
    var statuses = {}, report = {}, allOk = true;
    contents.forEach(function (st, mode) {
      statuses[mode] = st.status;
      report[mode] = { status: st.status, issues: st.issues.slice() };
      if (st.status !== "ok") allOk = false;
    });
    return { statuses: statuses, report: report, ok: allOk };
  }
  function checkContent(packs) {
    S.contents = C.content.loadAll(packs || {});
    var r = contentReport();
    return { ok: r.ok, line: dumpsSorted({ event: "content_report", modes: r.report }) };
  }
  function banner(host, port, web, surface) {
    return dumpsSorted({
      event: "listening", protocol: PROTOCOL, host: host, port: port, url: "http://" + host + ":" + port,
      content: contentReport().statuses,
      persistence: "memory-only: all rooms are lost when this process stops",
      web: web ? "allowlist" : "disabled", surface: surface, engine: "jsc", core: C.version
    });
  }
  function line(obj) { return dumpsSorted(obj); }
  function sweep(now) { return guarded(function () { requireReady(); return { status: 0, removed: S.manager.sweep(now) }; }); }
  function shutdown() { if (S.manager) S.manager.shutdown(); return true; }
  function takeDiagnostics() { var d = S.diagnostics; S.diagnostics = []; return d; }

  // ------------------------------------------------------------ wire routes
  function health() { return resp(200, { ok: true, protocol: PROTOCOL }); }
  function modes() { return guarded(function () { requireReady(); return resp(200, { protocol: PROTOCOL, modes: C.modes.catalog(S.contents) }); }); }
  function errorFor(code) { return errorResp(code); }

  // POST /api/rooms, test-loopback surface only.
  function create(bodyBytes, now, entropy) {
    try {
      return guarded(function () {
        requireReady(); requireTest();
        var body = readBody(bodyBytes, true);
        return resp(201, S.manager.createRoom(body, now, new C.EntropyPool(entropy)));
      });
    } finally { wipe(entropy); }
  }
  // POST /api/rooms/:code/join. bearer: 0 absent, 1 present (token), 2 malformed.
  function join(code, bodyBytes, bearer, token, now, entropy) {
    try {
      return guarded(function () {
        requireReady();
        var body = readBody(bodyBytes, true);
        if (bearer === 2) throw new PartyError("unauthorized");
        var room = S.manager.getRoom(code, now);
        var t = bearer === 1 ? token : null;
        if (t !== null && !isTest()) authNetwork(room, t, now); // test surface: manager.join decides, as the reference
        var r = S.manager.join(room, body, t, now, new C.EntropyPool(entropy));
        return resp(r[0], r[1]);
      });
    } finally { wipe(entropy); }
  }
  function state(code, bearer, token, now) {
    return guarded(function () {
      requireReady();
      if (bearer !== 1) throw new PartyError("unauthorized");
      var room = S.manager.getRoom(code, now);
      var principal = authNetwork(room, token, now);
      return resp(200, S.manager.snapshot(room, principal, now));
    });
  }
  // POST /api/rooms/:code/actions, phase 1: body, bearer, room, principal.
  // Returns {status: 0, limiterKey, ctx} or an error response. The host
  // applies the action rate limit, then calls actionCommit(ctx, ...) in the
  // same serialized turn, so nothing can run between the two calls.
  function actionPrepare(code, bodyBytes, bearer, token, now) {
    return guarded(function () {
      requireReady();
      var body = readBody(bodyBytes, false);
      if (bearer !== 1) throw new PartyError("unauthorized");
      var room = S.manager.getRoom(code, now);
      var principal = authNetwork(room, token, now);
      return { status: 0, limiterKey: room.code + "/" + principal.key(), ctx: { room: room, principal: principal, body: body } };
    });
  }
  function actionCommit(ctx, now, entropy) {
    try {
      return guarded(function () {
        requireReady();
        var r = S.manager.submitAction(ctx.room, ctx.principal, ctx.body, now, new C.EntropyPool(entropy));
        return resp(r[0], r[1], { replayed: r[2] ? 1 : 0 });
      });
    } finally { wipe(entropy); ctx.body = null; }
  }

  // ------------------------------------------------------------------- SSE
  // GET /api/rooms/:code/events: bearer, room, principal, open_stream.
  function streamOpen(code, bearer, token, now) {
    return guarded(function () {
      requireReady();
      if (bearer !== 1) throw new PartyError("unauthorized");
      var room = S.manager.getRoom(code, now);
      var principal = authNetwork(room, token, now);
      var session = new C.EventSession(S.manager, room, principal, now);
      var id = S.nextStream++;
      S.sessions.set(id, session);
      return { status: 0, id: id };
    });
  }
  // One step of the stream loop. kind: "state" | "heartbeat" | "error" |
  // "close" | "wait". frame is the exact SSE text to write; wait in seconds.
  function streamPoll(id, now) {
    var session = S.sessions.get(id);
    if (!session || !session.open) { S.sessions.delete(id); return { kind: "close" }; }
    try {
      var ev = session.poll(now);
      switch (ev[0]) {
        case "wait": return { kind: "wait", wait: ev[1] };
        case "heartbeat": return { kind: "heartbeat", frame: ": heartbeat\n\n" };
        case "state": return { kind: "state", revision: ev[1], frame: "id: " + ev[1] + "\nevent: state\ndata: " + dumps(ev[2]) + "\n\n" };
        case "error":
          S.sessions.delete(id);
          return { kind: "error", frame: "event: error\ndata: " + dumps({ ok: false, error: errorBody(ev[1]) }) + "\n\n" };
        default:
          S.sessions.delete(id);
          return { kind: "close" };
      }
    } catch (e) {
      note(e);
      streamClose(id);
      return { kind: "close" };
    }
  }
  // Idempotent teardown (close_stream in the reference's finally).
  function streamClose(id) {
    var session = S.sessions.get(id);
    S.sessions.delete(id);
    if (session) { try { session.end(); } catch (e) { note(e); } return true; }
    return false;
  }
  function streamCount() { return S.sessions.size; }

  // ------------------------------------------------- private operator bridge
  // One JSON object per call, parsed as data by the reviewed body parser:
  //   {"op":"create","body":{...}}            -> {"ok":true,"op":"create","code","revision"}
  //   {"op":"action","code":C,"body":{...}}   -> {"ok":true,"op":"action","status","result","replayed"}
  //   {"op":"state","code":C}                 -> {"ok":true,"op":"state","state":{...operator view}}
  //   {"op":"forget","code":C}                -> {"ok":true,"op":"forget"}
  //   {"op":"stats"} (test-loopback only)     -> {"ok":true,"op":"stats","rooms","streams","operatorRooms"}
  // The host token stays in S.operatorTokens; the display token is dropped
  // at creation. Neither is ever part of a returned value.
  function operatorCall(requestBytes, now, entropy) {
    var op = null;
    try {
      requireReady();
      var req = C.json.parseRequestBody(requestBytes);
      op = typeof req.op === "string" && /^[a-z]{1,12}$/.test(req.op) ? req.op : null;
      var body = hasOwn.call(req, "body") && req.body !== null ? req.body : Object.create(null);
      if (!C.json.isDict(body)) throw new PartyError("invalid_json");
      var code = typeof req.code === "string" ? req.code : null;
      if (op === "create") {
        var created = S.manager.createRoom(body, now, new C.EntropyPool(entropy));
        S.operatorTokens.set(created.code, created.hostToken);
        created.hostToken = null; created.displayToken = null;
        return line({ ok: true, op: op, code: created.code, revision: created.revision });
      }
      if (op === "action" || op === "state") {
        if (code === null) throw new PartyError("room_not_found");
        var room = S.manager.getRoom(code, now);
        var token = S.operatorTokens.get(room.code);
        if (token === undefined) throw new PartyError("unauthorized");
        var principal = S.manager.authenticate(room, token, now);
        if (principal.role !== "operator") throw new PartyError("unauthorized");
        if (op === "state") return "{\"ok\":true,\"op\":\"state\",\"state\":" + dumps(S.manager.snapshot(room, principal, now)) + "}";
        var r = S.manager.submitAction(room, principal, body, now, new C.EntropyPool(entropy));
        return "{\"ok\":true,\"op\":\"action\",\"status\":" + r[0] + ",\"result\":" + dumps(r[1]) +
          ",\"replayed\":" + (r[2] ? "true" : "false") + "}";
      }
      if (op === "forget") {
        if (code !== null) S.operatorTokens.delete(code);
        return line({ ok: true, op: op });
      }
      if (op === "stats" && isTest()) {
        return line({ ok: true, op: op, rooms: S.manager.rooms.size, streams: S.manager.streamTotal,
          sessions: S.sessions.size, operatorRooms: S.operatorTokens.size });
      }
      throw new PartyError("unknown_action");
    } catch (e) {
      var codeName = e instanceof PartyError ? e.code : "internal_error";
      if (!(e instanceof PartyError)) note(e);
      return "{\"ok\":false,\"op\":" + (op === null ? "null" : dumpStr(op, false)) +
        ",\"error\":" + dumps(errorBody(codeName)) + "}";
    } finally { wipe(entropy); }
  }

  return {
    init: init, checkContent: checkContent, banner: banner, line: line, sweep: sweep, shutdown: shutdown,
    takeDiagnostics: takeDiagnostics, health: health, modes: modes, errorFor: errorFor,
    create: create, join: join, state: state, actionPrepare: actionPrepare, actionCommit: actionCommit,
    streamOpen: streamOpen, streamPoll: streamPoll, streamClose: streamClose, streamCount: streamCount,
    operatorCall: operatorCall,
    _test: { dumps: dumps, dumpsSorted: dumpsSorted, floatRepr: floatRepr }
  };
})(MajlisCore);
