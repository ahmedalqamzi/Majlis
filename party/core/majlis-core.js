// Majlis shared authority core (L01A candidate, protocol v1).
//
// Pure-JS port of the accepted Python server at 77ecabd: errors.py,
// textutil.py, content.py, modes/{__init__,who_am_i,feud}.py, room_logic.py
// and the RoomManager in manager.py. HTTP parsing, rate limits, Host/Origin
// checks and bearer parsing stay in the transport (L01B) and are NOT here.
//
// Rules for this file:
// * Classic script, ES2020, strict. No DOM, network, timers, storage, eval,
//   Function(), Math.random, Date or native callbacks. Time is a `now`
//   argument (seconds, double); entropy is an EntropyPool of bytes supplied by
//   the caller (production: SecRandomCopyBytes / SecureRandom).
// * Text semantics are CPython 3.9 + unicodedata 13.0.0, from the generated
//   tables in unicode13.generated.js. String.prototype.normalize, toUpperCase,
//   toLowerCase and \s are never used for protocol decisions: host ICU tracks
//   a newer Unicode and drifts.
// * Python dict get semantics: has(o, k) decides presence; a present JSON
//   null is Python None, never the default.
// * JSON numbers: safe ints are JS numbers, ints beyond 2^53 are PyBigInt
//   boxes holding exact digits, floats are PyFloat boxes so that
//   is_int rejects 1.0 and 1e2 exactly like Python.
"use strict";

var MajlisCore = (function (T) {
  if (!T || T.unidata !== "13.0.0") throw new Error("unicode13 tables missing or wrong version");

  var hasOwn = Object.prototype.hasOwnProperty;
  function has(o, k) { return o !== null && typeof o === "object" && hasOwn.call(o, k); }
  // dict.get(key, default): present key wins even when its value is null.
  function get(o, k, dflt) { return has(o, k) ? o[k] : (dflt === undefined ? null : dflt); }
  function isNone(v) { return v === null || v === undefined; }
  function obj() { return Object.create(null); }
  // Python bool(v) for JSON-decoded values.
  function pyTruthy(v) {
    if (v === null || v === undefined || v === false) return false;
    if (v === true) return true;
    if (typeof v === "number") return v !== 0;
    if (typeof v === "string" || Array.isArray(v)) return v.length > 0;
    if (v instanceof PyFloat) return v.value !== 0; // NaN is truthy in Python too
    if (v instanceof PyBigInt) return true; // only built for |v| > 2^53
    return Object.keys(v).length > 0;
  }

  // ------------------------------------------------------------- PyFloat
  function PyFloat(value) { this.value = value; }
  PyFloat.prototype.toJSON = function () {
    var v = this.value;
    if (v !== v) return "NaN";
    if (v === Infinity) return "Infinity";
    if (v === -Infinity) return "-Infinity";
    return Object.is(v, -0) ? "-0.0" : v;
  };
  // An int outside the IEEE-safe range keeps its exact decimal digits. Every
  // protocol int is range-checked to small bounds, so such a value can only
  // ever fail is_int (as in Python) or be reported back by the trace tools.
  function PyBigInt(digits) { this.digits = digits; }
  function isPyDict(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v) && !(v instanceof PyFloat) && !(v instanceof PyBigInt);
  }
  function isStr(v) { return typeof v === "string"; }
  function isBool(v) { return typeof v === "boolean"; }
  // isinstance(v, int) and not isinstance(v, bool), for values that can pass
  // a range check; isinstance alone also accepts PyBigInt (see validateLimit).
  function isPyInt(v) { return typeof v === "number"; }

  // ------------------------------------------------------- code point utils
  function cps(s) { var out = []; for (var ch of s) out.push(ch.codePointAt(0)); return out; }
  function fromCps(list) {
    var out = "";
    for (var i = 0; i < list.length; i++) out += String.fromCodePoint(list[i]);
    return out;
  }
  // len(str): code points. Lone surrogates count 1, like Python.
  function pyLen(s) { var n = 0; for (var _ of s) n++; return n; } // eslint-disable-line no-unused-vars

  // ------------------------------------------------------------ tables
  function expansionMap(flat) {
    var m = new Map();
    for (var i = 0; i < flat.length;) {
      var cp = flat[i], n = flat[i + 1];
      m.set(cp, flat.slice(i + 2, i + 2 + n));
      i += 2 + n;
    }
    return m;
  }
  var NFD = expansionMap(T.nfd);
  var NFKD = expansionMap(T.nfkd);
  var FOLD = expansionMap(T.fold);
  var UPPER_CODE = expansionMap(T.upperCode);
  var WS = new Set(T.ws);
  var COMP = new Map();
  for (var ci = 0; ci < T.comp.length; ci += 3) COMP.set(T.comp[ci] * 0x110000 + T.comp[ci + 1], T.comp[ci + 2]);
  function rangeLookup(flat, stride) {
    var starts = [];
    for (var i = 0; i < flat.length; i += stride) starts.push(i);
    return function (cp) {
      var lo = 0, hi = starts.length - 1;
      while (lo <= hi) {
        var mid = (lo + hi) >> 1, at = starts[mid];
        if (cp < flat[at]) hi = mid - 1;
        else if (cp > flat[at + 1]) lo = mid + 1;
        else return stride === 3 ? flat[at + 2] : 1;
      }
      return 0;
    };
  }
  var ccc = rangeLookup(T.ccc, 3);
  var rejected = rangeLookup(T.reject, 2);

  // ------------------------------------------------- Unicode 13 normalization
  var SBase = 0xAC00, LBase = 0x1100, VBase = 0x1161, TBase = 0x11A7;
  var LCount = 19, VCount = 21, TCount = 28, NCount = VCount * TCount, SCount = LCount * NCount;

  function decompose(s, compat) {
    var out = [];
    for (var ch of s) {
      var cp = ch.codePointAt(0);
      var si = cp - SBase;
      if (si >= 0 && si < SCount) {
        out.push(LBase + Math.floor(si / NCount), VBase + Math.floor((si % NCount) / TCount));
        if (si % TCount) out.push(TBase + si % TCount);
        continue;
      }
      var m = (compat && NFKD.get(cp)) || NFD.get(cp);
      if (m) { for (var k = 0; k < m.length; k++) out.push(m[k]); } else out.push(cp);
    }
    // Canonical ordering: stable sort of each run of non-starters by ccc.
    for (var i = 0; i < out.length; i++) {
      if (ccc(out[i]) === 0) continue;
      var j = i;
      while (j < out.length && ccc(out[j]) !== 0) j++;
      if (j - i > 1) {
        var run = out.slice(i, j).map(function (c, idx) { return [ccc(c), idx, c]; });
        run.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
        for (var r = 0; r < run.length; r++) out[i + r] = run[r][2];
      }
      i = j - 1;
    }
    return out;
  }

  function compose(list) {
    if (!list.length) return list;
    var out = [], starterIdx = -1, lastCcc = -1;
    for (var i = 0; i < list.length; i++) {
      var c = list[i], cc = ccc(c);
      if (starterIdx >= 0) {
        var s = out[starterIdx];
        // Hangul LV and LV+T
        if (lastCcc === -1 || out.length - 1 === starterIdx) {
          var li = s - LBase, vi = c - VBase;
          if (li >= 0 && li < LCount && vi >= 0 && vi < VCount) {
            out[starterIdx] = SBase + (li * VCount + vi) * TCount; lastCcc = -1; continue;
          }
          var si = s - SBase, ti = c - TBase;
          if (si >= 0 && si < SCount && si % TCount === 0 && ti > 0 && ti < TCount) {
            out[starterIdx] = s + ti; lastCcc = -1; continue;
          }
        }
        var blocked = out.length - 1 !== starterIdx && (lastCcc === 0 || lastCcc >= cc);
        if (!blocked) {
          var p = COMP.get(s * 0x110000 + c);
          if (p !== undefined) { out[starterIdx] = p; continue; }
        }
      }
      if (cc === 0) { starterIdx = out.length; lastCcc = -1; } else lastCcc = cc;
      out.push(c);
    }
    return out;
  }

  function nfc(s) { return fromCps(compose(decompose(s, false))); }
  function nfkc(s) { return fromCps(compose(decompose(s, true))); }
  function nfd(s) { return fromCps(decompose(s, false)); }
  function nfkd(s) { return fromCps(decompose(s, true)); }

  // --------------------------------------------- str.split/strip/casefold/upper
  function pySplit(s) { // str.split() with no arguments
    var out = [], cur = "", inWord = false;
    for (var ch of s) {
      if (WS.has(ch.codePointAt(0))) { if (inWord) { out.push(cur); cur = ""; inWord = false; } }
      else { cur += ch; inWord = true; }
    }
    if (inWord) out.push(cur);
    return out;
  }
  function pyStrip(s) {
    var list = cps(s), a = 0, b = list.length;
    while (a < b && WS.has(list[a])) a++;
    while (b > a && WS.has(list[b - 1])) b--;
    return fromCps(list.slice(a, b));
  }
  function casefold(s) {
    var out = "";
    for (var ch of s) { var m = FOLD.get(ch.codePointAt(0)); out += m ? fromCps(m) : ch; }
    return out;
  }
  // code.upper() restricted to results that can still fullmatch [A-Z2-9]{6};
  // returns null as soon as any character's uppercase falls outside it.
  function upperForCode(s) {
    var out = "";
    for (var ch of s) {
      var cp = ch.codePointAt(0);
      if ((cp >= 0x41 && cp <= 0x5A) || (cp >= 0x32 && cp <= 0x39)) { out += ch; continue; }
      var m = UPPER_CODE.get(cp);
      if (!m) return null;
      out += fromCps(m);
    }
    return out;
  }

  // ----------------------------------------------------------- textutil.py
  var MAX_NAME_LEN = 24;
  function cleanText(value, maxLen) {
    if (!isStr(value)) return null;
    value = nfc(value);
    for (var ch of value) {
      var cp = ch.codePointAt(0);
      // rejected() already folds in the bidi controls and the isspace exemption
      if (rejected(cp)) return null;
    }
    var collapsed = pySplit(value).join(" ");
    if (!collapsed || pyLen(collapsed) > maxLen) return null;
    return collapsed;
  }
  function cleanName(value) { return cleanText(value, MAX_NAME_LEN); }
  function matchKey(value) { return pySplit(casefold(nfkc(value))).join(" "); }

  // ------------------------------------------------------------- errors.py
  var ERRORS = {
    bad_request: [400, "Malformed request."],
    invalid_json: [400, "Request body must be a JSON object."],
    length_required: [411, "Content-Length is required; chunked bodies are not accepted."],
    body_too_large: [413, "Request body exceeds the size limit."],
    unsupported_media_type: [415, "Content-Type must be application/json."],
    query_not_allowed: [400, "Query strings are not accepted on API routes; send credentials only in the Authorization header."],
    origin_forbidden: [403, "Cross-origin requests are not allowed."],
    host_forbidden: [403, "Host header is not an allowed server origin."],
    not_found: [404, "Not found."],
    method_not_allowed: [405, "Method not allowed."],
    rate_limited: [429, "Too many requests; retry later."],
    too_many_rooms: [503, "Room capacity reached; retry later."],
    too_many_streams: [429, "Too many open event streams."],
    server_busy: [503, "Server is busy."],
    internal_error: [500, "Internal error."],
    room_not_found: [404, "Room not found or expired."],
    room_expired: [410, "Room expired."],
    unauthorized: [401, "Missing or invalid credentials."],
    token_expired: [401, "Credentials expired."],
    forbidden: [403, "This role may not perform that action."],
    invalid_config: [400, "Invalid room configuration."],
    invalid_name: [400, "Name must be 1-24 visible characters."],
    name_taken: [409, "That name is already used in this room."],
    room_full: [409, "Room is full."],
    room_closed: [409, "Room is closed."],
    game_in_progress: [409, "A game is in progress; new players may join between games."],
    invalid_action_id: [400, "actionId must be 1-64 characters of [A-Za-z0-9._:-]."],
    invalid_action: [400, "Action must contain a valid type and optional string roundId."],
    unknown_action: [400, "Unknown action type."],
    invalid_payload: [400, "Invalid action payload."],
    round_required: [400, "This action requires the current roundId."],
    stale_round: [409, "roundId does not match the current round."],
    invalid_phase: [409, "Action is not valid in the current phase."],
    paused: [409, "Game is paused."],
    wrong_mode: [409, "Action belongs to a different mode."],
    unknown_mode: [400, "Unknown mode."],
    mode_unimplemented: [409, "Mode is not implemented in this server build."],
    mode_not_selected: [409, "Select a mode first."],
    content_missing: [409, "Content pack for this mode is missing."],
    content_invalid: [409, "Content pack for this mode failed validation."],
    content_insufficient: [409, "Content pack has too few items for this game."],
    content_exhausted: [409, "No more content items remain for another round."],
    not_enough_players: [409, "Not enough players for this room or mode."],
    too_many_players: [409, "Too many players for this room or mode."],
    unknown_player: [400, "Unknown player."],
    already_revealed: [409, "Already revealed."],
    no_pending_guess: [409, "That player has no pending guess."],
    already_awarded: [409, "Points for this round were already awarded."],
    buzzer_closed: [409, "Buzzer is not open."],
    buzzer_taken: [409, "Another player already won the buzzer."],
    unknown_answer: [400, "Unknown answer."],
    no_match: [409, "No unrevealed answer matches that text."],
    max_strikes: [409, "Maximum strikes reached."],
    steal_not_available: [409, "A steal is only available after the maximum strikes."],
    no_control: [409, "No player is in control; specify playerId."],
    round_awarded: [409, "This round was already awarded."]
  };
  function PartyError(code) {
    if (!hasOwn.call(ERRORS, code)) code = "internal_error";
    this.code = code;
    this.status = ERRORS[code][0];
  }
  function statusFor(code) { return (hasOwn.call(ERRORS, code) ? ERRORS[code] : ERRORS.internal_error)[0]; }
  function errorBody(code) {
    if (!hasOwn.call(ERRORS, code)) code = "internal_error";
    return { code: code, message: ERRORS[code][1] };
  }

  // ------------------------------------------------- Python-compatible JSON
  // json.loads(str) as CPython 3.9's C scanner: ASCII digits only, strict
  // strings (no raw control characters), no leading BOM, duplicate keys keep
  // the first position with the last value, ints vs floats distinguished.
  // allowConstants: NaN/Infinity/-Infinity (request bodies: yes; content
  // packs: no, parse_constant rejects them). maxDepth stands in for CPython's
  // stack-dependent RecursionError (declared difference, see L01A report).
  function JsonError(msg) { this.message = msg; }
  function parseJson(text, opts) {
    opts = opts || {};
    var allowConstants = opts.allowConstants !== false;
    var maxDepth = opts.maxDepth || 900;
    var i = 0, n = text.length;
    if (n && text.charCodeAt(0) === 0xFEFF) throw new JsonError("Unexpected UTF-8 BOM");
    function ws() {
      while (i < n) {
        var c = text.charCodeAt(i);
        if (c === 0x20 || c === 0x09 || c === 0x0A || c === 0x0D) i++; else break;
      }
    }
    function fail(msg) { throw new JsonError(msg + " at " + i); }
    function isDigit(c) { return c >= 0x30 && c <= 0x39; }
    function hex4(at) {
      if (at + 4 > n) fail("Invalid \\uXXXX escape");
      var v = 0;
      for (var k = 0; k < 4; k++) {
        var c = text.charCodeAt(at + k), d;
        if (c >= 0x30 && c <= 0x39) d = c - 0x30;
        else if (c >= 0x61 && c <= 0x66) d = c - 0x57;
        else if (c >= 0x41 && c <= 0x46) d = c - 0x37;
        else fail("Invalid \\uXXXX escape");
        v = v * 16 + d;
      }
      return v;
    }
    function str() {
      i++; // opening quote
      var out = "", start = i;
      for (;;) {
        if (i >= n) fail("Unterminated string");
        var c = text.charCodeAt(i);
        if (c === 0x22) { out += text.slice(start, i); i++; return out; }
        if (c < 0x20) fail("Invalid control character");
        if (c !== 0x5C) { i++; continue; }
        out += text.slice(start, i);
        if (i + 1 >= n) fail("Unterminated string");
        var e = text.charAt(i + 1);
        if (e === "u") {
          var u = hex4(i + 2);
          i += 6;
          if (u >= 0xD800 && u <= 0xDBFF && text.charCodeAt(i) === 0x5C && text.charAt(i + 1) === "u") {
            var u2 = hex4(i + 2);
            if (u2 >= 0xDC00 && u2 <= 0xDFFF) { out += String.fromCharCode(u, u2); i += 6; start = i; continue; }
          }
          out += String.fromCharCode(u);
        } else {
          var map = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
          if (!hasOwn.call(map, e)) fail("Invalid \\escape");
          out += map[e];
          i += 2;
        }
        start = i;
      }
    }
    function num() {
      var s = i;
      if (text.charCodeAt(i) === 0x2D) i++;
      if (i < n && text.charCodeAt(i) === 0x30) i++;
      else if (i < n && text.charCodeAt(i) >= 0x31 && text.charCodeAt(i) <= 0x39) { while (i < n && isDigit(text.charCodeAt(i))) i++; }
      else { i = s; fail("Expecting value"); }
      var isFloat = false;
      if (i + 1 < n && text.charCodeAt(i) === 0x2E && isDigit(text.charCodeAt(i + 1))) {
        isFloat = true; i += 2;
        while (i < n && isDigit(text.charCodeAt(i))) i++;
      }
      if (i < n && (text.charCodeAt(i) === 0x65 || text.charCodeAt(i) === 0x45)) {
        var j = i + 1;
        if (j < n && (text.charCodeAt(j) === 0x2B || text.charCodeAt(j) === 0x2D)) j++;
        if (j < n && isDigit(text.charCodeAt(j))) {
          while (j < n && isDigit(text.charCodeAt(j))) j++;
          isFloat = true; i = j;
        }
      }
      var lit = text.slice(s, i);
      if (isFloat) return new PyFloat(Number(lit));
      var v = Number(lit);
      if (!Number.isSafeInteger(v)) return new PyBigInt(BigInt(lit).toString());
      return v === 0 ? 0 : v; // Python ints have no -0
    }
    function constant(name, value) {
      if (text.startsWith(name, i)) {
        i += name.length;
        if (!allowConstants) throw new JsonError("non-finite number");
        return new PyFloat(value);
      }
      return undefined;
    }
    function value(depth) {
      if (i >= n) fail("Expecting value");
      var c = text.charAt(i);
      if (c === '"') return str();
      if (c === "{" || c === "[") {
        if (depth >= maxDepth) throw new JsonError("maximum recursion depth exceeded");
        return c === "{" ? object(depth + 1) : array(depth + 1);
      }
      if (c === "n" && text.startsWith("null", i)) { i += 4; return null; }
      if (c === "t" && text.startsWith("true", i)) { i += 4; return true; }
      if (c === "f" && text.startsWith("false", i)) { i += 5; return false; }
      var k;
      if (c === "N" && (k = constant("NaN", NaN)) !== undefined) return k;
      if (c === "I" && (k = constant("Infinity", Infinity)) !== undefined) return k;
      if (c === "-" && text.startsWith("-Infinity", i)) return constant("-Infinity", -Infinity);
      if (c === "-" || isDigit(text.charCodeAt(i))) return num();
      fail("Expecting value");
    }
    function object(depth) {
      i++;
      var o = obj();
      ws();
      if (text.charAt(i) === "}") { i++; return o; }
      for (;;) {
        if (text.charAt(i) !== '"') fail("Expecting property name enclosed in double quotes");
        var key = str();
        ws();
        if (text.charAt(i) !== ":") fail("Expecting ':' delimiter");
        i++; ws();
        Object.defineProperty(o, key, { value: value(depth), writable: true, enumerable: true, configurable: true });
        ws();
        var c = text.charAt(i);
        if (c === "}") { i++; return o; }
        if (c !== ",") fail("Expecting ',' delimiter");
        i++; ws();
      }
    }
    function array(depth) {
      i++;
      var a = [];
      ws();
      if (text.charAt(i) === "]") { i++; return a; }
      for (;;) {
        ws();
        a.push(value(depth));
        ws();
        var c = text.charAt(i);
        if (c === "]") { i++; return a; }
        if (c !== ",") fail("Expecting ',' delimiter");
        i++;
      }
    }
    ws();
    var result = value(0);
    ws();
    if (i !== n) fail("Extra data");
    return result;
  }

  // Strict UTF-8 decode (Python codec "utf-8", or "utf-8-sig" with stripBom).
  // Returns null on any invalid sequence, surrogate, overlong or > U+10FFFF.
  function decodeUtf8(bytes, stripBom) {
    var i = 0, n = bytes.length, out = "";
    if (stripBom && n >= 3 && bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) i = 3;
    while (i < n) {
      var b = bytes[i], cp, need, min;
      if (b < 0x80) { out += String.fromCharCode(b); i++; continue; }
      if (b >= 0xC2 && b <= 0xDF) { cp = b & 0x1F; need = 1; min = 0x80; }
      else if (b >= 0xE0 && b <= 0xEF) { cp = b & 0x0F; need = 2; min = 0x800; }
      else if (b >= 0xF0 && b <= 0xF4) { cp = b & 0x07; need = 3; min = 0x10000; }
      else return null;
      if (i + need >= n) return null; // truncated sequence
      for (var k = 1; k <= need; k++) {
        var c = bytes[i + k];
        if (c === undefined || (c & 0xC0) !== 0x80) return null;
        cp = (cp << 6) | (c & 0x3F);
      }
      if (cp < min || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return null;
      out += String.fromCodePoint(cp);
      i += need + 1;
    }
    return out;
  }

  // httpd._read_json after the transport has checked length and media type:
  // strict UTF-8 (no BOM stripping), json.loads, and the body must be an object.
  function parseRequestBody(bytes) {
    var text = decodeUtf8(bytes, false);
    if (text === null) throw new PartyError("invalid_json");
    var body;
    try { body = parseJson(text, { allowConstants: true }); }
    catch (e) {
      if (e instanceof JsonError) throw new PartyError("invalid_json");
      throw e;
    }
    if (!isPyDict(body)) throw new PartyError("invalid_json");
    return body;
  }

  // ------------------------------------------------------------ content.py
  var SCHEMA_VERSION = 1, MAX_FILE_BYTES = 2 * 1024 * 1024, MAX_ITEMS = 2000, MAX_TEXT = 500, MAX_ID = 64;
  function isText(value, maxLen) {
    if (maxLen === undefined) maxLen = MAX_TEXT;
    return isStr(value) && pyStrip(value) !== "" && pyLen(value) <= maxLen;
  }
  function isInt(value, lo, hi) { return isPyInt(value) && lo <= value && value <= hi; }
  function ContentStatus(mode, status, pack, issues) {
    this.mode = mode; this.status = status; this.pack = pack || null; this.issues = (issues || []).slice();
  }
  ContentStatus.prototype.ok = function () { return this.status === "ok"; };

  function parsePack(mode, data, validateItem, validateItems) {
    if (!isPyDict(data)) return [null, ["root: must be an object"]];
    var issues = [];
    if (!isInt(get(data, "schemaVersion"), SCHEMA_VERSION, SCHEMA_VERSION)) issues.push("schemaVersion: must be 1");
    if (get(data, "mode") !== mode) issues.push("mode: must equal the file's mode id");
    if (!isText(get(data, "title"))) issues.push("title: required non-empty string");
    if (!isText(get(data, "licenseNote"), 2000)) issues.push("licenseNote: required non-empty string");
    var items = get(data, "items");
    var normalized = [];
    if (!Array.isArray(items) || !items.length) issues.push("items: required non-empty array");
    else if (items.length > MAX_ITEMS) issues.push("items: too many items");
    else {
      var seen = new Set();
      for (var index = 0; index < items.length; index++) {
        var path = "items[" + index + "]";
        var before = issues.length;
        var out = validateItem(items[index], path, issues);
        if (issues.length === before && out !== null) {
          if (seen.has(out.id)) issues.push(path + ".id: duplicate id");
          else { seen.add(out.id); normalized.push(out); }
        }
      }
      if (!issues.length && validateItems) validateItems(normalized, issues);
    }
    if (issues.length) return [null, issues];
    return [{ mode: mode, title: data.title, licenseNote: data.licenseNote, items: normalized }, []];
  }

  // bytes: Uint8Array/array of the pack file, or null when the file is absent.
  function loadMode(mode, bytes, validateItem, validateItems) {
    if (bytes === null || bytes === undefined) return new ContentStatus(mode, "content_missing");
    if (bytes.length > MAX_FILE_BYTES) return new ContentStatus(mode, "content_invalid", null, ["file: exceeds size limit"]);
    var text = decodeUtf8(bytes, true);
    if (text === null) return new ContentStatus(mode, "content_invalid", null, ["file: not UTF-8"]);
    var data;
    try { data = parseJson(text, { allowConstants: false }); }
    catch (e) {
      if (e instanceof JsonError) return new ContentStatus(mode, "content_invalid", null, ["file: not valid JSON"]);
      throw e;
    }
    var r = parsePack(mode, data, validateItem, validateItems);
    if (r[0] === null) return new ContentStatus(mode, "content_invalid", null, r[1]);
    return new ContentStatus(mode, "ok", r[0]);
  }

  // ------------------------------------------------------------- helpers
  function deepcopy(v) {
    if (v === null || typeof v !== "object") return v;
    if (v instanceof PyFloat) return new PyFloat(v.value);
    if (v instanceof PyBigInt) return new PyBigInt(v.digits);
    if (Array.isArray(v)) return v.map(deepcopy);
    var o = obj();
    for (var k of Object.keys(v)) o[k] = deepcopy(v[k]);
    return o;
  }
  function deepFreeze(v) {
    if (v !== null && typeof v === "object" && !Object.isFrozen(v)) {
      Object.freeze(v);
      for (var k of Object.keys(v)) deepFreeze(v[k]);
    }
    return v;
  }
  function inList(list, v) { return isStr(v) && list.indexOf(v) >= 0; }

  // ---------------------------------------------------------- MT19937 shuffle
  var MT_N = 624, MT_M = 397;
  function MT(seed) { // seed: BigInt; CPython random.seed(int) == init_by_array(abs(seed))
    var big = seed < 0n ? -seed : seed, key = [];
    do { key.push(Number(big & 0xffffffffn)); big >>= 32n; } while (big > 0n);
    var mt = new Uint32Array(MT_N);
    mt[0] = 19650218;
    for (var a = 1; a < MT_N; a++) {
      var p = mt[a - 1] ^ (mt[a - 1] >>> 30);
      mt[a] = (Math.imul(1812433253, p) + a) >>> 0;
    }
    var i = 1, j = 0, k = Math.max(MT_N, key.length);
    for (; k; k--) {
      var q = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(q, 1664525)) + key[j] + j) >>> 0;
      i++; j++;
      if (i >= MT_N) { mt[0] = mt[MT_N - 1]; i = 1; }
      if (j >= key.length) j = 0;
    }
    for (k = MT_N - 1; k; k--) {
      var r = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(r, 1566083941)) - i) >>> 0;
      i++;
      if (i >= MT_N) { mt[0] = mt[MT_N - 1]; i = 1; }
    }
    mt[0] = 0x80000000;
    this.mt = mt; this.idx = MT_N;
  }
  MT.prototype.u32 = function () {
    var mt = this.mt, y, kk;
    if (this.idx >= MT_N) {
      for (kk = 0; kk < MT_N; kk++) {
        y = (mt[kk] & 0x80000000) | (mt[(kk + 1) % MT_N] & 0x7fffffff);
        mt[kk] = mt[(kk + MT_M) % MT_N] ^ (y >>> 1) ^ ((y & 1) ? 0x9908b0df : 0);
      }
      this.idx = 0;
    }
    y = mt[this.idx++];
    y ^= y >>> 11; y ^= (y << 7) & 0x9d2c5680; y ^= (y << 15) & 0xefc60000; y ^= y >>> 18;
    return y >>> 0;
  };
  MT.prototype.randbelow = function (n) { // n <= 2^31
    var k = 32 - Math.clz32(n), r = this.u32() >>> (32 - k);
    while (r >= n) r = this.u32() >>> (32 - k);
    return r;
  };
  function shuffleInPlace(seed, x) { // random.Random(seed).shuffle(x)
    var rng = new MT(BigInt(seed));
    for (var i = x.length - 1; i > 0; i--) {
      var j = rng.randbelow(i + 1), t = x[i]; x[i] = x[j]; x[j] = t;
    }
    return x;
  }

  // -------------------------------------------------------- modes/who_am_i
  var WhoAmI = (function () {
    var MODE_ID = "who_am_i", DEFAULT_POINTS = 1, MAX_AWARD_POINTS = 100, MAX_GUESS_LEN = 80;
    function validateItem(item, path, issues) {
      if (!isPyDict(item)) { issues.push(path + ": must be an object"); return null; }
      var before = issues.length;
      if (!isText(get(item, "id"), MAX_ID)) issues.push(path + ".id: required non-empty string (max 64)");
      if (!isText(get(item, "label"), 120)) issues.push(path + ".label: required non-empty string (max 120)");
      var ar = get(item, "ar");
      if (!isNone(ar) && !isText(ar, 120)) issues.push(path + ".ar: must be a non-empty string when present");
      var difficulty = get(item, "difficulty");
      if (!(isText(difficulty, 32) || isInt(difficulty, 0, 100))) issues.push(path + ".difficulty: required short string or integer 0-100");
      if (issues.length !== before) return null;
      var out = { id: item.id, label: item.label, difficulty: difficulty };
      if (!isNone(ar)) out.ar = ar;
      return out;
    }
    function validateItems(items, issues) {
      var owner = new Map();
      items.forEach(function (item, index) {
        var keys = [["label", matchKey(item.label)]];
        if (has(item, "ar")) keys.push(["ar", matchKey(item.ar)]);
        keys.forEach(function (fk) {
          var first = owner.has(fk[1]) ? owner.get(fk[1]) : null;
          if (first !== null && first !== index) {
            issues.push("items[" + index + "]." + fk[0] + ": collides with display text of items[" + first + "] (case/width-insensitive)");
          } else if (first === null) owner.set(fk[1], index);
        });
      });
    }
    function startRound(state, roundNo) {
      var order = state.order;
      var drawn = state.deck.slice(0, order.length);
      state.deck = state.deck.slice(order.length);
      var a = obj();
      order.forEach(function (p, i) { if (i < drawn.length) a[p] = drawn[i]; });
      state.assignments = a;
      state.roundNo = roundNo;
      state.roundId = "g" + state.gameNo + "r" + roundNo;
      state.roundPhase = "asking";
      state.revealed = []; state.ready = []; state.guesses = obj(); state.solved = obj();
    }
    function initialize(config, players, content) {
      var order = players.map(function (p) { return p.id; });
      if (order.length < 2) return [null, "not_enough_players"];
      var items = deepcopy(content.items);
      if (items.length < order.length) return [null, "content_insufficient"];
      shuffleInPlace(config.seed, items);
      var scores = obj();
      players.forEach(function (p) { scores[p.id] = p.score; });
      var state = { mode: MODE_ID, title: content.title, gameNo: config.gameNo, order: order, deck: items,
        awards: [], scores: scores, points: get(config, "whoAmIPoints", DEFAULT_POINTS), ended: false };
      startRound(state, 1);
      return [state, null];
    }
    function inOrder(state, pid) { return inList(state.order, pid); }
    function ordered(state, pids) { return state.order.filter(function (p) { return pids.indexOf(p) >= 0; }); }
    function reduce(state, actor, action, seq) {
      var kind = get(action, "type");
      var payload = get(action, "payload");
      if (!pyTruthy(payload)) payload = obj(); // `action.get("payload") or {}`
      var role = get(actor, "role"), pid = get(actor, "playerId"), roundId = get(action, "roundId");
      if (!isNone(roundId) && roundId !== state.roundId) return [state, "stale_round"];
      if (!isPyDict(payload)) return [state, "invalid_payload"];
      var s;
      if (kind === "game.end") {
        if (role !== "operator") return [state, "forbidden"];
        if (state.ended) return [state, "invalid_phase"];
        s = deepcopy(state); s.ended = true; s.revealed = s.order.slice(); s.roundPhase = "revealed";
        return [s, null];
      }
      if (state.ended) return [state, "invalid_phase"];
      if (kind === "whoami.ready") {
        if (role !== "player" || !inOrder(state, pid)) return [state, "forbidden"];
        var ready = get(payload, "ready", true);
        if (!isBool(ready)) return [state, "invalid_payload"];
        if (state.roundPhase !== "asking") return [state, "invalid_phase"];
        s = deepcopy(state);
        var current = s.ready.filter(function (p) { return p !== pid; });
        if (ready) current.push(pid);
        s.ready = ordered(s, current);
        return [s, null];
      }
      if (kind === "whoami.guess") {
        if (role !== "player" || !inOrder(state, pid)) return [state, "forbidden"];
        var text = cleanText(get(payload, "text"), MAX_GUESS_LEN);
        if (text === null) return [state, "invalid_payload"];
        if (state.revealed.indexOf(pid) >= 0) return [state, "already_revealed"];
        s = deepcopy(state);
        s.guesses[pid] = { text: text, seq: seq };
        return [s, null];
      }
      var target;
      if (kind === "whoami.dismiss") {
        if (role !== "operator") return [state, "forbidden"];
        target = get(payload, "playerId");
        if (!inOrder(state, target)) return [state, "unknown_player"];
        if (!has(state.guesses, target)) return [state, "no_pending_guess"];
        s = deepcopy(state);
        delete s.guesses[target];
        return [s, null];
      }
      if (kind === "whoami.reveal") {
        if (role !== "operator") return [state, "forbidden"];
        target = get(payload, "playerId");
        s = deepcopy(state);
        if (isNone(target)) {
          if (s.revealed.length === s.order.length) return [state, "already_revealed"];
          s.revealed = s.order.slice();
        } else {
          if (!inOrder(s, target)) return [state, "unknown_player"];
          if (s.revealed.indexOf(target) >= 0) return [state, "already_revealed"];
          s.revealed = ordered(s, s.revealed.concat([target]));
        }
        if (s.revealed.length === s.order.length) s.roundPhase = "revealed";
        return [s, null];
      }
      if (kind === "whoami.award") {
        if (role !== "operator") return [state, "forbidden"];
        target = get(payload, "playerId");
        if (!inOrder(state, target)) return [state, "unknown_player"];
        var points = get(payload, "points", state.points);
        if (!isInt(points, 0, MAX_AWARD_POINTS)) return [state, "invalid_payload"];
        var key = state.roundId + ":" + target;
        if (state.awards.indexOf(key) >= 0) return [state, "already_awarded"];
        s = deepcopy(state);
        s.awards.push(key);
        s.scores[target] = get(s.scores, target, 0) + points;
        s.solved[target] = { points: points, seq: seq };
        if (s.revealed.indexOf(target) < 0) s.revealed = ordered(s, s.revealed.concat([target]));
        if (s.revealed.length === s.order.length) s.roundPhase = "revealed";
        return [s, null];
      }
      if (kind === "round.next") {
        if (role !== "operator") return [state, "forbidden"];
        if (state.deck.length < state.order.length) return [state, "content_exhausted"];
        s = deepcopy(state);
        startRound(s, s.roundNo + 1);
        return [s, null];
      }
      return [state, "unknown_action"];
    }
    function publicItem(item) {
      var out = { label: item.label };
      if (has(item, "ar")) out.ar = item.ar;
      return out;
    }
    function project(state, viewer) {
      var role = get(viewer, "role");
      var me = role === "player" ? get(viewer, "playerId") : null;
      if (!isNone(me) && state.order.indexOf(me) < 0) { role = "display"; me = null; }
      var order = state.order;
      var revealed = state.ended ? order.slice() : state.revealed;
      var isRev = function (p) { return revealed.indexOf(p) >= 0; };
      var A = state.assignments;
      var view = {
        title: state.title,
        round: state.roundNo,
        roundPhase: state.ended ? "ended" : state.roundPhase,
        order: order.slice(),
        ready: state.ready.slice(),
        revealed: order.filter(isRev),
        solved: order.filter(function (p) { return has(state.solved, p); }),
        pendingGuesses: order.filter(function (p) { return has(state.guesses, p); })
      };
      var m = obj();
      if (role === "operator") {
        var g = obj();
        order.forEach(function (p) { if (has(state.guesses, p)) g[p] = state.guesses[p].text; });
        view.guesses = g;
        order.forEach(function (p) { m[p] = deepcopy(A[p]); });
        view.assignments = m;
        view.roundsRemaining = Math.floor(state.deck.length / Math.max(1, order.length));
        view.awardPoints = state.points;
      } else if (role === "player") {
        order.forEach(function (p) { if (p !== me) m[p] = publicItem(A[p]); });
        view.assignments = m;
        var you = {
          revealed: isRev(me),
          ready: state.ready.indexOf(me) >= 0,
          solved: has(state.solved, me),
          guess: has(state.guesses, me) ? state.guesses[me].text : null
        };
        if (isRev(me)) you.assignment = publicItem(A[me]);
        view.you = you;
      } else {
        order.forEach(function (p) { if (isRev(p)) m[p] = publicItem(A[p]); });
        view.assignments = m;
      }
      return view;
    }
    return { MODE_ID: MODE_ID, TITLE: "Who Am I", ACTION_PREFIX: "whoami.", MIN_PLAYERS: 2,
      validateItem: validateItem, validateItems: validateItems, initialize: initialize, reduce: reduce, project: project };
  })();

  // ------------------------------------------------------------ modes/feud
  var Feud = (function () {
    var MODE_ID = "feud", PREFIX = "feud.", HOUSE_LABEL = "House answers", MAX_STRIKES = 3,
      MAX_ANSWERS = 20, MAX_ALIASES = 50, MAX_POINTS = 1000, MAX_MATCH_TEXT = 120;
    function validateItem(item, path, issues) {
      if (!isPyDict(item)) { issues.push(path + ": must be an object"); return null; }
      var before = issues.length;
      if (!isText(get(item, "id"), MAX_ID)) issues.push(path + ".id: required non-empty string (max 64)");
      if (!isText(get(item, "question"))) issues.push(path + ".question: required non-empty string");
      if (get(item, "label") !== HOUSE_LABEL) issues.push(path + '.label: must be exactly "House answers"');
      var answers = get(item, "answers"), outAnswers = [];
      if (!Array.isArray(answers) || !answers.length || answers.length > MAX_ANSWERS) {
        issues.push(path + ".answers: required array of 1-20 answers");
      } else {
        var seen = new Set();
        answers.forEach(function (answer, index) {
          var apath = path + ".answers[" + index + "]";
          if (!isPyDict(answer)) { issues.push(apath + ": must be an object"); return; }
          var abefore = issues.length;
          var aid = get(answer, "id");
          if (!isText(aid, MAX_ID)) issues.push(apath + ".id: required non-empty string (max 64)");
          else if (seen.has(aid)) issues.push(apath + ".id: duplicate answer id");
          if (!isText(get(answer, "text"), 200)) issues.push(apath + ".text: required non-empty string");
          var aliases = get(answer, "aliases");
          if (!Array.isArray(aliases) || aliases.length > MAX_ALIASES) issues.push(apath + ".aliases: required array (max 50)");
          else aliases.forEach(function (alias, j) {
            if (!isText(alias, 200)) issues.push(apath + ".aliases[" + j + "]: must be a non-empty string");
          });
          if (!isInt(get(answer, "points"), 0, MAX_POINTS)) issues.push(apath + ".points: required integer 0-1000");
          if (issues.length === abefore) {
            seen.add(aid);
            outAnswers.push({ id: aid, text: answer.text, aliases: aliases.slice(), points: answer.points });
          }
        });
      }
      if (issues.length !== before) return null;
      return { id: item.id, question: item.question, label: HOUSE_LABEL, answers: outAnswers };
    }
    function startBoard(state, index) {
      state.boardIndex = index;
      state.roundId = "g" + state.gameNo + "b" + (index + 1);
      state.roundPhase = "closed";
      state.revealed = [];
      state.strikes = 0;
      state.buzzer = { open: false, winner: null, winnerSeq: null };
      state.control = null; state.stealFrom = null; state.awardedTo = null; state.awardedPoints = null;
    }
    function initialize(config, players, content) {
      if (players.length < 1) return [null, "not_enough_players"];
      var boards = deepcopy(content.items);
      if (!boards.length) return [null, "content_insufficient"];
      var scores = obj();
      players.forEach(function (p) { scores[p.id] = p.score; });
      var state = { mode: MODE_ID, title: content.title, gameNo: config.gameNo,
        order: players.map(function (p) { return p.id; }), boards: boards, awards: [], scores: scores, ended: false };
      startBoard(state, 0);
      return [state, null];
    }
    function board(state) { return state.boards[state.boardIndex]; }
    function revealedPoints(state) {
      var sum = 0;
      board(state).answers.forEach(function (a) { if (state.revealed.indexOf(a.id) >= 0) sum += a.points; });
      return sum;
    }
    function findAnswer(state, payload) {
      var answers = board(state).answers;
      var answerId = get(payload, "answerId");
      var k;
      if (!isNone(answerId)) {
        for (k = 0; k < answers.length; k++) if (answers[k].id === answerId) return [answers[k], null];
        return [null, "unknown_answer"];
      }
      var text = cleanText(get(payload, "text"), MAX_MATCH_TEXT);
      if (text === null) return [null, "invalid_payload"];
      var key = matchKey(text), matched = null;
      for (k = 0; k < answers.length; k++) {
        var a = answers[k];
        var keys = [matchKey(a.text)].concat(a.aliases.map(matchKey));
        if (keys.indexOf(key) >= 0) {
          if (state.revealed.indexOf(a.id) < 0) return [a, null];
          matched = matched || a;
        }
      }
      if (matched !== null) return [matched, null];
      return [null, "no_match"];
    }
    function reduce(state, actor, action, seq) {
      var kind = get(action, "type");
      var payload = get(action, "payload");
      if (!pyTruthy(payload)) payload = obj(); // `action.get("payload") or {}`
      var role = get(actor, "role"), pid = get(actor, "playerId"), roundId = get(action, "roundId");
      if (!isNone(roundId) && roundId !== state.roundId) return [state, "stale_round"];
      if (!isPyDict(payload)) return [state, "invalid_payload"];
      var s;
      if (kind === "game.end") {
        if (role !== "operator") return [state, "forbidden"];
        if (state.ended) return [state, "invalid_phase"];
        s = deepcopy(state); s.ended = true; s.buzzer.open = false;
        return [s, null];
      }
      if (state.ended) return [state, "invalid_phase"];
      if (kind === "feud.buzz") {
        if (role !== "player" || !inList(state.order, pid)) return [state, "forbidden"];
        if (state.buzzer.winner !== null) return [state, "buzzer_taken"];
        if (state.roundPhase !== "buzzer_open" || !state.buzzer.open) return [state, "buzzer_closed"];
        s = deepcopy(state);
        s.buzzer = { open: false, winner: pid, winnerSeq: seq };
        s.control = pid;
        s.roundPhase = "answering";
        return [s, null];
      }
      if (!kind || !isStr(kind)) return [state, "unknown_action"];
      if (kind.startsWith(PREFIX) || kind === "round.next") {
        if (role !== "operator") return [state, "forbidden"];
      }
      var target;
      if (kind === "feud.open_buzzer") {
        if (state.buzzer.winner !== null) return [state, "buzzer_taken"];
        if (state.roundPhase !== "closed") return [state, "invalid_phase"];
        s = deepcopy(state); s.buzzer.open = true; s.roundPhase = "buzzer_open";
        return [s, null];
      }
      if (kind === "feud.reset_buzzer") {
        if (state.roundPhase === "awarded") return [state, "round_awarded"];
        s = deepcopy(state);
        s.buzzer = { open: false, winner: null, winnerSeq: null };
        s.control = null; s.stealFrom = null; s.roundPhase = "closed";
        return [s, null];
      }
      if (kind === "feud.reveal") {
        var r = findAnswer(state, payload);
        if (r[1]) return [state, r[1]];
        var answer = r[0];
        if (state.revealed.indexOf(answer.id) >= 0) return [state, "already_revealed"];
        s = deepcopy(state);
        var want = s.revealed.concat([answer.id]);
        s.revealed = board(s).answers.filter(function (a) { return want.indexOf(a.id) >= 0; }).map(function (a) { return a.id; });
        return [s, null];
      }
      if (kind === "feud.strike") {
        if (state.roundPhase !== "answering" && state.roundPhase !== "steal") return [state, "invalid_phase"];
        if (state.strikes >= MAX_STRIKES) return [state, "max_strikes"];
        s = deepcopy(state); s.strikes += 1;
        return [s, null];
      }
      if (kind === "feud.steal") {
        target = get(payload, "playerId");
        if (!inList(state.order, target)) return [state, "unknown_player"];
        if (state.roundPhase !== "answering") return [state, "invalid_phase"];
        if (state.strikes < MAX_STRIKES) return [state, "steal_not_available"];
        if (target === state.control) return [state, "invalid_payload"];
        s = deepcopy(state);
        s.stealFrom = s.control; s.control = target; s.roundPhase = "steal";
        return [s, null];
      }
      if (kind === "feud.award") {
        if (state.roundPhase === "awarded") return [state, "round_awarded"];
        target = get(payload, "playerId", state.control);
        if (isNone(target)) return [state, "no_control"];
        if (!inList(state.order, target)) return [state, "unknown_player"];
        var key = state.roundId + ":" + board(state).id;
        if (state.awards.indexOf(key) >= 0) return [state, "already_awarded"];
        s = deepcopy(state);
        var points = revealedPoints(s);
        s.awards.push(key);
        s.scores[target] = get(s.scores, target, 0) + points;
        s.awardedTo = target; s.awardedPoints = points;
        s.buzzer.open = false; s.roundPhase = "awarded";
        return [s, null];
      }
      if (kind === "round.next") {
        if (state.boardIndex + 1 >= state.boards.length) return [state, "content_exhausted"];
        s = deepcopy(state);
        startBoard(s, s.boardIndex + 1);
        return [s, null];
      }
      return [state, "unknown_action"];
    }
    function project(state, viewer) {
      var b = board(state);
      var isRev = function (id) { return state.revealed.indexOf(id) >= 0; };
      var slots = b.answers.map(function (a, index) {
        return isRev(a.id) ? { index: index, revealed: true, id: a.id, text: a.text, points: a.points }
          : { index: index, revealed: false };
      });
      var view = {
        title: state.title, label: HOUSE_LABEL, answerSource: "house",
        board: state.boardIndex + 1, boardCount: state.boards.length, question: b.question,
        roundPhase: state.ended ? "ended" : state.roundPhase,
        slots: slots, strikes: state.strikes, maxStrikes: MAX_STRIKES,
        roundPoints: state.awardedTo !== null ? state.awardedPoints : revealedPoints(state),
        buzzer: { open: state.buzzer.open, winner: state.buzzer.winner },
        control: state.control, stealFrom: state.stealFrom,
        awardedTo: state.awardedTo, awardedPoints: state.awardedPoints
      };
      if (get(viewer, "role") === "operator") {
        view.boardId = b.id;
        view.boardsRemaining = state.boards.length - state.boardIndex - 1;
        view.answers = b.answers.map(function (a) {
          return { id: a.id, text: a.text, aliases: a.aliases.slice(), points: a.points, revealed: isRev(a.id) };
        });
      }
      return view;
    }
    return { MODE_ID: MODE_ID, TITLE: "Feud (House Answers)", ACTION_PREFIX: PREFIX, MIN_PLAYERS: 1,
      MAX_STRIKES: MAX_STRIKES, validateItem: validateItem, validateItems: null,
      initialize: initialize, reduce: reduce, project: project };
  })();

  // ---------------------------------------------------------- modes registry
  function ModeSpec(id, title, module, prefix) {
    this.id = id; this.title = title; this.module = module || null; this.implemented = !!module; this.actionPrefix = prefix;
  }
  var REGISTRY = [
    new ModeSpec(WhoAmI.MODE_ID, WhoAmI.TITLE, WhoAmI, WhoAmI.ACTION_PREFIX),
    new ModeSpec(Feud.MODE_ID, Feud.TITLE, Feud, Feud.ACTION_PREFIX),
    new ModeSpec("impostor", "Impostor", null, "impostor."),
    new ModeSpec("draw_guess", "Draw & Guess", null, "draw_guess."),
    new ModeSpec("drawing_chain", "Drawing Chain", null, "drawing_chain."),
    new ModeSpec("bluff", "Bluff", null, "bluff."),
    new ModeSpec("werewolf", "Werewolf", null, "werewolf."),
    new ModeSpec("taboo", "Forbidden Words", null, "taboo."),
    new ModeSpec("wavelength", "Spectrum", null, "wavelength."),
    new ModeSpec("escape_room", "Escape Room", null, "escape_room.")
  ];
  function getMode(id) {
    if (!isStr(id)) return null;
    for (var i = 0; i < REGISTRY.length; i++) if (REGISTRY[i].id === id) return REGISTRY[i];
    return null;
  }
  function modeForAction(type) {
    for (var i = 0; i < REGISTRY.length; i++) if (type.startsWith(REGISTRY[i].actionPrefix)) return REGISTRY[i];
    return null;
  }
  // contents: Map mode -> ContentStatus
  function catalog(contents) {
    return REGISTRY.map(function (spec) {
      var status = contents.has(spec.id) ? contents.get(spec.id) : null;
      return { id: spec.id, title: spec.title, implemented: spec.implemented,
        content: (spec.implemented && status !== null) ? status.status : null };
    });
  }
  // packs: {mode: Uint8Array|null}; mirrors content.load_all
  function loadAll(packs) {
    var out = new Map();
    REGISTRY.forEach(function (spec) {
      if (!spec.implemented) return;
      var bytes = has(packs, spec.id) ? packs[spec.id] : null;
      out.set(spec.id, loadMode(spec.id, bytes, spec.module.validateItem, spec.module.validateItems));
    });
    return out;
  }

  // ------------------------------------------------------------ room_logic
  var HARD_MIN = 3, HARD_MAX = 12;
  var ROOM_ACTIONS = ["room.set_mode", "room.start", "room.pause", "room.resume", "room.end", "room.reset", "room.close", "round.next"];
  function newRoomState(code, operatorName, minP, maxP) {
    return { code: code, operatorName: operatorName, limits: { min: minP, max: maxP }, phase: "lobby",
      mode: null, gameNo: 0, players: [], game: null };
  }
  function applyJoin(state, playerId, name) {
    var phase = state.phase;
    if (phase === "closed") return [state, "room_closed"];
    if (phase === "playing" || phase === "paused") return [state, "game_in_progress"];
    if (state.players.length >= state.limits.max) return [state, "room_full"];
    var key = matchKey(name);
    if (state.players.some(function (p) { return matchKey(p.name) === key; })) return [state, "name_taken"];
    var s = deepcopy(state);
    s.players.push({ id: playerId, name: name, score: 0 });
    return [s, null];
  }
  function syncScores(s) {
    var scores = s.game ? get(s.game, "scores", obj()) : obj();
    s.players.forEach(function (p) { if (has(scores, p.id)) p.score = scores[p.id]; });
  }
  function contentError(contents, mode) {
    var status = contents.has(mode) ? contents.get(mode) : null;
    if (status === null || status.status === "content_missing") return "content_missing";
    if (!status.ok()) return "content_invalid";
    return null;
  }
  // seed: decimal string (64-bit, JSON-safe) or 0
  function reduceRoom(state, actor, action, seq, contents, seed) {
    var role = get(actor, "role"), kind = action.type;
    var payload = get(action, "payload");
    if (!pyTruthy(payload)) payload = obj();
    var roundId = get(action, "roundId"), phase = state.phase, s, err, r;
    if (role === "display") return [state, "forbidden"];
    var isRoomAction = ROOM_ACTIONS.indexOf(kind) >= 0;
    var spec = isRoomAction ? null : modeForAction(kind);
    if (!isRoomAction && spec === null) return [state, "unknown_action"];
    if (isRoomAction && role !== "operator") return [state, "forbidden"];
    if (phase === "closed") return [state, "room_closed"];
    var game = state.game;
    if ((phase === "playing" || phase === "paused") && game !== null && !isNone(roundId) && roundId !== game.roundId) {
      return [state, "stale_round"];
    }
    if (kind === "room.set_mode") {
      var target = get(payload, "mode"), ts = getMode(target);
      if (ts === null) return [state, "unknown_mode"];
      if (!ts.implemented) return [state, "mode_unimplemented"];
      if (phase !== "lobby" && phase !== "ended") return [state, "invalid_phase"];
      s = deepcopy(state); s.mode = target; s.phase = "lobby"; s.game = null;
      return [s, null];
    }
    if (kind === "room.start") {
      if (phase !== "lobby" && phase !== "ended") return [state, "invalid_phase"];
      var ms = getMode(state.mode);
      if (ms === null) return [state, "mode_not_selected"];
      if (!ms.implemented) return [state, "mode_unimplemented"];
      err = contentError(contents, ms.id);
      if (err) return [state, err];
      var count = state.players.length;
      if (count < Math.max(state.limits.min, ms.module.MIN_PLAYERS)) return [state, "not_enough_players"];
      if (count > state.limits.max) return [state, "too_many_players"];
      s = deepcopy(state);
      s.gameNo += 1;
      r = ms.module.initialize({ seed: seed, gameNo: s.gameNo }, deepcopy(s.players), contents.get(ms.id).pack);
      if (r[1]) return [state, r[1]];
      s.game = r[0]; s.phase = "playing";
      return [s, null];
    }
    if (kind === "room.pause") {
      if (phase !== "playing") return [state, "invalid_phase"];
      s = deepcopy(state); s.phase = "paused"; return [s, null];
    }
    if (kind === "room.resume") {
      if (phase !== "paused") return [state, "invalid_phase"];
      s = deepcopy(state); s.phase = "playing"; return [s, null];
    }
    var module;
    if (kind === "room.end") {
      if ((phase !== "playing" && phase !== "paused") || game === null) return [state, "invalid_phase"];
      module = getMode(state.mode).module;
      r = module.reduce(game, actor, { type: "game.end", payload: obj() }, seq);
      if (r[1]) return [state, r[1]];
      s = deepcopy(state); s.game = r[0]; s.phase = "ended"; syncScores(s);
      return [s, null];
    }
    if (kind === "room.reset") {
      var clear = get(payload, "clearScores", false);
      if (!isBool(clear)) return [state, "invalid_payload"];
      s = deepcopy(state); s.phase = "lobby"; s.game = null;
      if (clear) s.players.forEach(function (p) { p.score = 0; });
      return [s, null];
    }
    if (kind === "room.close") {
      s = deepcopy(state); s.phase = "closed"; s.game = null; return [s, null];
    }
    if (phase === "paused") return [state, "paused"];
    if (phase !== "playing" || game === null) return [state, "invalid_phase"];
    if (spec !== null && spec.id !== state.mode) return [state, "wrong_mode"];
    if (isNone(roundId)) return [state, "round_required"];
    module = getMode(state.mode).module;
    r = module.reduce(game, actor, action, seq);
    if (r[1]) return [state, r[1]];
    s = deepcopy(state); s.game = r[0]; syncScores(s);
    return [s, null];
  }
  // presence: Map playerId -> count
  function projectRoom(state, viewer, revision, presence, contents) {
    var role = viewer.role;
    var pid = role === "player" ? get(viewer, "playerId") : null;
    var selfView = null;
    if (!isNone(pid)) state.players.forEach(function (p) { if (p.id === pid) selfView = { id: p.id, name: p.name }; });
    var players = state.players.map(function (p) {
      return { id: p.id, name: p.name, connected: (presence.get(p.id) || 0) > 0, score: p.score };
    });
    var gameView = null, roundId = null, game = state.game, spec = getMode(state.mode);
    if (game !== null && spec !== null && spec.implemented) {
      gameView = spec.module.project(game, { role: role, playerId: pid });
      roundId = game.roundId;
    }
    var snap = { protocol: 1, code: state.code, revision: revision, role: role, self: selfView, mode: state.mode,
      phase: state.phase, roundId: roundId, players: players, game: gameView,
      operatorName: state.operatorName, playerLimits: { min: state.limits.min, max: state.limits.max } };
    if (role === "operator") snap.modes = catalog(contents);
    return snap;
  }

  // -------------------------------------------------------------- entropy
  // Production feeds bytes from the OS CSPRNG. Derivations (match the oracle
  // harness's FakeSecrets): choice = alphabet[byte % len] (len 32 here, so
  // unbiased); token_urlsafe(32) = base64url(32 bytes) unpadded; token_hex(5)
  // = 5 bytes hex; randbits(64) = 8 bytes big-endian as a decimal string.
  function EntropyPool(bytes) { this.bytes = bytes; this.used = 0; }
  EntropyPool.fromHex = function (hex) {
    var b = new Uint8Array(hex.length / 2);
    for (var i = 0; i < b.length; i++) b[i] = parseInt(hex.substr(i * 2, 2), 16);
    return new EntropyPool(b);
  };
  EntropyPool.prototype.take = function (n) {
    if (this.used + n > this.bytes.length) throw new PartyError("internal_error"); // pool exhausted
    var out = this.bytes.subarray(this.used, this.used + n);
    this.used += n;
    return out;
  };
  var B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  function base64url(b) {
    var out = "", i;
    for (i = 0; i + 2 < b.length; i += 3) {
      var v = (b[i] << 16) | (b[i + 1] << 8) | b[i + 2];
      out += B64URL[v >> 18] + B64URL[(v >> 12) & 63] + B64URL[(v >> 6) & 63] + B64URL[v & 63];
    }
    if (b.length - i === 1) { out += B64URL[b[i] >> 2] + B64URL[(b[i] & 3) << 4]; }
    else if (b.length - i === 2) {
      var w = (b[i] << 8) | b[i + 1];
      out += B64URL[w >> 10] + B64URL[(w >> 4) & 63] + B64URL[(w & 15) << 2];
    }
    return out;
  }
  function hex(b) { var s = ""; for (var i = 0; i < b.length; i++) s += (b[i] < 16 ? "0" : "") + b[i].toString(16); return s; }
  var Secrets = {
    choice: function (pool, alphabet) { return alphabet[pool.take(1)[0] % alphabet.length]; },
    tokenUrlsafe: function (pool) { return base64url(pool.take(32)); },
    tokenHex: function (pool, n) { return hex(pool.take(n)); },
    randbits64: function (pool) { return BigInt("0x" + hex(pool.take(8))).toString(); }
  };

  // --------------------------------------------------------------- SHA-256
  var K256 = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
  function sha256Hex(bytes) {
    var len = bytes.length, total = ((len + 9 + 63) >> 6) << 6, m = new Uint8Array(total);
    m.set(bytes); m[len] = 0x80;
    var bits = len * 8;
    m[total - 4] = (bits >>> 24) & 255; m[total - 3] = (bits >>> 16) & 255; m[total - 2] = (bits >>> 8) & 255; m[total - 1] = bits & 255;
    m[total - 5] = Math.floor(bits / 4294967296) & 255;
    var H = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    var W = new Uint32Array(64);
    for (var off = 0; off < total; off += 64) {
      for (var t = 0; t < 16; t++) W[t] = (m[off + 4 * t] << 24) | (m[off + 4 * t + 1] << 16) | (m[off + 4 * t + 2] << 8) | m[off + 4 * t + 3];
      for (t = 16; t < 64; t++) {
        var x = W[t - 15], y = W[t - 2];
        var s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        var s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        W[t] = (W[t - 16] + s0 + W[t - 7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (t = 0; t < 64; t++) {
        var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        var t1 = (h + S1 + ((e & f) ^ (~e & g)) + K256[t] + W[t]) >>> 0;
        var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        var t2 = (S0 + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] += a; H[1] += b; H[2] += c; H[3] += d; H[4] += e; H[5] += f; H[6] += g; H[7] += h;
    }
    var out = "";
    for (var k = 0; k < 8; k++) out += ("00000000" + H[k].toString(16)).slice(-8);
    return out;
  }
  function hashToken(token) { // token is ASCII (TOKEN_RE) before this is called
    var b = new Uint8Array(token.length);
    for (var i = 0; i < token.length; i++) b[i] = token.charCodeAt(i);
    return sha256Hex(b);
  }

  // ------------------------------------------------------------ manager.py
  var CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789", CODE_LENGTH = 6;
  var CODE_RE = /^[A-Z2-9]{6}$/, ACTION_ID_RE = /^[A-Za-z0-9._:\-]{1,64}$/,
    ACTION_TYPE_RE = /^[a-z_]{1,24}(?:\.[a-z_]{1,32}){1,2}$/, ROUND_ID_RE = /^[A-Za-z0-9._:\-]{1,32}$/,
    TOKEN_RE = /^[A-Za-z0-9_\-]{32,128}$/;

  var CONFIG_DEFAULTS = {
    min_players: 3, max_players: 12, room_idle_ttl: 7200.0, room_max_age: 43200.0, token_ttl: 43200.0,
    closed_room_grace: 60.0, max_rooms: 64, action_retention: 128, heartbeat_interval: 15.0, sweep_interval: 30.0,
    sse_write_timeout: 10.0, request_timeout: 15.0, max_body_bytes: 16384, max_streams_per_principal: 4,
    max_streams_per_room: 64, max_streams_total: 256, max_connections: 320, request_rate: 30.0, request_burst: 120,
    create_per_minute: 10.0, create_burst: 10, join_per_minute: 60.0, join_burst: 30, action_rate: 10.0,
    action_burst: 30, access_log: false
  };
  // ServerConfig(**overrides). Throws Error("config: ...") where Python raises ValueError.
  function ServerConfig(overrides) {
    overrides = overrides || {};
    var unknown = Object.keys(overrides).filter(function (k) { return !hasOwn.call(CONFIG_DEFAULTS, k); }).sort();
    if (unknown.length) throw new Error("config: unknown config keys: " + unknown.join(", "));
    var self = this;
    Object.keys(CONFIG_DEFAULTS).forEach(function (k) { self[k] = hasOwn.call(overrides, k) ? overrides[k] : CONFIG_DEFAULTS[k]; });
    if (!(HARD_MIN <= this.min_players && this.min_players <= this.max_players && this.max_players <= HARD_MAX)) {
      throw new Error("config: player limits must satisfy 3 <= min <= max <= 12");
    }
    ["room_idle_ttl", "room_max_age", "token_ttl", "heartbeat_interval", "sweep_interval", "sse_write_timeout", "request_timeout"]
      .forEach(function (k) { if (!(self[k] > 0)) throw new Error("config: " + k + " must be positive"); });
    if (this.closed_room_grace < 0) throw new Error("config: closed_room_grace must not be negative");
    ["max_rooms", "action_retention", "max_body_bytes", "max_streams_per_principal", "max_streams_per_room",
      "max_streams_total", "max_connections", "request_burst", "create_burst", "join_burst", "action_burst"]
      .forEach(function (k) { if (Math.trunc(self[k]) < 1) throw new Error("config: " + k + " must be >= 1"); });
    ["request_rate", "create_per_minute", "join_per_minute", "action_rate"]
      .forEach(function (k) { if (!(self[k] > 0)) throw new Error("config: " + k + " must be positive"); });
  }

  function Principal(role, playerId, tokenHash, expiresAt) {
    this.role = role; this.playerId = playerId; this.tokenHash = tokenHash; this.expiresAt = expiresAt;
  }
  Principal.prototype.key = function () { return this.role === "player" ? "player:" + this.playerId : this.role; };
  Principal.prototype.actor = function () { return { role: this.role, playerId: this.playerId }; };

  function Room(code, state, now) {
    this.code = code; this.state = state; this.revision = 1; this.receiptSeq = 0;
    this.tokens = new Map(); this.actions = new Map(); this.streams = new Map(); this.presence = new Map();
    this.streamTotal = 0; this.createdAt = now; this.lastActivity = now; this.closedAt = null; this.expired = false;
  }
  Room.prototype.expiresAt = function (cfg) {
    var d = Math.min(this.createdAt + cfg.room_max_age, this.lastActivity + cfg.room_idle_ttl);
    if (this.closedAt !== null) d = Math.min(d, this.closedAt + cfg.closed_room_grace);
    return d;
  };

  function validateLimit(body, key, dflt) {
    var v = get(body, key, dflt);
    if (isNone(v)) return dflt;
    // A PyBigInt is an int to Python but always fails the 3..12 check that
    // follows, with the same invalid_config.
    if (isBool(v) || !isPyInt(v)) throw new PartyError("invalid_config");
    return v;
  }
  function parseAction(body) {
    var kind = get(body, "type");
    if (!isStr(kind) || !ACTION_TYPE_RE.test(kind)) return [null, "invalid_action"];
    var roundId = get(body, "roundId");
    if (!isNone(roundId) && (!isStr(roundId) || !ROUND_ID_RE.test(roundId))) return [null, "invalid_action"];
    var payload = get(body, "payload", obj());
    if (isNone(payload)) payload = obj();
    if (!isPyDict(payload)) return [null, "invalid_payload"];
    return [{ type: kind, roundId: roundId, payload: payload }, null];
  }

  // Single-threaded port: every call runs to completion on the engine queue,
  // which gives the total order Python gets from its locks. `now` is passed in
  // by the caller and read once per call, like the oracle's clock read under
  // the room lock. Methods that need randomness take an EntropyPool.
  function RoomManager(config, contents, options) {
    this.config = config; this.contents = contents;
    this.rooms = new Map(); this.streamTotal = 0; this.stopped = false;
    this.freeze = !!(options && options.freezeState);
    this.internalErrors = [];
  }
  RoomManager.prototype._commit = function (room, state) {
    room.state = this.freeze ? deepFreeze(state) : state;
  };
  RoomManager.prototype._expireIfDue = function (room, now) {
    if (room.expired || now >= room.expiresAt(this.config)) { room.expired = true; return true; }
    return false;
  };
  RoomManager.prototype._purgeExpired = function (now) {
    var removed = 0, self = this;
    Array.from(this.rooms.entries()).forEach(function (e) {
      if (self._expireIfDue(e[1], now)) { self.rooms.delete(e[0]); removed++; }
    });
    return removed;
  };
  RoomManager.prototype.sweep = function (now) { return this._purgeExpired(now); };
  RoomManager.prototype.shutdown = function () { this.stopped = true; };
  RoomManager.prototype._checkLive = function (room, principal, now) {
    if (room.expired || now >= room.expiresAt(this.config)) { room.expired = true; throw new PartyError("room_not_found"); }
    if (principal && now >= principal.expiresAt) throw new PartyError("token_expired");
  };
  RoomManager.prototype.createRoom = function (body, now, pool) {
    var cfg = this.config;
    var operatorName = get(body, "operatorName");
    if (!isNone(operatorName)) {
      operatorName = cleanName(operatorName);
      if (operatorName === null) throw new PartyError("invalid_name");
    } else operatorName = null;
    var minP = validateLimit(body, "minPlayers", cfg.min_players);
    var maxP = validateLimit(body, "maxPlayers", cfg.max_players);
    if (!(HARD_MIN <= minP && minP <= maxP && maxP <= HARD_MAX)) throw new PartyError("invalid_config");
    this._purgeExpired(now);
    if (this.rooms.size >= cfg.max_rooms) throw new PartyError("too_many_rooms");
    var code;
    do {
      code = "";
      for (var i = 0; i < CODE_LENGTH; i++) code += Secrets.choice(pool, CODE_ALPHABET);
    } while (this.rooms.has(code));
    var room = new Room(code, null, now);
    this._commit(room, newRoomState(code, operatorName, minP, maxP));
    var hostToken = Secrets.tokenUrlsafe(pool), displayToken = Secrets.tokenUrlsafe(pool);
    room.tokens.set(hashToken(hostToken), new Principal("operator", null, hashToken(hostToken), now + cfg.token_ttl));
    room.tokens.set(hashToken(displayToken), new Principal("display", null, hashToken(displayToken), now + cfg.token_ttl));
    this.rooms.set(code, room);
    return { code: room.code, hostToken: hostToken, displayToken: displayToken, revision: room.revision };
  };
  RoomManager.prototype.getRoom = function (code, now) {
    if (!isStr(code)) throw new PartyError("room_not_found");
    code = upperForCode(code);
    if (code === null || !CODE_RE.test(code)) throw new PartyError("room_not_found");
    var room = this.rooms.get(code);
    if (!room) throw new PartyError("room_not_found");
    if (this._expireIfDue(room, now)) { this.rooms.delete(code); throw new PartyError("room_not_found"); }
    return room;
  };
  RoomManager.prototype.authenticate = function (room, token, now) {
    if (!isStr(token) || !TOKEN_RE.test(token)) throw new PartyError("unauthorized");
    var digest = hashToken(token);
    this._checkLive(room, null, now);
    var principal = room.tokens.get(digest);
    if (!principal) throw new PartyError("unauthorized");
    this._checkLive(room, principal, now);
    return principal;
  };
  // Returns [status, response].
  RoomManager.prototype.join = function (room, body, token, now, pool) {
    var cfg = this.config;
    if (!isNone(token)) {
      var principal = this.authenticate(room, token, now);
      if (principal.role !== "player") throw new PartyError("forbidden");
      this._checkLive(room, principal, now);
      return [200, { playerId: principal.playerId, playerToken: token, revision: room.revision, resumed: true }];
    }
    var name = cleanName(get(body, "name"));
    if (name === null) throw new PartyError("invalid_name");
    token = Secrets.tokenUrlsafe(pool);
    var digest = hashToken(token);
    this._checkLive(room, null, now);
    var existing = room.state.players.map(function (p) { return p.id; });
    var playerId = "p" + Secrets.tokenHex(pool, 5);
    while (existing.indexOf(playerId) >= 0) playerId = "p" + Secrets.tokenHex(pool, 5);
    var r = applyJoin(room.state, playerId, name);
    if (r[1]) throw new PartyError(r[1]);
    this._commit(room, r[0]);
    room.tokens.set(digest, new Principal("player", playerId, digest, now + cfg.token_ttl));
    room.revision += 1;
    room.lastActivity = now;
    return [201, { playerId: playerId, playerToken: token, revision: room.revision }];
  };
  RoomManager.prototype.project = function (room, principal) { // no liveness side effects
    return projectRoom(room.state, { role: principal.role, playerId: principal.playerId }, room.revision, room.presence, this.contents);
  };
  RoomManager.prototype.snapshot = function (room, principal, now) {
    this._checkLive(room, principal, now);
    return this.project(room, principal);
  };
  // Returns [status, result, replayed].
  RoomManager.prototype.submitAction = function (room, principal, body, now, pool) {
    var actionId = get(body, "actionId");
    if (!isStr(actionId) || !ACTION_ID_RE.test(actionId)) throw new PartyError("invalid_action_id");
    var pa = parseAction(body), action = pa[0], err = pa[1];
    this._checkLive(room, principal, now);
    var key = principal.key();
    var log = room.actions.get(key);
    if (!log) { log = new Map(); room.actions.set(key, log); }
    if (log.has(actionId)) { var prev = log.get(actionId); return [prev[0], prev[1], true]; }
    room.receiptSeq += 1;
    var seq = room.receiptSeq, newState = null;
    if (err === null) {
      // Drawn before the guarded call, like the oracle: a start that fails
      // validation still consumes its 8 seed bytes.
      var seed = action.type === "room.start" ? Secrets.randbits64(pool) : 0;
      try {
        var r = reduceRoom(room.state, principal.actor(), action, seq, this.contents, seed);
        newState = r[0]; err = r[1];
      } catch (e) { // `except Exception` in the oracle; kept for diagnostics only
        this.internalErrors.push(String(e && e.stack || e));
        newState = null; err = "internal_error";
      }
    }
    var entry;
    if (err === null) {
      this._commit(room, newState);
      room.revision += 1;
      room.lastActivity = now;
      if (newState.phase === "closed" && room.closedAt === null) room.closedAt = now;
      entry = [200, { accepted: true, revision: room.revision }];
    } else {
      entry = [statusFor(err), { accepted: false, revision: room.revision, error: errorBody(err) }];
    }
    log.set(actionId, entry);
    while (log.size > this.config.action_retention) log.delete(log.keys().next().value);
    return [entry[0], entry[1], false];
  };
  RoomManager.prototype.openStream = function (room, principal, now) {
    var cfg = this.config;
    if (this.streamTotal >= cfg.max_streams_total) throw new PartyError("too_many_streams");
    this.streamTotal += 1;
    try {
      this._checkLive(room, principal, now);
      var key = principal.key();
      if ((room.streams.get(key) || 0) >= cfg.max_streams_per_principal || room.streamTotal >= cfg.max_streams_per_room) {
        throw new PartyError("too_many_streams");
      }
      room.streams.set(key, (room.streams.get(key) || 0) + 1);
      room.streamTotal += 1;
      if (principal.role === "player") {
        var previous = room.presence.get(principal.playerId) || 0;
        room.presence.set(principal.playerId, previous + 1);
        if (previous === 0) room.revision += 1;
      }
    } catch (e) {
      this.streamTotal -= 1;
      throw e;
    }
  };
  RoomManager.prototype.closeStream = function (room, principal) {
    var key = principal.key();
    var count = (room.streams.get(key) || 0) - 1;
    if (count > 0) room.streams.set(key, count); else room.streams.delete(key);
    room.streamTotal = Math.max(0, room.streamTotal - 1);
    if (principal.role === "player") {
      var remaining = (room.presence.get(principal.playerId) || 0) - 1;
      if (remaining > 0) room.presence.set(principal.playerId, remaining);
      else {
        room.presence.delete(principal.playerId);
        if (!room.expired) room.revision += 1;
      }
    }
    this.streamTotal = Math.max(0, this.streamTotal - 1);
  };
  // One non-blocking step of next_stream_event: returns ["state", rev, snap],
  // ["heartbeat"], ["error", code], ["close"] or ["wait", seconds]. The caller
  // (transport) re-polls after `seconds` or on the next room change.
  RoomManager.prototype.nextStreamEvent = function (room, principal, lastRevision, heartbeatAt, now) {
    if (this.stopped) return ["close"];
    if (room.expired || now >= room.expiresAt(this.config)) { room.expired = true; return ["error", "room_expired"]; }
    if (now >= principal.expiresAt) return ["error", "token_expired"];
    if (room.revision !== lastRevision) return ["state", room.revision, this.project(room, principal)];
    if (now >= heartbeatAt) return ["heartbeat"];
    var deadline = Math.min(heartbeatAt, principal.expiresAt, room.expiresAt(this.config));
    return ["wait", Math.max(0.01, Math.min(deadline - now, 5.0))];
  };

  // httpd._serve_events loop state, minus socket writes: open -> poll* -> end.
  function EventSession(manager, room, principal, now) {
    manager.openStream(room, principal, now);
    this.manager = manager; this.room = room; this.principal = principal;
    this.lastRevision = null; this.heartbeatAt = now + manager.config.heartbeat_interval; this.open = true;
  }
  EventSession.prototype.poll = function (now) {
    if (!this.open) throw new Error("session closed");
    var ev = this.manager.nextStreamEvent(this.room, this.principal, this.lastRevision, this.heartbeatAt, now);
    if (ev[0] === "wait") return ev;
    if (ev[0] === "close" || ev[0] === "error") { this.end(); return ev; }
    if (ev[0] === "state") this.lastRevision = ev[1];
    this.heartbeatAt = now + this.manager.config.heartbeat_interval;
    return ev;
  };
  EventSession.prototype.end = function () {
    if (this.open) { this.open = false; this.manager.closeStream(this.room, this.principal); }
  };

  return {
    version: "l01a-1",
    unicode: { nfc: nfc, nfkc: nfkc, nfd: nfd, nfkd: nfkd, casefold: casefold, pySplit: pySplit, pyStrip: pyStrip,
      pyLen: pyLen, cleanText: cleanText, cleanName: cleanName, matchKey: matchKey, upperForCode: upperForCode,
      decodeUtf8: decodeUtf8, unidata: T.unidata },
    json: { parse: parseJson, parseRequestBody: parseRequestBody, JsonError: JsonError, PyFloat: PyFloat, PyBigInt: PyBigInt, isDict: isPyDict },
    errors: { ERRORS: ERRORS, PartyError: PartyError, statusFor: statusFor, errorBody: errorBody },
    content: { loadMode: loadMode, loadAll: loadAll, parsePack: parsePack, isText: isText, isInt: isInt },
    modes: { REGISTRY: REGISTRY, getMode: getMode, catalog: catalog, WhoAmI: WhoAmI, Feud: Feud },
    room: { newRoomState: newRoomState, applyJoin: applyJoin, reduceRoom: reduceRoom, projectRoom: projectRoom },
    random: { shuffleInPlace: shuffleInPlace },
    crypto: { sha256Hex: sha256Hex, hashToken: hashToken, base64url: base64url },
    EntropyPool: EntropyPool, Secrets: Secrets, ServerConfig: ServerConfig, RoomManager: RoomManager,
    EventSession: EventSession, deepcopy: deepcopy, deepFreeze: deepFreeze
  };
})(typeof MajlisUnicode13Tables !== "undefined" ? MajlisUnicode13Tables : null);
