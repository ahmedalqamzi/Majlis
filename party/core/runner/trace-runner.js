// L01A parity replay runner (test tool, not shipped in the app bundle).
//
// Replays the static ops recorded by party/core/tools/oracle_record.py
// against MajlisCore and writes one output line per input line, in the same
// shape the recorder stores under "py". Every harness seam mirrors the
// recorder: the entropy pool derivation, the _read_json wrapper with
// harness-set headers, reference tables, token redaction and the per-room
// observation. Trace files are parsed with the host JSON.parse because they
// are harness data; request bodies always go through MajlisCore's own parser.
"use strict";

var MajlisTraceRunner = (function (Core) {
  var hasOwn = Object.prototype.hasOwnProperty;
  var PartyError = Core.errors.PartyError, PyFloat = Core.json.PyFloat, PyBigInt = Core.json.PyBigInt;
  var CODE_RE = /^[A-Z2-9]{6}$/;
  var POOL_BYTES = 16384;

  // ------------------------------------------------------------ byte utils
  var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  var B64REV = {};
  for (var bi = 0; bi < 64; bi++) B64REV[B64[bi]] = bi;
  function b64decode(s) {
    var clean = s.replace(/=+$/, ""), out = new Uint8Array(Math.floor(clean.length * 3 / 4)), o = 0, acc = 0, bits = 0;
    for (var i = 0; i < clean.length; i++) {
      var v = B64REV[clean[i]];
      if (v === undefined) throw new Error("bad base64");
      acc = (acc << 6) | v; bits += 6;
      if (bits >= 8) { bits -= 8; out[o++] = (acc >> bits) & 255; }
    }
    return out.subarray(0, o);
  }
  function utf8encode(s) { // strict: recorder bodies never hold lone surrogates
    var out = [];
    for (var ch of s) {
      var cp = ch.codePointAt(0);
      if (cp >= 0xD800 && cp <= 0xDFFF) throw new Error("lone surrogate in utf-8 body");
      if (cp < 0x80) out.push(cp);
      else if (cp < 0x800) out.push(0xC0 | (cp >> 6), 0x80 | (cp & 63));
      else if (cp < 0x10000) out.push(0xE0 | (cp >> 12), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
      else out.push(0xF0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
    }
    return new Uint8Array(out);
  }
  function ascii(s) {
    var b = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) b[i] = s.charCodeAt(i);
    return b;
  }
  function hexBytes(h) {
    var b = new Uint8Array(h.length / 2);
    for (var i = 0; i < b.length; i++) b[i] = parseInt(h.substr(i * 2, 2), 16);
    return b;
  }

  // Same derivation as the recorder: SHA-256(seed ":" i) blocks.
  function poolBytes(seed, size) {
    var out = new Uint8Array(size), filled = 0, i = 0;
    while (filled < size) {
      var block = hexBytes(Core.crypto.sha256Hex(ascii(seed + ":" + i)));
      var n = Math.min(32, size - filled);
      out.set(block.subarray(0, n), filled);
      filled += n; i++;
    }
    return out;
  }
  function PoolExhausted() { this.name = "PoolExhausted"; }
  function harnessPool(bytes) {
    var pool = new Core.EntropyPool(bytes);
    pool.take = function (n) { // the recorder raises PoolExhausted, not a party error
      if (this.used + n > this.bytes.length) throw new PoolExhausted();
      return Core.EntropyPool.prototype.take.call(this, n);
    };
    return pool;
  }
  function HarnessKeyError(key) { this.name = "KeyError"; this.message = "'" + key + "'"; }

  // ------------------------------------------------------------ helpers
  function redact(value) {
    if (Array.isArray(value)) return value.map(redact);
    if (value !== null && typeof value === "object" && !(value instanceof PyFloat) && !(value instanceof PyBigInt)) {
      var out = {};
      Object.keys(value).forEach(function (k) {
        var v = value[k];
        if (k.endsWith("Token") && typeof v === "string") out[k] = { sha256: Core.crypto.hashToken(v) };
        else out[k] = redact(v);
      });
      return out;
    }
    return value;
  }
  function tag(v) {
    if (v === null || v === undefined) return { n: 1 };
    if (typeof v === "boolean") return { b: v };
    if (typeof v === "number") return { i: String(v) };
    if (v instanceof PyBigInt) return { i: v.digits };
    if (v instanceof PyFloat) {
      var f = v.value;
      if (f !== f) return { f: "nan" };
      if (f === Infinity) return { f: "inf" };
      if (f === -Infinity) return { f: "-inf" };
      if (Object.is(f, -0)) return { f: "-0" };
      return { f: f };
    }
    if (typeof v === "string") return { s: v };
    if (Array.isArray(v)) return { l: v.map(tag) };
    return { d: Object.keys(v).map(function (k) { return [k, tag(v[k])]; }) };
  }
  function bodyBytes(spec) {
    if (hasOwn.call(spec, "empty")) return null;
    if (hasOwn.call(spec, "text")) return utf8encode(spec.text);
    return b64decode(spec.b64);
  }
  // httpd._read_json with the recorder's FakeRequest: no headers for an empty
  // spec, otherwise Content-Length = len(data) and application/json.
  function readJson(config, spec, allowEmpty) {
    var data = bodyBytes(spec);
    if (data === null) {
      if (allowEmpty) return {};
      throw new PartyError("length_required");
    }
    if (data.length > config.max_body_bytes) throw new PartyError("body_too_large");
    if (data.length === 0) {
      if (allowEmpty) return {};
      throw new PartyError("invalid_json");
    }
    return Core.json.parseRequestBody(data);
  }
  function errorOut(e) {
    if (e instanceof PartyError) return { status: e.status, error: Core.errors.errorBody(e.code) };
    if (e instanceof PoolExhausted) return { exception: "PoolExhausted" };
    if (e instanceof HarnessKeyError) return { exception: "KeyError", detail: e.message };
    var name = (e && e.constructor && e.constructor.name) || "Error";
    return { exception: "JS:" + name, detail: String(e && e.message || e).slice(0, 200) };
  }

  // ------------------------------------------------------------ room traces
  var blobs = null;

  function Trace(header) {
    this.name = header.trace;
    this.now = 1000.0;
    this.pool = harnessPool(poolBytes(header.entropy.seed, header.entropy.bytes));
    var packs = {};
    Object.keys(header.content).forEach(function (mode) {
      var spec = header.content[mode];
      if (typeof spec === "string") packs[mode] = blobs[spec + "/" + mode];
      else packs[mode] = spec === null ? null : b64decode(spec.b64);
    });
    this.contents = Core.content.loadAll(packs);
    this.config = new Core.ServerConfig(header.config);
    this.mgr = new Core.RoomManager(this.config, this.contents, { freezeState: true });
    this.rooms = {}; this.roomOrder = []; this.tokens = {}; this.pids = {}; this.sessions = {};
    this.prevObs = {};
  }
  Trace.prototype._room = function (ref) {
    if (hasOwn.call(ref, "code")) return ref.code;
    if (!hasOwn.call(this.rooms, ref.ref)) throw new HarnessKeyError(ref.ref);
    return this.rooms[ref.ref];
  };
  Trace.prototype._token = function (ref) {
    if (ref === null || ref === undefined) return null;
    if (hasOwn.call(ref, "literal")) return ref.literal;
    return hasOwn.call(this.tokens, ref.ref) ? this.tokens[ref.ref] : null;
  };
  Trace.prototype.run = function (op) {
    if (hasOwn.call(op, "now")) this.now = Number(op.now);
    var out;
    try { out = this._apply(op); } catch (e) { out = errorOut(e); }
    return { out: out, obs: this.observe() };
  };
  Trace.prototype._auth = function (op) {
    var token = this._token(op.token);
    if (token === null) throw new PartyError("unauthorized");
    var room = this.mgr.getRoom(this._room(op.room), this.now);
    return [room, this.mgr.authenticate(room, token, this.now)];
  };
  Trace.prototype._apply = function (op) {
    var mgr = this.mgr, now = this.now, ra, s, ev;
    switch (op.op) {
      case "create": {
        var cbody = readJson(this.config, op.body, true);
        var result = mgr.createRoom(cbody, now, this.pool);
        if (op.as) {
          this.rooms[op.as] = result.code;
          this.tokens[op.as + ".host"] = result.hostToken;
          this.tokens[op.as + ".display"] = result.displayToken;
          this.roomOrder.push([op.as + "#" + this.roomOrder.length, mgr.rooms.get(result.code)]);
        }
        return { status: 201, body: redact(result) };
      }
      case "join": {
        var jbody = readJson(this.config, op.body, true);
        var token = this._token(op.token);
        var room = mgr.getRoom(this._room(op.room), now);
        var jr = mgr.join(room, jbody, token, now, this.pool);
        if (op.as) {
          this.tokens[op.as] = jr[1].playerToken;
          this.pids[op.as] = jr[1].playerId;
        }
        return { status: jr[0], body: redact(jr[1]) };
      }
      case "state":
        ra = this._auth(op);
        return { status: 200, body: mgr.snapshot(ra[0], ra[1], now) };
      case "action": {
        var abody = readJson(this.config, op.body, false);
        ra = this._auth(op);
        var sr = mgr.submitAction(ra[0], ra[1], abody, now, this.pool);
        return { status: sr[0], body: sr[1], replayed: sr[2] };
      }
      case "open":
        ra = this._auth(op);
        this.sessions[op.as] = new Core.EventSession(mgr, ra[0], ra[1], now);
        return { status: 200, opened: true };
      case "poll":
        s = hasOwn.call(this.sessions, op.stream) ? this.sessions[op.stream] : null;
        if (s === null) return { harness: "no stream" };
        if (!s.open) return { harness: "stream not open" };
        ev = s.poll(now);
        if (ev[0] === "wait") return { event: "wait", timeout: ev[1] };
        if (ev[0] === "close") return { event: "close" };
        if (ev[0] === "error") return { event: "error", error: Core.errors.errorBody(ev[1]) };
        if (ev[0] === "state") return { event: "state", id: ev[1], data: ev[2] };
        return { event: "heartbeat" };
      case "close":
        s = hasOwn.call(this.sessions, op.stream) ? this.sessions[op.stream] : null;
        if (s === null) return { harness: "no stream" };
        if (s.open) { s.end(); return { closed: true }; }
        return { closed: false };
      case "sweep":
        return { removed: mgr.sweep(now) };
      case "shutdown":
        mgr.shutdown();
        return { stopped: true };
      case "lookup":
        return { found: mgr.getRoom(this._room(op.room), now).code };
    }
    throw new Error("unknown op " + op.op);
  };
  function pairs(map) { return Array.from(map.entries()).map(function (e) { return [e[0], e[1]]; }); }
  Trace.prototype.observe = function () {
    var mgr = this.mgr, rooms = {}, self = this;
    this.roomOrder.forEach(function (entry) {
      var ref = entry[0], room = entry[1];
      var obs = {
        inTable: mgr.rooms.get(room.code) === room,
        state: room.state,
        revision: room.revision,
        receiptSeq: room.receiptSeq,
        expired: room.expired,
        createdAt: room.createdAt,
        lastActivity: room.lastActivity,
        closedAt: room.closedAt,
        streams: pairs(room.streams),
        presence: pairs(room.presence),
        streamTotal: room.streamTotal,
        tokens: Array.from(room.tokens.entries()).map(function (e) {
          return [e[0], e[1].role, e[1].playerId, e[1].expiresAt];
        }),
        actions: Array.from(room.actions.entries()).map(function (e) {
          return [e[0], Array.from(e[1].entries()).map(function (a) { return [a[0], a[1][0], a[1][1]]; })];
        }),
        projections: Array.from(room.tokens.values()).map(function (p) { return [p.key(), mgr.project(room, p)]; })
      };
      // Dedup is a size optimisation only; compare.py expands {"same": true}.
      var canon = JSON.stringify(obs);
      if (self.prevObs[ref] === canon) rooms[ref] = { same: true };
      else { self.prevObs[ref] = canon; rooms[ref] = obs; }
    });
    return { rooms: rooms, table: Array.from(mgr.rooms.keys()), streamTotal: mgr.streamTotal,
      stopped: mgr.stopped, entropyUsed: this.pool.used };
  };

  // ------------------------------------------------------------ other suites
  var U = Core.unicode;
  function textRecord(s) {
    var up = U.upperForCode(s);
    return { nfc: U.nfc(s), nfd: U.nfd(s), nfkc: U.nfkc(s), nfkd: U.nfkd(s), casefold: U.casefold(s),
      matchKey: U.matchKey(s), split: U.pySplit(s), strip: U.pyStrip(s), len: U.pyLen(s),
      clean24: U.cleanText(s, 24), clean80: U.cleanText(s, 80), clean120: U.cleanText(s, 120),
      isText500: Core.content.isText(s), code: up !== null && CODE_RE.test(up) ? up : null };
  }
  function cpList(s) { var out = []; for (var ch of s) out.push(ch.codePointAt(0)); return out; }
  function sweepRecord() {
    var entries = [], rejected = [], start = null;
    for (var cp = 0; cp < 0x110000; cp++) {
      var ch = String.fromCodePoint(cp), e = {}, any = false;
      var vals = [["nfc", U.nfc(ch)], ["nfkc", U.nfkc(ch)], ["nfd", U.nfd(ch)], ["nfkd", U.nfkd(ch)],
        ["casefold", U.casefold(ch)], ["matchKey", U.matchKey(ch)]];
      for (var k = 0; k < vals.length; k++) {
        if (vals[k][1] !== ch) { e[vals[k][0]] = cpList(vals[k][1]); any = true; }
      }
      var up = U.upperForCode(ch);
      if (up !== null && up !== ch) { e.code = cpList(up); any = true; }
      if (any) { e.cp = cp; entries.push(e); }
      var bad = U.cleanText(ch, 24) === null;
      if (bad && start === null) start = cp;
      else if (!bad && start !== null) { rejected.push([start, cp - 1]); start = null; }
    }
    if (start !== null) rejected.push([start, 0x10FFFF]);
    return { entries: entries, cleanRejected: rejected };
  }
  var defaultConfig = null;
  function jsonRecord(b64) {
    try { return { value: tag(readJson(defaultConfig, { b64: b64 }, false)) }; }
    catch (e) {
      if (e instanceof PartyError) return { error: e.code };
      throw e;
    }
  }
  function depthRecord(depth) {
    var text = '{"a":' + "[".repeat(depth) + "]".repeat(depth) + "}";
    try { readJson(defaultConfig, { text: text }, false); return { thread: "ok" }; }
    catch (e) {
      if (e instanceof PartyError) return { thread: e.code };
      return { thread: "JS:" + String(e && e.message || e) };
    }
  }
  function contentRecord(mode, b64) {
    var spec = Core.modes.getMode(mode);
    var st = Core.content.loadMode(mode, b64 === null ? null : b64decode(b64), spec.module.validateItem,
      spec.module.validateItems);
    return { status: st.status, issues: st.issues, pack: st.pack };
  }

  // ------------------------------------------------------------ driver
  var suite = null, trace = null;
  function handle(rec) {
    if (hasOwn.call(rec, "suite")) {
      suite = rec.suite; trace = null;
      if (rec.blobs) {
        blobs = {};
        Object.keys(rec.blobs).forEach(function (k) { blobs[k] = b64decode(rec.blobs[k]); });
      }
      defaultConfig = new Core.ServerConfig({});
      return { suite: suite, core: Core.version, unidata: Core.unicode.unidata };
    }
    if (suite === "golden" || suite === "fuzz") {
      if (hasOwn.call(rec, "trace")) { trace = new Trace(rec); return { trace: rec.trace }; }
      return { js: trace.run(rec.op) };
    }
    if (suite === "text") return { js: textRecord(rec["in"]) };
    if (suite === "sweep") return { js: sweepRecord() };
    if (suite === "json") return { js: jsonRecord(rec.b64) };
    if (suite === "depth") return { js: depthRecord(rec.depth) };
    if (suite === "content") return { js: contentRecord(rec.mode, rec.b64) };
    if (suite === "shuffle") {
      var x = [];
      for (var i = 0; i < rec.n; i++) x.push(i);
      return { js: Core.random.shuffleInPlace(rec.seed, x) };
    }
    if (suite === "errors") {
      var errs = {};
      Object.keys(Core.errors.ERRORS).forEach(function (k) { errs[k] = Core.errors.ERRORS[k].slice(); });
      return { js: errs };
    }
    throw new Error("unknown suite " + suite);
  }
  function runLine(line) {
    var out;
    try { out = handle(JSON.parse(line)); }
    catch (e) { out = { runnerError: String(e && e.stack || e).slice(0, 500) }; }
    return JSON.stringify(out);
  }

  return { runLine: runLine, poolBytes: poolBytes, b64decode: b64decode, utf8encode: utf8encode };
})(MajlisCore);
