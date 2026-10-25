"""Record language-neutral L01A parity traces from the accepted Python oracle.

The oracle is the unmodified Python server (party/server at 77ecabd),
imported from ORACLE_ROOT. Only these seams are replaced, never its logic:

* ``manager.secrets``: FakeSecrets drawing from a derived byte pool, with
  the same derivations the JS EntropyPool uses (choice = byte % len,
  token_urlsafe(32) = base64url of 32 bytes, token_hex(5), randbits(64) =
  8 bytes big-endian). Pool bytes = SHA-256(seed ":" i) blocks.
* ``RoomManager.clock``: the step's injected ``now``.
* ``Room.cond.wait``: raises WouldWait(timeout) instead of blocking, so one
  ``next_stream_event`` call is one non-blocking poll.
* Request bodies go through the oracle's own ``PartyRequestHandler._read_json``
  with a fake request (Content-Length and application/json set by the harness).

Every trace is a list of static ops. Python decides the ops adaptively while
recording; the JS runner replays the same ops blind. Outputs never contain a
plaintext token: any ``*Token`` string becomes ``{"sha256": ...}``.

Usage: python3 -I -B oracle_record.py ORACLE_ROOT OUT_DIR [--fuzz N] [--seed S]
"""

import argparse
import base64
import copy
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unicodedata

ARGS = None
CODE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789")
POOL_BYTES = 16384


def b64(data):
    return base64.b64encode(data).decode("ascii")


def pool_bytes(seed, size):
    out = bytearray()
    i = 0
    while len(out) < size:
        out += hashlib.sha256(("%s:%d" % (seed, i)).encode("ascii")).digest()
        i += 1
    return bytes(out[:size])


class PoolExhausted(Exception):
    pass


class FakeSecrets(object):
    def __init__(self, data):
        self.data = data
        self.used = 0

    def _take(self, n):
        if self.used + n > len(self.data):
            raise PoolExhausted("entropy pool exhausted")
        out = self.data[self.used:self.used + n]
        self.used += n
        return out

    def choice(self, seq):
        return seq[self._take(1)[0] % len(seq)]

    def token_urlsafe(self, nbytes):
        return base64.urlsafe_b64encode(self._take(nbytes)).rstrip(b"=").decode("ascii")

    def token_hex(self, nbytes):
        return self._take(nbytes).hex()

    def randbits(self, k):
        if k != 64:
            raise AssertionError("only randbits(64) is derived")
        return int.from_bytes(self._take(8), "big")


class WouldWait(Exception):
    def __init__(self, timeout):
        Exception.__init__(self, timeout)
        self.timeout = timeout


def load_oracle(root):
    sys.path.insert(0, root)
    global M, H, C, E, RL, TU, MODES
    from party.server import manager as M
    from party.server import httpd as H
    from party.server import content as C
    from party.server import errors as E
    from party.server import room_logic as RL
    from party.server import textutil as TU
    from party.server import modes as MODES

    class NoWaitCondition(threading.Condition):
        def wait(self, timeout=None):
            raise WouldWait(timeout)

    original_init = M.Room.__init__

    def room_init(self, code, state, now):
        original_init(self, code, state, now)
        self.cond = NoWaitCondition(self.lock)

    M.Room.__init__ = room_init


# ------------------------------------------------------------------ helpers

def redact(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.endswith("Token") and isinstance(v, str):
                out[k] = {"sha256": M.hash_token(v)}
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def tag(value):
    """Exact, order-preserving representation of a decoded JSON value."""
    if value is None:
        return {"n": 1}
    if isinstance(value, bool):
        return {"b": value}
    if isinstance(value, int):
        return {"i": str(value)}
    if isinstance(value, float):
        if value != value:
            return {"f": "nan"}
        if value in (float("inf"), float("-inf")):
            return {"f": "inf" if value > 0 else "-inf"}
        if value == 0 and str(value).startswith("-"):
            return {"f": "-0"}
        return {"f": value}
    if isinstance(value, str):
        return {"s": value}
    if isinstance(value, list):
        return {"l": [tag(v) for v in value]}
    if isinstance(value, dict):
        return {"d": [[k, tag(v)] for k, v in value.items()]}
    raise TypeError(type(value))


def json_text(value, ascii_only=None):
    """Body text for a generated JSON value; ASCII when it holds lone surrogates."""
    if ascii_only is None:
        text = json.dumps(value, ensure_ascii=False, allow_nan=True)
        try:
            text.encode("utf-8")
            return text
        except UnicodeEncodeError:
            pass
    return json.dumps(value, ensure_ascii=True, allow_nan=True)


def body_json(value, ascii_only=None):
    return {"text": json_text(value, ascii_only)}


def body_bytes(spec):
    if "empty" in spec:
        return None
    if "text" in spec:
        return spec["text"].encode("utf-8")
    return base64.b64decode(spec["b64"])


class FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeRequest(object):
    def __init__(self, data):
        self.headers = FakeHeaders()
        if data is not None:
            self.headers["Content-Length"] = str(len(data))
            self.headers["Content-Type"] = "application/json"
        self.rfile = io.BytesIO(data or b"")
        self.close_connection = False

    def _drain(self, length):
        pass


class FakeApp(object):
    def __init__(self, config):
        self.config = config


def read_json(config, spec, allow_empty):
    req = FakeRequest(body_bytes(spec))
    return H.PartyRequestHandler._read_json(req, FakeApp(config), allow_empty)


# -------------------------------------------------------------- the recorder

class Trace(object):
    def __init__(self, name, kind="room", content=None, config=None, seed=None, pool=POOL_BYTES, note=""):
        self.name = name
        self.content = content or {"who_am_i": "accepted", "feud": "accepted"}
        self.config_overrides = config or {}
        self.seed = seed if seed is not None else "l01a:" + name
        self.secrets = FakeSecrets(pool_bytes(self.seed, pool))
        self.pool = pool
        self.note = note
        self.now = 1000.0
        self.ops, self.outs = [], []
        self.rooms, self.room_order = {}, []
        self.tokens, self.pids, self.sessions = {}, {}, {}
        self.prev_obs = {}
        M.secrets = self.secrets
        tmp = tempfile.mkdtemp(prefix="l01a-content-")
        try:
            for mode, spec in self.content.items():
                data = BLOBS[spec + "/" + mode] if isinstance(spec, str) else (
                    None if spec is None else base64.b64decode(spec["b64"]))
                if data is not None:
                    with open(os.path.join(tmp, mode + ".json"), "wb") as handle:
                        handle.write(data)
            self.contents = C.load_all(tmp, MODES.REGISTRY)
        finally:
            shutil.rmtree(tmp)
        self.config = M.ServerConfig(**self.config_overrides)
        self.mgr = M.RoomManager(self.config, self.contents, clock=lambda: self.now)

    def header(self):
        return {"trace": self.name, "kind": "room", "note": self.note, "content": self.content,
                "config": self.config_overrides, "entropy": {"seed": self.seed, "bytes": self.pool}}

    # -- reference resolution (both sides keep their own tables)
    def _room(self, ref):
        if "code" in ref:
            return ref["code"]
        return self.rooms[ref["ref"]]

    def _token(self, ref):
        if ref is None:
            return None
        if "literal" in ref:
            return ref["literal"]
        return self.tokens.get(ref["ref"])  # a ref whose join failed sends no token

    def run(self, op):
        if "now" in op:
            self.now = float(op["now"])
        try:
            out = self._apply(op)
        except M.PartyError as exc:
            out = {"status": exc.status, "error": E.error_body(exc.code)}
        except (WouldWait, PoolExhausted) as exc:
            out = {"exception": type(exc).__name__}
        except Exception as exc:  # recorded, never hidden
            out = {"exception": type(exc).__name__, "detail": str(exc)[:200]}
        self.ops.append(op)
        self.outs.append({"out": out, "obs": self.observe()})
        return out

    def _apply(self, op):
        kind = op["op"]
        mgr = self.mgr
        if kind == "create":
            body = read_json(self.config, op["body"], True)
            result = mgr.create_room(body)
            if op.get("as"):
                self.rooms[op["as"]] = result["code"]
                self.tokens[op["as"] + ".host"] = result["hostToken"]
                self.tokens[op["as"] + ".display"] = result["displayToken"]
                self.room_order.append(("%s#%d" % (op["as"], len(self.room_order)), mgr._rooms[result["code"]]))
            return {"status": 201, "body": redact(result)}
        if kind == "join":
            body = read_json(self.config, op["body"], True)
            token = self._token(op.get("token"))
            room = mgr.get_room(self._room(op["room"]))
            status, result = mgr.join(room, body, token)
            if op.get("as"):
                self.tokens[op["as"]] = result["playerToken"]
                self.pids[op["as"]] = result["playerId"]
            return {"status": status, "body": redact(result)}
        if kind == "state":
            token = self._token(op.get("token"))
            if token is None:
                raise M.PartyError("unauthorized")
            room = mgr.get_room(self._room(op["room"]))
            principal = mgr.authenticate(room, token)
            return {"status": 200, "body": mgr.snapshot(room, principal)}
        if kind == "action":
            body = read_json(self.config, op["body"], False)
            token = self._token(op.get("token"))
            if token is None:
                raise M.PartyError("unauthorized")
            room = mgr.get_room(self._room(op["room"]))
            principal = mgr.authenticate(room, token)
            status, result, replayed = mgr.submit_action(room, principal, body)
            return {"status": status, "body": result, "replayed": replayed}
        if kind == "open":
            token = self._token(op.get("token"))
            if token is None:
                raise M.PartyError("unauthorized")
            room = mgr.get_room(self._room(op["room"]))
            principal = mgr.authenticate(room, token)
            mgr.open_stream(room, principal)
            self.sessions[op["as"]] = {"room": room, "principal": principal, "last": None,
                                       "hb": mgr.clock() + self.config.heartbeat_interval, "open": True}
            return {"status": 200, "opened": True}
        if kind == "poll":
            s = self.sessions.get(op["stream"])
            if s is None:
                return {"harness": "no stream"}
            if not s["open"]:
                return {"harness": "stream not open"}
            try:
                ev = mgr.next_stream_event(s["room"], s["principal"], s["last"], s["hb"])
            except WouldWait as wait:
                return {"event": "wait", "timeout": wait.timeout}
            if ev[0] in ("close", "error"):
                s["open"] = False
                mgr.close_stream(s["room"], s["principal"])
                if ev[0] == "close":
                    return {"event": "close"}
                return {"event": "error", "error": E.error_body(ev[1])}
            if ev[0] == "state":
                s["last"] = ev[1]
                out = {"event": "state", "id": ev[1], "data": ev[2]}
            else:
                out = {"event": "heartbeat"}
            s["hb"] = mgr.clock() + self.config.heartbeat_interval
            return out
        if kind == "close":
            s = self.sessions.get(op["stream"])
            if s is None:
                return {"harness": "no stream"}
            if s["open"]:
                s["open"] = False
                mgr.close_stream(s["room"], s["principal"])
                return {"closed": True}
            return {"closed": False}
        if kind == "sweep":
            return {"removed": mgr.sweep()}
        if kind == "shutdown":
            mgr.shutdown()
            return {"stopped": True}
        if kind == "lookup":
            room = mgr.get_room(self._room(op["room"]))
            return {"found": room.code}
        raise ValueError("unknown op " + kind)

    def observe(self):
        mgr = self.mgr
        rooms = {}
        for ref, room in self.room_order:
            obs = {
                "inTable": mgr._rooms.get(room.code) is room,
                "state": room.state,
                "revision": room.revision,
                "receiptSeq": room.receipt_seq,
                "expired": room.expired,
                "createdAt": room.created_at,
                "lastActivity": room.last_activity,
                "closedAt": room.closed_at,
                "streams": [[k, v] for k, v in room.streams.items()],
                "presence": [[k, v] for k, v in room.presence.items()],
                "streamTotal": room.stream_total,
                "tokens": [[h, p.role, p.player_id, p.expires_at] for h, p in room.tokens.items()],
                "actions": [[k, [[aid, e[0], e[1]] for aid, e in log.items()]] for k, log in room.actions.items()],
                "projections": [[p.key, mgr._project_locked(room, p)] for p in room.tokens.values()],
            }
            canon = json.dumps(obs, sort_keys=True)
            if self.prev_obs.get(ref) == canon:
                rooms[ref] = {"same": True}
            else:
                self.prev_obs[ref] = canon
                rooms[ref] = obs
        return {"rooms": rooms, "table": list(mgr._rooms.keys()), "streamTotal": mgr._stream_total,
                "stopped": mgr._stop.is_set(), "entropyUsed": self.secrets.used}

    # -- convenience wrappers used by scenarios
    def create(self, ref, body=None, now=None, raw=None):
        op = {"op": "create", "now": self._t(now), "body": raw or body_json(body if body is not None else {}),
              "as": ref}
        return self.run(op)

    def join(self, room, ref, name=None, token=None, now=None, raw=None, code=None):
        body = raw or (body_json({"name": name}) if name is not None else {"empty": True})
        op = {"op": "join", "now": self._t(now), "room": {"code": code} if code else {"ref": room},
              "body": body, "token": token, "as": ref}
        return self.run(op)

    def state(self, room, tok, now=None):
        return self.run({"op": "state", "now": self._t(now), "room": {"ref": room}, "token": tok})

    def act(self, room, tok, kind=None, payload=None, aid=None, round_id=None, now=None, raw=None, extra=None):
        if raw is None:
            body = {"actionId": aid or self.next_aid(), "type": kind}
            if payload is not None:
                body["payload"] = payload
            if round_id is not None:
                body["roundId"] = round_id
            if extra:
                body.update(extra)
            raw = body_json(body)
        return self.run({"op": "action", "now": self._t(now), "room": {"ref": room}, "token": tok, "body": raw})

    def open(self, room, tok, sid, now=None):
        return self.run({"op": "open", "now": self._t(now), "room": {"ref": room}, "token": tok, "as": sid})

    def poll(self, sid, now=None):
        return self.run({"op": "poll", "now": self._t(now), "stream": sid})

    def drain(self, sid, now=None, limit=6):
        outs = []
        for _ in range(limit):
            out = self.poll(sid, now)
            outs.append(out)
            if out.get("event") != "state":
                break
        return outs

    def close(self, sid):
        return self.run({"op": "close", "stream": sid})

    def sweep(self, now=None):
        return self.run({"op": "sweep", "now": self._t(now)})

    def lookup(self, code, now=None):
        return self.run({"op": "lookup", "now": self._t(now), "room": {"code": code}})

    _aid = 0

    def next_aid(self):
        self._aid += 1
        return "a%d" % self._aid

    def _t(self, now):
        if now is None:
            self.now += 0.25
            return self.now
        return now

    def pid(self, ref):
        return self.pids.get(ref, "p0000000000")

    def snap(self, room, tok):
        out = self.mgr._project_locked(self.mgr._rooms[self.rooms[room]],
                                       self.mgr._rooms[self.rooms[room]].tokens[M.hash_token(self.tokens[tok])])
        return out

    def records(self):
        yield self.header()
        for op, rec in zip(self.ops, self.outs):
            yield {"op": op, "py": rec}


T = lambda ref: {"ref": ref}  # noqa: E731  token reference


# ------------------------------------------------------------- golden traces

ARABIC_NAMES = ["أحمد", "سلامة", "مَريَم", "خالد"]


def g_whoami_full(content="accepted"):
    t = Trace("golden-whoami-full-" + content, content={"who_am_i": content, "feud": content},
              note="Full Who Am I game: streams, ready, guesses, dismiss, award, reveal, rounds, end, reset")
    t.create("R", {"operatorName": "  أبو   أحمد  "})
    names = ["Ahmed", "سلامة", "Maryam", "مَريم"]
    for i, n in enumerate(names):
        t.join("R", "p%d" % i, n)
    for sid, tok in (("sOp", "R.host"), ("sDisp", "R.display"), ("s0", "p0"), ("s1", "p1")):
        t.open("R", T(tok), sid)
        t.drain(sid)
    t.act("R", T("R.host"), "room.set_mode", {"mode": "who_am_i"})
    t.act("R", T("R.host"), "room.start")
    for sid in ("sOp", "sDisp", "s0", "s1"):
        t.drain(sid)
    rid = t.snap("R", "R.host")["roundId"]
    t.act("R", T("p0"), "whoami.ready", {}, round_id=rid)
    t.act("R", T("p1"), "whoami.ready", {"ready": True}, round_id=rid)
    t.act("R", T("p1"), "whoami.ready", {"ready": False}, round_id=rid)
    t.act("R", T("p2"), "whoami.ready", {"ready": 1}, round_id=rid)
    t.act("R", T("p0"), "whoami.guess", {"text": "  Am I   a lion? "}, round_id=rid)
    t.act("R", T("p1"), "whoami.guess", {"text": "أسَدٌ"}, round_id=rid)
    t.act("R", T("p2"), "whoami.guess", {"text": "x" * 81}, round_id=rid)
    t.act("R", T("p2"), "whoami.guess", {"text": "a‮b"}, round_id=rid)
    t.act("R", T("p3"), "whoami.guess", {"text": "\U0001F981 lion?"}, round_id=rid)
    for sid in ("sOp", "s0", "s1"):
        t.drain(sid)
    t.act("R", T("R.host"), "whoami.dismiss", {"playerId": t.pid("p0")}, round_id=rid)
    t.act("R", T("R.host"), "whoami.dismiss", {"playerId": t.pid("p0")}, round_id=rid)
    t.act("R", T("R.host"), "whoami.award", {"playerId": t.pid("p1")}, round_id=rid, aid="award-1")
    t.act("R", T("R.host"), "whoami.award", {"playerId": t.pid("p1")}, round_id=rid, aid="award-1")
    t.act("R", T("R.host"), "whoami.award", {"playerId": t.pid("p1"), "points": 5}, round_id=rid)
    t.act("R", T("R.host"), "whoami.award", {"playerId": t.pid("p3"), "points": 100}, round_id=rid)
    t.act("R", T("R.host"), "whoami.award", {"playerId": t.pid("p2"), "points": 101}, round_id=rid)
    t.act("R", T("R.host"), "whoami.award", {"playerId": t.pid("p2"), "points": True}, round_id=rid)
    t.act("R", T("R.host"), "whoami.reveal", {"playerId": t.pid("p0")}, round_id=rid)
    t.act("R", T("p0"), "whoami.guess", {"text": "late"}, round_id=rid)
    t.act("R", T("R.host"), "whoami.reveal", {"playerId": t.pid("p0")}, round_id=rid)
    t.act("R", T("R.host"), "whoami.reveal", {}, round_id=rid)
    t.act("R", T("R.host"), "whoami.reveal", {}, round_id=rid)
    for sid in ("sOp", "sDisp", "s0", "s1"):
        t.drain(sid)
    for _ in range(40):
        snap = t.snap("R", "R.host")
        out = t.act("R", T("R.host"), "round.next", round_id=snap["roundId"])
        if out.get("body", {}).get("error"):
            break
        rid = t.snap("R", "R.host")["roundId"]
        t.act("R", T("p1"), "whoami.guess", {"text": "guess %s" % rid}, round_id=rid)
        t.act("R", T("R.host"), "whoami.award", {"playerId": t.pid("p0"), "points": 2}, round_id=rid)
        t.act("R", T("p2"), "whoami.ready", round_id="g1r1")
    t.drain("s0")
    t.act("R", T("R.host"), "room.pause")
    t.act("R", T("p0"), "whoami.ready", round_id=t.snap("R", "R.host")["roundId"])
    t.act("R", T("R.host"), "room.resume")
    t.act("R", T("R.host"), "room.end")
    t.act("R", T("R.host"), "room.end")
    for sid in ("sOp", "sDisp", "s0", "s1"):
        t.drain(sid)
    t.state("R", T("p3"))
    t.act("R", T("R.host"), "room.reset", {"clearScores": "yes"})
    t.act("R", T("R.host"), "room.reset", {"clearScores": False})
    t.state("R", T("R.display"))
    t.act("R", T("R.host"), "room.start")
    t.act("R", T("R.host"), "room.end")
    t.act("R", T("R.host"), "room.reset", {"clearScores": True})
    t.act("R", T("R.host"), "room.close")
    t.act("R", T("R.host"), "room.close")
    for sid in ("sOp", "s0"):
        t.drain(sid)
    t.close("s0")
    t.close("s1")
    t.drain("sOp")
    t.join("R", "late", "Late")
    return t


def feud_answer_texts(t):
    return t.snap("R", "R.host")["game"]["answers"]


def g_feud_full(content="accepted"):
    t = Trace("golden-feud-full-" + content, content={"who_am_i": content, "feud": content},
              note="Full Feud game: buzz order, text/alias/id reveal, strikes, steal, award, every board")
    t.create("R", {"operatorName": "Host", "minPlayers": 3, "maxPlayers": 6})
    for i, n in enumerate(["Ali", "ALI ", "Ｆａｔｉｍａ", "Noor", "noor"]):
        t.join("R", "p%d" % i, n)
    for sid, tok in (("sDisp", "R.display"), ("s0", "p0"), ("sOp", "R.host")):
        t.open("R", T(tok), sid)
        t.drain(sid)
    t.act("R", T("R.host"), "room.start")
    t.act("R", T("R.host"), "room.set_mode", {"mode": "feud"})
    t.act("R", T("R.host"), "room.start")
    rnd = random.Random("feud-full")
    board = 0
    while True:
        board += 1
        snap = t.snap("R", "R.host")
        rid = snap["roundId"]
        answers = snap["game"]["answers"]
        t.act("R", T("p2"), "feud.buzz", round_id=rid)
        t.act("R", T("R.host"), "feud.open_buzzer", round_id=rid)
        t.act("R", T("R.host"), "feud.open_buzzer", round_id=rid)
        order = ["p2", "p0", "p3"] if board % 2 else ["p3", "p2", "p0"]
        for p in order:
            t.act("R", T(p), "feud.buzz", round_id=rid)
        t.act("R", T("R.display"), "feud.buzz", round_id=rid)
        t.act("R", T("R.host"), "feud.buzz", round_id=rid)
        if board == 2:
            t.act("R", T("R.host"), "feud.reset_buzzer", round_id=rid)
            t.act("R", T("R.host"), "feud.open_buzzer", round_id=rid)
            t.act("R", T("p0"), "feud.buzz", round_id=rid)
        t.drain("s0")
        t.drain("sDisp")
        a0 = answers[0]
        t.act("R", T("R.host"), "feud.reveal", {"text": "  " + a0["text"].upper() + " "}, round_id=rid)
        t.act("R", T("R.host"), "feud.reveal", {"text": a0["text"]}, round_id=rid)
        if len(answers) > 1 and answers[1]["aliases"]:
            alias = answers[1]["aliases"][0]
            t.act("R", T("R.host"), "feud.reveal", {"text": unicodedata.normalize("NFKC", alias).swapcase()},
                  round_id=rid)
        t.act("R", T("R.host"), "feud.reveal", {"text": "zzz no such answer"}, round_id=rid)
        t.act("R", T("R.host"), "feud.reveal", {"answerId": "nope"}, round_id=rid)
        t.act("R", T("R.host"), "feud.reveal", {"answerId": answers[-1]["id"]}, round_id=rid)
        t.act("R", T("R.host"), "feud.reveal", {"answerId": answers[-1]["id"], "text": "ignored"}, round_id=rid)
        t.act("R", T("R.host"), "feud.reveal", {"text": ""}, round_id=rid)
        t.act("R", T("R.host"), "feud.steal", {"playerId": t.pid("p3")}, round_id=rid)
        for _ in range(4):
            t.act("R", T("R.host"), "feud.strike", round_id=rid)
        t.drain("sDisp")
        steal_to = "p0" if board % 3 else "p2"
        t.act("R", T("R.host"), "feud.steal", {"playerId": t.snap("R", "R.host")["game"]["control"]}, round_id=rid)
        t.act("R", T("R.host"), "feud.steal", {"playerId": t.pid(steal_to)}, round_id=rid)
        t.act("R", T("R.host"), "feud.strike", round_id=rid)
        if board % 4 == 0:
            t.act("R", T("R.host"), "feud.award", {"playerId": None}, round_id=rid)
            t.act("R", T("R.host"), "feud.award", {"playerId": "p-nobody"}, round_id=rid)
            t.act("R", T("R.host"), "feud.award", {"playerId": t.pid("p1")}, round_id=rid)
        else:
            t.act("R", T("R.host"), "feud.award", round_id=rid)
        t.act("R", T("R.host"), "feud.award", round_id=rid)
        t.act("R", T("R.host"), "feud.reset_buzzer", round_id=rid)
        t.act("R", T("R.host"), "feud.reveal", {"answerId": answers[0]["id"]}, round_id=rid)
        for sid in ("s0", "sDisp", "sOp"):
            t.drain(sid)
        if rnd.random() < 0.3:
            t.state("R", T("p%d" % rnd.randrange(5)))
        out = t.act("R", T("R.host"), "round.next", round_id=rid)
        if out.get("body", {}).get("error"):
            break
        if board > 40:
            break
    t.act("R", T("p1"), "feud.buzz", round_id=t.snap("R", "R.host")["roundId"])
    t.act("R", T("R.host"), "room.end")
    t.act("R", T("R.host"), "feud.open_buzzer")
    t.drain("sDisp")
    t.state("R", T("p4"))
    return t


def g_buzz_race():
    t = Trace("golden-feud-buzz-order", content={"who_am_i": "fixture", "feud": "fixture"},
              note="Buzz ordering under one total receipt order; replays; stale and missing round ids")
    t.create("R")
    for i in range(6):
        t.join("R", "p%d" % i, "Player %d" % i)
    t.act("R", T("R.host"), "room.set_mode", {"mode": "feud"})
    t.act("R", T("R.host"), "room.start")
    rnd = random.Random("buzz")
    for board in range(3):
        rid = t.snap("R", "R.host")["roundId"]
        t.act("R", T("R.host"), "feud.open_buzzer", round_id=rid)
        order = list(range(6))
        rnd.shuffle(order)
        for i in order:
            t.act("R", T("p%d" % i), "feud.buzz", round_id=rid, aid="buzz-%d" % board)
        for i in order[:2]:
            t.act("R", T("p%d" % i), "feud.buzz", round_id=rid, aid="buzz-%d" % board)
        t.act("R", T("p%d" % order[-1]), "feud.buzz", aid="nr-%d" % board)
        t.act("R", T("p%d" % order[-1]), "feud.buzz", round_id="g9b9", aid="st-%d" % board)
        t.act("R", T("R.host"), "feud.reset_buzzer", round_id=rid)
        t.act("R", T("R.host"), "feud.open_buzzer", round_id=rid)
        t.act("R", T("p%d" % order[-1]), "feud.buzz", round_id=rid, aid="buzz-%d" % board)
        t.act("R", T("p%d" % order[-1]), "feud.buzz", round_id=rid, aid="buzz2-%d" % board)
        t.act("R", T("R.host"), "feud.award", round_id=rid)
        t.act("R", T("R.host"), "round.next", round_id=rid)
    return t


def g_errors():
    t = Trace("golden-error-matrix", content={"who_am_i": "fixture", "feud": "fixture"},
              config={"max_players": 5, "action_retention": 4},
              note="Each reachable core error code, plus envelope and body validation")
    t.create("X", raw={"text": "[]"})
    t.create("X", raw={"text": "{\"a\":"})
    t.create("X", raw={"b64": b64(b"\xff\xfe{}")})
    t.create("X", raw={"b64": b64(b"\xef\xbb\xbf{}")})
    t.create("X", raw={"text": "x" * 16385})
    t.create("X", raw={"text": ""})
    t.create("X", {"minPlayers": 2})
    t.create("X", {"minPlayers": 5, "maxPlayers": 4})
    t.create("X", {"minPlayers": 3.0})
    t.create("X", {"maxPlayers": True})
    t.create("X", {"maxPlayers": "12"})
    t.create("X", raw={"text": '{"maxPlayers": 18446744073709551616}'})
    t.create("X", raw={"text": '{"maxPlayers": -0}'})
    t.create("X", {"operatorName": ""})
    t.create("X", {"operatorName": "a" * 25})
    t.create("X", {"operatorName": 7})
    t.create("X", {"operatorName": None, "minPlayers": None})
    t.create("R", {"operatorName": "Op", "minPlayers": 3, "maxPlayers": 4})
    t.join("R", "bad", raw={"text": "{}"})
    t.join("R", "bad", raw={"text": '{"name": "\\u0000x"}'})
    t.join("R", "bad", raw={"text": '{"name": "\\ud800"}'})
    t.join("R", "bad", "⁦Ahmed⁩")
    t.join("R", "bad", "   ")
    t.join("R", "bad", "\U000E0001tag")
    t.join("R", "bad", "private")
    t.join("R", "bad", "͸unassigned")
    t.join("R", "bad", "ࡰpost13")
    t.join("R", "p0", "Ahmed")
    t.join("R", "bad", "AHMED")
    t.join("R", "bad", "Ａｈｍｅｄ")
    t.join("R", "bad", " ahmed ")
    t.join("R", "p1", "Ahmed2")
    t.join("R", "bad", "Ahmed", token=T("R.host"))
    t.join("R", "bad", "x", token={"literal": "short"})
    t.join("R", "p0b", "ignored name", token=T("p0"))
    t.state("R", None)
    t.state("R", {"literal": "A" * 43})
    t.state("R", {"literal": "A" * 129})
    t.state("R", {"literal": "Ab-_" * 8 + "é"})
    a = lambda **kw: t.act("R", T("R.host"), **kw)  # noqa: E731
    a(raw={"empty": True})
    a(raw={"text": ""})
    a(raw={"text": "[1]"})
    a(raw={"text": '{"type": "room.start"}'})
    a(raw={"text": '{"actionId": "", "type": "room.start"}'})
    a(raw={"text": '{"actionId": "%s", "type": "room.start"}' % ("a" * 65)})
    a(raw={"text": '{"actionId": 5, "type": "room.start"}'})
    a(raw={"text": '{"actionId": "e1", "type": "Room.Start"}'})
    a(raw={"text": '{"actionId": "e2", "type": "room"}'})
    a(raw={"text": '{"actionId": "e3", "type": "room.start", "roundId": 5}'})
    a(raw={"text": '{"actionId": "e4", "type": "room.start", "roundId": "bad id"}'})
    a(raw={"text": '{"actionId": "e5", "type": "room.start", "payload": []}'})
    a(raw={"text": '{"actionId": "e5", "type": "room.start", "payload": {}}'})
    a(kind="room.start")
    a(kind="room.set_mode", payload={"mode": "nope"})
    a(kind="room.set_mode", payload={"mode": "werewolf"})
    a(kind="room.set_mode", payload={"mode": ["feud"]})
    a(kind="room.set_mode", payload={"mode": "who_am_i"})
    a(kind="whoami.ready")
    a(kind="unknown.thing")
    a(kind="werewolf.vote")
    a(kind="room.start")
    t.join("R", "p2", "Third")
    t.act("R", T("p0"), "room.start")
    t.act("R", T("R.display"), "room.start")
    t.act("R", T("R.display"), "whoami.ready")
    a(kind="room.start")
    a(kind="room.start")
    t.join("R", "bad", "Fourth")
    rid = t.snap("R", "R.host")["roundId"]
    a(kind="room.set_mode", payload={"mode": "feud"})
    a(kind="feud.open_buzzer", round_id=rid)
    a(kind="whoami.reveal", payload={"playerId": "nobody"})
    a(kind="whoami.reveal", payload={"playerId": "nobody"}, round_id=rid)
    a(kind="whoami.reveal", payload={"playerId": 5}, round_id=rid)
    a(kind="whoami.dismiss", payload={"playerId": t.pid("p1")}, round_id=rid)
    a(kind="whoami.award", payload={"playerId": t.pid("p1"), "points": 2.0}, round_id=rid)
    a(kind="whoami.award", payload={"playerId": t.pid("p1"), "points": -1}, round_id=rid)
    a(kind="whoami.award", payload={"playerId": t.pid("p1"), "points": 0}, round_id=rid)
    a(kind="whoami.award", payload={"playerId": t.pid("p1"), "points": 3}, round_id=rid)
    t.act("R", T("p0"), "whoami.ready", {"ready": "yes"}, round_id=rid)
    t.act("R", T("p0"), "whoami.guess", {"text": None}, round_id=rid)
    t.act("R", T("p0"), "whoami.guess", {"text": ["x"]}, round_id=rid)
    t.act("R", T("p1"), "whoami.guess", {"text": "after award"}, round_id=rid)
    t.act("R", T("p0"), "whoami.award", {"playerId": t.pid("p0")}, round_id=rid)
    t.act("R", T("p0"), "round.next", round_id=rid)
    a(kind="whoami.ready", round_id=rid)
    a(kind="room.pause")
    a(kind="room.pause")
    t.act("R", T("p0"), "whoami.ready", round_id=rid)
    a(kind="room.resume")
    a(kind="room.resume")
    for _ in range(12):
        out = a(kind="round.next")
        if out.get("body", {}).get("error"):
            break
    a(kind="round.next")
    t.act("R", T("p0"), "whoami.ready")
    a(kind="room.reset", payload={"clearScores": 1})
    a(kind="room.reset", payload=None)
    a(kind="round.next")
    a(kind="room.end")
    a(kind="room.set_mode", payload={"mode": "feud"})
    a(kind="room.start")
    rid = t.snap("R", "R.host")["roundId"]
    a(kind="whoami.ready", round_id=rid)
    a(kind="feud.strike", round_id=rid)
    a(kind="feud.steal", payload={"playerId": t.pid("p0")}, round_id=rid)
    a(kind="feud.award", round_id=rid)
    a(kind="feud.reset_buzzer", round_id=rid)
    a(kind="feud.open_buzzer", round_id=rid)
    t.act("R", T("p1"), "feud.buzz", round_id=rid)
    a(kind="feud.open_buzzer", round_id=rid)
    a(kind="feud.steal", payload={"playerId": t.pid("p0")}, round_id=rid)
    a(kind="feud.steal", payload={"playerId": "ghost"}, round_id=rid)
    a(kind="feud.reveal", payload={"answerId": 7}, round_id=rid)
    a(kind="feud.reveal", payload={"text": 7}, round_id=rid)
    a(kind="feud.reveal", payload={"text": "x" * 121}, round_id=rid)
    a(kind="feud.unknown", round_id=rid)
    t.act("R", T("p1"), "feud.strike", round_id=rid)
    for aid in ("s1", "s2", "s3", "s4", "s1"):
        a(kind="feud.strike", round_id=rid, aid=aid)
    a(kind="feud.steal", payload={"playerId": t.pid("p1")}, round_id=rid)
    a(kind="feud.award", payload={"playerId": t.pid("p2")}, round_id=rid)
    a(kind="feud.award", round_id=rid)
    a(kind="feud.reset_buzzer", round_id=rid)
    a(kind="feud.award", round_id=rid, aid="s1")
    while True:
        out = a(kind="round.next")
        if out.get("body", {}).get("error"):
            break
    a(kind="feud.award")
    a(kind="room.end")
    a(kind="room.end")
    a(kind="room.close")
    a(kind="room.start")
    t.act("R", T("p0"), "feud.buzz")
    t.join("R", "bad", "Closed")
    t.join("R", "p0c", token=T("p0"))
    return t


def g_bodies():
    t = Trace("golden-body-semantics", content={"who_am_i": "fixture", "feud": "fixture"},
              note="Python dict/bool/int semantics on bodies: dup keys, floats, -0, NaN, bigints, __proto__")
    t.create("R", raw={"text": '{"operatorName": "A", "operatorName": "B", "minPlayers": 3, "minPlayers": null}'})
    t.create("Q", raw={"text": '{"__proto__": {"x": 1}, "constructor": 1, "toString": null}'})
    t.create("Z", raw={"text": '{"operatorName": NaN}'})
    t.create("Z", raw={"text": '{"minPlayers": Infinity}'})
    t.create("Z", raw={"text": '{"minPlayers": 3e0}'})
    t.create("Z", raw={"text": '{"minPlayers": 1E400}'})
    t.create("Z", raw={"text": '{"minPlayers": -Infinity, "maxPlayers": 12}'})
    t.create("Z", raw={"text": '{"maxPlayers": 12.5}'})
    t.create("Z", raw={"text": ' \t\r\n{"maxPlayers": 12} \n'})
    t.create("Z", raw={"text": '{"maxPlayers": 12} '})
    t.create("Z", raw={"text": '{"maxPlayers": 012}'})
    t.create("Z", raw={"text": '{"maxPlayers": 0x0c}'})
    for i, nm in enumerate(["__proto__", "constructor", "hasOwnProperty", "toString"]):
        t.join("R", "p%d" % i, nm)
    t.join("R", "bad", raw={"text": '{"name": "a", "name": ""}'})
    t.join("R", "bad", raw={"text": '{"name": "ok", "__proto__": null, "name": 5}'})
    a = lambda raw: t.act("R", T("R.host"), raw={"text": raw})  # noqa: E731
    a('{"actionId": "__proto__", "type": "room.set_mode", "payload": {"mode": "who_am_i"}}')
    a('{"actionId": "__proto__", "type": "room.set_mode", "payload": {"mode": "feud"}}')
    a('{"actionId": "constructor", "type": "room.set_mode", "payload": {"mode": "feud", "mode": "who_am_i"}}')
    a('{"actionId": "toString", "type": "room.start", "payload": 0}')
    a('{"actionId": "valueOf", "type": "room.start", "payload": false}')
    a('{"actionId": "hasOwnProperty", "type": "room.start", "payload": ""}')
    a('{"actionId": "x1", "type": "room.start", "payload": []}')
    a('{"actionId": "x2", "type": "room.start", "payload": 0.0}')
    a('{"actionId": "x3", "type": "room.start", "payload": 1}')
    a('{"actionId": "x3b", "type": "room.start", "payload": "s"}')
    a('{"actionId": "x4", "type": "room.start", "roundId": null, "payload": null}')
    rid = t.snap("R", "R.host")["roundId"]
    p = [t.pid("p%d" % i) for i in range(4)]
    a('{"actionId": "w1", "type": "whoami.award", "roundId": "%s", "payload": {"playerId": "%s", "points": -0}}'
      % (rid, p[0]))
    a('{"actionId": "w2", "type": "whoami.award", "roundId": "%s", "payload": {"playerId": "%s", "points": 1.0}}'
      % (rid, p[1]))
    a('{"actionId": "w3", "type": "whoami.award", "roundId": "%s", "payload": {"playerId": "%s", "points": 9007199254740993}}'
      % (rid, p[1]))
    a('{"actionId": "w4", "type": "whoami.award", "roundId": "%s", "payload": {"playerId": "%s", "points": NaN}}'
      % (rid, p[1]))
    a('{"actionId": "w5", "type": "whoami.award", "roundId": "%s", "payload": {"playerId": "%s", "points": 1e1}}'
      % (rid, p[1]))
    a('{"actionId": "w6", "type": "whoami.award", "roundId": "%s", "payload": {"playerId": "%s", "points": false}}'
      % (rid, p[1]))
    a('{"actionId": "w7", "type": "whoami.award", "roundId": "%s", "payload": {"playerId": "%s", "points": 100, "points": 7}}'
      % (rid, p[1]))
    a('{"actionId": "w8", "type": "whoami.reveal", "roundId": "%s", "payload": {"playerId": null}}' % rid)
    a('{"actionId": "w9", "type": "room.reset", "payload": {"clearScores": 0}}')
    a('{"actionId": "w10", "type": "room.end"}')
    a('{"actionId": "w11", "type": "room.reset", "payload": {"clearScores": null}}')
    a('{"actionId": "w12", "type": "room.reset", "payload": {}}')
    t.act("R", T("p0"), raw={"text": '{"actionId": "__proto__", "type": "whoami.ready"}'})
    a('{"actionId": "f1", "type": "room.set_mode", "payload": {"mode": "feud"}}')
    a('{"actionId": "f2", "type": "room.start"}')
    rid = t.snap("R", "R.host")["roundId"]
    a('{"actionId": "f3", "type": "feud.open_buzzer", "roundId": "%s"}' % rid)
    t.act("R", T("p1"), raw={"text": '{"actionId": "__proto__", "type": "feud.buzz", "roundId": "%s"}' % rid})
    a('{"actionId": "f4", "type": "feud.award", "roundId": "%s", "payload": {"playerId": ["%s"]}}' % (rid, p[2]))
    a('{"actionId": "f5", "type": "feud.award", "roundId": "%s", "payload": {"playerId": {"%s": 1}}}' % (rid, p[2]))
    a('{"actionId": "f6", "type": "feud.reveal", "roundId": "%s", "payload": {"answerId": ["x"]}}' % rid)
    a('{"actionId": "f7", "type": "feud.reveal", "roundId": "%s", "payload": {"answerId": null, "text": "testans-b1-mango"}}'
      % rid)
    a('{"actionId": "f8", "type": "feud.steal", "roundId": "%s", "payload": {"playerId": null}}' % rid)
    a('{"actionId": "f9", "type": "feud.award", "roundId": "%s", "payload": {"playerId": "%s"}}' % (rid, p[3]))
    t.state("R", T("p3"))
    t.state("R", T("R.display"))
    return t


def g_lookup():
    t = Trace("golden-code-lookup", content={"who_am_i": "fixture", "feud": "fixture"},
              note="get_room code.upper() semantics: lowercase, ligatures, long s, fullwidth, ß")
    t.create("R")
    code = t.rooms["R"]
    inverse = {}
    for cp in range(0x110000):
        ch = chr(cp)
        if 0xD800 <= cp <= 0xDFFF:
            continue
        up = ch.upper()
        if up != ch and len(up) == 1 and up in M.CODE_ALPHABET + "IO01":
            inverse.setdefault(up, []).append(ch)
    t.lookup(code)
    t.lookup(code.lower())
    t.lookup(" " + code)
    t.lookup(code + "\n")
    t.lookup(code[:5])
    t.lookup(code + "A")
    rnd = random.Random("lookup")
    for _ in range(12):
        t.lookup("".join(rnd.choice(inverse.get(c, [c])) for c in code))
    t.lookup("".join(chr(ord(c) + 0xFEE0) for c in code))
    t.lookup("ß" + code[2:])
    t.lookup("ﬆ" + code[2:])
    for lig, up in (("ß", "SS"), ("ﬆ", "ST"), ("ﬀ", "FF"), ("ﬅ", "ST")):
        t.lookup(lig + lig + lig)
    # make a room whose code contains a pair a ligature uppercases to
    for i in range(40):
        t.create("L%d" % i)
        c = t.rooms["L%d" % i]
        for lig, up in (("ß", "SS"), ("ﬆ", "ST"), ("ﬀ", "FF")):
            if up in c:
                t.lookup(c.replace(up, lig, 1))
    return t


def g_expiry():
    cfg = {"room_idle_ttl": 100.0, "room_max_age": 400.0, "token_ttl": 60.0, "closed_room_grace": 10.0,
           "heartbeat_interval": 7.0, "max_rooms": 4}
    t = Trace("golden-expiry-streams", content={"who_am_i": "fixture", "feud": "fixture"}, config=cfg,
              note="Idle/max-age/token expiry, closed grace, sweep, stream heartbeat/wait/errors, max_rooms")
    t.create("R", now=1000.0)
    for i in range(3):
        t.join("R", "p%d" % i, "P%d" % i, now=1001.0 + i)
    t.open("R", T("p0"), "s0", now=1005.0)
    t.open("R", T("R.display"), "sD", now=1005.0)
    t.drain("s0", now=1005.0)
    t.drain("sD", now=1005.5)
    t.poll("s0", now=1011.9)
    t.poll("s0", now=1012.0)
    t.poll("s0", now=1012.0)
    t.poll("sD", now=1020.0)
    t.poll("sD", now=1020.0)
    t.join("R", "p3", "P3", now=1050.0)
    t.drain("s0", now=1050.5)
    t.state("R", T("p0"), now=1060.9)
    t.state("R", T("p0"), now=1061.0)
    t.poll("s0", now=1061.0)
    t.state("R", T("R.host"), now=1059.0)
    t.state("R", T("p3"), now=1100.0)
    t.act("R", T("p3"), "whoami.ready", now=1105.0)
    t.poll("sD", now=1058.0)
    t.drain("sD", now=1150.0)
    for i in range(3):
        t.create("X%d" % i, now=1150.0 + i)
    t.create("X3", now=1153.0)
    t.lookup(t.rooms["R"], now=1204.9)
    t.sweep(now=1250.0)
    t.create("X3", now=1260.0)
    t.act("X0", T("X0.host"), "room.close", now=1240.0)
    t.open("X0", T("X0.display"), "sX", now=1245.0)
    t.drain("sX", now=1246.0)
    t.state("X0", T("X0.display"), now=1249.9)
    t.poll("sX", now=1250.0)
    t.state("X0", T("X0.display"), now=1250.0)
    for k in range(5):
        t.act("X1", T("X1.host"), "room.set_mode", {"mode": "who_am_i" if k % 2 else "feud"}, now=1240.0 + 99.9 * k)
        t.lookup(t.rooms["X1"], now=1240.0 + 99.9 * k + 50.0)
    t.lookup(t.rooms["X1"], now=1550.9)
    t.state("X1", T("X1.host"), now=1550.95)
    t.lookup(t.rooms["X1"], now=1551.0)
    t.sweep(now=1551.0)
    t.create("Y", now=1500.0)
    t.open("Y", T("Y.host"), "sY", now=1500.0)
    t.drain("sY", now=1500.0)
    for k in range(6):
        t.poll("sY", now=1500.0 + 3.3 * k)
    t.run({"op": "shutdown"})
    t.poll("sY", now=1530.0)
    t.create("Y2", now=1531.0)
    t.sweep(now=1900.0)
    t.sweep(now=1950.0)
    return t


def g_streams():
    cfg = {"max_streams_per_principal": 2, "max_streams_per_room": 4, "max_streams_total": 5,
           "heartbeat_interval": 2.5}
    t = Trace("golden-stream-limits", content={"who_am_i": "fixture", "feud": "fixture"}, config=cfg,
              note="Per-principal, per-room, total stream caps; presence revisions; close order")
    t.create("R")
    t.create("S")
    for i in range(3):
        t.join("R", "p%d" % i, "P%d" % i)
    t.join("S", "q0", "Q0")
    t.open("R", T("p0"), "a")
    t.open("R", T("p0"), "b")
    t.open("R", T("p0"), "c")
    t.open("R", T("p1"), "d")
    t.open("R", T("R.host"), "e")
    t.open("R", T("R.display"), "f")
    t.open("S", T("q0"), "g")
    t.open("S", T("S.host"), "h")
    t.drain("a")
    t.drain("d")
    t.close("a")
    t.drain("d")
    t.close("b")
    t.drain("d")
    t.close("b")
    t.open("S", T("S.host"), "h")
    t.open("R", T("p2"), "i")
    t.drain("i")
    t.act("R", T("R.host"), "room.set_mode", {"mode": "feud"})
    t.drain("i")
    t.drain("d")
    for k in range(5):
        t.poll("i")
    t.close("d")
    t.close("e")
    t.close("i")
    t.close("g")
    t.close("h")
    return t


def g_retention():
    t = Trace("golden-replay-retention", content={"who_am_i": "fixture", "feud": "fixture"},
              config={"action_retention": 3},
              note="actionId dedup per principal, eviction of oldest results, rejected results retained")
    t.create("R")
    for i in range(3):
        t.join("R", "p%d" % i, "P%d" % i)
    for aid in ("m1", "m2", "m1", "m3", "m4", "m1", "m2", "m5", "m1"):
        t.act("R", T("R.host"), "room.set_mode", {"mode": "feud" if aid in ("m1", "m4") else "who_am_i"}, aid=aid)
    t.act("R", T("p0"), "room.start", aid="m1")
    t.act("R", T("p0"), "room.start", aid="m1")
    t.act("R", T("R.host"), "room.start", aid="go")
    rid = t.snap("R", "R.host")["roundId"]
    for aid in ("r", "r", "r2"):
        t.act("R", T("p1"), "whoami.ready", {"ready": aid != "r2"}, round_id=rid, aid=aid)
    t.act("R", T("p2"), "whoami.ready", round_id=rid, aid="r")
    t.act("R", T("R.host"), "room.start", aid="go")
    t.act("R", T("R.host"), "room.end", aid="go")
    return t


def g_content_states():
    traces = []
    wai = json.loads(BLOBS["fixture/who_am_i"])
    small = dict(wai, items=wai["items"][:2])
    variants = [
        ("missing", {"who_am_i": None, "feud": None}),
        ("invalid", {"who_am_i": {"b64": b64(b'{"schemaVersion": 2}')},
                     "feud": {"b64": b64(b"\xef\xbb\xbf" + BLOBS["fixture/feud"])}}),
        ("insufficient", {"who_am_i": {"b64": b64(json.dumps(small).encode())}, "feud": "fixture"}),
    ]
    for name, content in variants:
        t = Trace("golden-content-" + name, content=content, note="room.start against content state " + name)
        t.create("R")
        for i in range(3):
            t.join("R", "p%d" % i, "P%d" % i)
        t.state("R", T("R.host"))
        for mode in ("who_am_i", "feud"):
            t.act("R", T("R.host"), "room.set_mode", {"mode": mode})
            t.act("R", T("R.host"), "room.start")
            t.act("R", T("R.host"), "room.end")
        traces.append(t)
    return traces


def g_whoami_two_rooms():
    t = Trace("golden-two-rooms-isolation", content={"who_am_i": "accepted", "feud": "accepted"},
              note="Two live rooms; tokens of one room never authenticate in the other")
    t.create("A", {"maxPlayers": 3})
    t.create("B")
    for i in range(4):
        t.join("A", "a%d" % i, "Name%d" % i)
        t.join("B", "b%d" % i, "Name%d" % i)
    t.state("A", T("b0"))
    t.state("B", T("A.host"))
    t.act("B", T("A.host"), "room.close")
    for r in ("A", "B"):
        t.act(r, T(r + ".host"), "room.set_mode", {"mode": "who_am_i"})
        t.act(r, T(r + ".host"), "room.start")
    t.state("A", T("a1"))
    t.state("B", T("b1"))
    return t


# Code points unassigned in Unicode 13 that a newer host ICU NFKC-decomposes
# (the L00 "Unicode158" set, e.g. U+A7F1..U+A7F3, U+1E030..U+1E06D). They must
# be rejected explicitly on the wire, exactly like the Python server does.
DRIFT = ["꟱", "ꟲ", "ꟳ", "\U0001E030", "\U0001E06D"]


def g_unicode_drift():
    t = Trace("golden-unicode-drift-wire", content={"who_am_i": "accepted", "feud": "accepted"},
              note="Post-Unicode-13 NFKC drift code points and Arabic combining text on every wire text path")
    t.create("X", {"operatorName": "Host" + DRIFT[0]})
    t.create("R", {"operatorName": "مُضيف"})
    names = ["Al" + DRIFT[0], DRIFT[1], DRIFT[3] + " x", "مَرْيَم", "ﬁona", "Ali" + DRIFT[4]]
    for i, n in enumerate(names):
        t.join("R", "p%d" % i, n)
    t.join("R", "ok0", "Noor")
    t.join("R", "ok1", "نُور")
    t.join("R", "ok2", "Zayd")
    t.act("R", T("R.host"), "room.set_mode", {"mode": "who_am_i"})
    t.act("R", T("R.host"), "room.start")
    rid = t.snap("R", "R.host")["roundId"]
    for i, g in enumerate(DRIFT + ["a" + DRIFT[2] + "b", "أَسَد", "ﬁsh " + DRIFT[3]]):
        t.act("R", T("ok%d" % (i % 3)), "whoami.guess", {"text": g}, round_id=rid)
    t.state("R", T("R.host"))
    t.act("R", T("R.host"), "room.end")
    t.act("R", T("R.host"), "room.reset", {"clearScores": True})
    t.act("R", T("R.host"), "room.set_mode", {"mode": "feud"})
    t.act("R", T("R.host"), "room.start")
    snap = t.snap("R", "R.host")
    rid = snap["roundId"]
    a0 = snap["game"]["answers"][0]["text"]
    for g in DRIFT + [a0 + DRIFT[0], DRIFT[1] + a0, unicodedata.normalize("NFD", a0), a0]:
        t.act("R", T("R.host"), "feud.reveal", {"text": g}, round_id=rid)
    t.state("R", T("R.display"))
    t.state("R", T("ok1"))
    return t


# ---------------------------------------------------------- adaptive fuzzer

GUESS_POOL = ["Lion", "أسد", "  lion  ", "LION", "ﬁsh", "Ｌｉｏｎ", "á", "á", "x​y", "\U0001D400",
              "الشَّباب", "الشَّباب",
              "ﻻ", "لا", "Straße", "STRASSE", "ΣΙΣ", "ẞ", "İstanbul",
              " 　x", "\x85y", "a b", "", " ", "‮x", "\u0000", "\ud800", "", "ࡰ",
              "\U0001FAE0", "x" * 80, "x" * 81, "م" * 120, "م" * 121, "ﷺ", "㎒", "가",
              "각", "ệ", "ệ", "Ω", "K", "K"]


def rand_name(rnd):
    pool = ["Ali", "ALI", "Noor", "نور", "مَريم", "Zayd", "Fatima", "Ｆａｔｉｍａ", "Omar", "Huda", "Åsa",
            "Åsa", "Mo  ham", "x" * 24, "x" * 25, "  ", "‮RTL", "\U0001F600", "ß", "ss", "SS",
            "İ", "i̇", "Layla", "Sara", "Hamad", "Reem"]
    name = rnd.choice(pool)
    if rnd.random() < 0.5:
        name += str(rnd.randrange(100))
    return name


def rand_junk(rnd):
    return rnd.choice([None, True, False, 0, -1, 1, 2, 101, 1001, 1.5, -0.0, "", "x", [], {}, ["a"], {"a": 1},
                       2 ** 64, -2 ** 63, float("nan"), float("inf"), "\U0001F981", "__proto__"])


def fuzz_trace(n, master):
    rnd = random.Random("%s:%d" % (master, n))
    cfg = {}
    if rnd.random() < 0.5:
        cfg["action_retention"] = rnd.choice([1, 2, 5, 128])
    if rnd.random() < 0.15:
        cfg.update({"room_idle_ttl": rnd.choice([60.0, 300.0]), "token_ttl": rnd.choice([90.0, 600.0]),
                    "closed_room_grace": rnd.choice([0.0, 5.0])})
    if rnd.random() < 0.3:
        cfg.update({"max_streams_per_principal": rnd.choice([1, 2]), "max_streams_per_room": rnd.choice([2, 5]),
                    "max_streams_total": rnd.choice([3, 8]), "heartbeat_interval": rnd.choice([1.0, 4.0])})
    if rnd.random() < 0.2:
        cfg["max_rooms"] = rnd.choice([1, 2])
    content = rnd.choice(["fixture", "fixture", "accepted"])
    t = Trace("fuzz-%04d" % n, content={"who_am_i": content, "feud": content}, config=cfg,
              seed="fuzz:%s:%d" % (master, n), note="adaptive randomized ops")
    rooms, players, streams = [], {}, []
    chaos = rnd.choice([0.05, 0.15, 0.4])  # share of deliberately wrong actor/round/payload choices
    steps = rnd.randrange(60, 220)
    for _ in range(steps):
        t.now += rnd.choice([0.0, 0.1, 0.5, 1.0, 2.0, 5.0]) if rnd.random() < 0.97 else rnd.choice([30.0, 120.0, 700.0])
        live = [r for r in rooms if t.rooms[r] in t.mgr._rooms]
        if not live or rnd.random() < 0.02:
            ref = "R%d" % len(rooms)
            body = {}
            if rnd.random() < 0.5:
                body["operatorName"] = rand_name(rnd)
            if rnd.random() < 0.3:
                body["minPlayers"] = rnd.choice([3, 3, 4, 2, 13, None, rand_junk(rnd)])
            if rnd.random() < 0.3:
                body["maxPlayers"] = rnd.choice([4, 6, 12, 3, rand_junk(rnd)])
            out = t.create(ref, body, now=t.now)
            if out.get("status") == 201:
                rooms.append(ref)
                players[ref] = []
            continue
        room = live[-1] if rnd.random() < 0.8 else rnd.choice(rooms)
        host, disp = T(room + ".host"), T(room + ".display")
        plist = players[room]
        snap = t.snap(room, room + ".host") if t.rooms[room] in t.mgr._rooms else None
        phase = snap["phase"] if snap else None
        mode = snap["mode"] if snap else None
        game = (snap or {}).get("game") or {}
        rid = snap["roundId"] if snap else None
        wrong = rnd.random() < chaos
        if wrong:
            rid = rnd.choice([None, "g1r1", "g1b1", "g0r0", "bad id", rid])
        ptok = (lambda: T(rnd.choice(plist))) if plist else (lambda: host)
        pids = [t.pid(p) for p in plist]
        r = rnd.random()
        if phase == "lobby" and len(plist) < 4 and r < 0.5:
            r = 0.0
        if r < 0.08:
            ref = "%s.p%d" % (room, len(plist))
            if rnd.random() < 0.1 and plist:
                t.join(room, None, token=T(rnd.choice(plist)), now=t.now)
            else:
                out = t.join(room, ref, rand_name(rnd), now=t.now)
                if out.get("status") == 201:
                    plist.append(ref)
            continue
        if r < 0.13:
            sid = "s%d" % len(streams)
            out = t.open(room, rnd.choice([host, disp, ptok()]), sid, now=t.now)
            if out.get("opened"):
                streams.append(sid)
            continue
        if r < 0.25 and streams:
            sid = rnd.choice(streams)
            if t.sessions[sid]["open"]:
                if rnd.random() < 0.15:
                    t.close(sid)
                else:
                    t.poll(sid, now=t.now)
            continue
        if r < 0.29:
            t.state(room, rnd.choice([host, disp, ptok(), ptok(), {"literal": "Q" * 43}]), now=t.now)
            continue
        if r < 0.30:
            t.sweep(now=t.now)
            continue
        if r < 0.31:
            t.lookup(rnd.choice([t.rooms[room], t.rooms[room].lower(), "ZZZZZZ"]), now=t.now)
            continue
        actor, payload = host, None
        if phase in ("lobby", "ended") or phase is None:
            kind = rnd.choices(["room.set_mode", "room.start", "room.reset", "room.close", "round.next"],
                               [5 if phase == "ended" or mode is None else 2, 8, 2, 0.05, 0.5])[0]
        elif phase == "paused":
            kind = rnd.choices(["room.resume", "room.end", "whoami.ready", "feud.buzz"], [8, 1, 1, 1])[0]
        elif phase == "closed":
            if rnd.random() < 0.7:
                out = t.create("R%d" % len(rooms), {}, now=t.now)
                if out.get("status") == 201:
                    rooms.append("R%d" % len(rooms))
                    players[rooms[-1]] = []
                continue
            kind = rnd.choice(["room.start", "room.reset", "feud.buzz"])
        elif mode == "who_am_i":
            pending = game.get("pendingGuesses") or []
            kind = rnd.choices(
                ["whoami.ready", "whoami.guess", "whoami.dismiss", "whoami.award", "whoami.reveal", "round.next",
                 "room.pause", "room.end", "room.set_mode"],
                [4, 6, 3 if pending else 0.3, 4 if pending else 0.5, 2, 3, 0.4, 0.4, 0.2])[0]
            if kind in ("whoami.ready", "whoami.guess"):
                actor = ptok()
            if kind in ("whoami.dismiss", "whoami.award"):
                target = rnd.choice(pending) if pending and not wrong else rnd.choice(pids + ["pghost"])
                payload = {"playerId": target}
                if kind == "whoami.award" and rnd.random() < 0.4:
                    payload["points"] = rnd.choice([1, 2, 5, 100, 0, 101, -1, True, 2.0])
            elif kind == "whoami.reveal":
                payload = rnd.choice([{}, {"playerId": rnd.choice(pids + ["pghost"])}]) if pids else {}
            elif kind == "whoami.guess":
                payload = {"text": rnd.choice(GUESS_POOL)}
            elif kind == "whoami.ready":
                payload = rnd.choice([None, {}, {"ready": True}, {"ready": False}, {"ready": 1}])
        else:  # feud
            buzzer = game.get("buzzer") or {}
            kind = rnd.choices(
                ["feud.open_buzzer", "feud.buzz", "feud.reveal", "feud.strike", "feud.steal", "feud.award",
                 "feud.reset_buzzer", "round.next", "room.pause", "room.end"],
                [3 if not buzzer.get("open") else 0.3, 5 if buzzer.get("open") else 1, 6, 4,
                 3 if game.get("strikes", 0) >= 3 else 0.3, 2, 1, 2, 0.3, 0.3])[0]
            if kind == "feud.buzz":
                actor = ptok()
            elif kind == "feud.reveal":
                answers = game.get("answers") or []
                c = rnd.random()
                if answers and c < 0.5:
                    a = rnd.choice(answers)
                    text = rnd.choice([a["text"]] + a["aliases"])
                    text = rnd.choice([text, text.upper(), text.lower(), "  %s  " % text,
                                       unicodedata.normalize("NFKD", text), text.replace(" ", "　"),
                                       "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in text)])
                    payload = {"text": text}
                elif answers and c < 0.8:
                    payload = {"answerId": rnd.choice(answers)["id"]}
                else:
                    payload = rnd.choice([{"text": rnd.choice(GUESS_POOL)}, {"answerId": rand_junk(rnd)}, {}])
            elif kind in ("feud.steal", "feud.award"):
                payload = rnd.choice([None, {"playerId": rnd.choice(pids + ["pghost", None])}]) if pids else None
        if kind == "room.set_mode":
            payload = {"mode": rnd.choice(["who_am_i", "feud"] * 4 + ["bluff", "x", rand_junk(rnd)])}
        elif kind == "room.reset":
            payload = rnd.choice([None, {}, {"clearScores": True}, {"clearScores": rand_junk(rnd)}])
        if wrong:
            if rnd.random() < 0.5:
                actor = rnd.choice([host, disp, ptok()])
            else:
                payload = rnd.choice([rand_junk(rnd), {"playerId": rand_junk(rnd)}, {"text": rand_junk(rnd)}])
        aid = rnd.choice(["x%d" % rnd.randrange(8)] + [t.next_aid()] * 4)
        if rnd.random() < 0.03:
            raw = rnd.choice([{"text": "{"}, {"empty": True}, {"text": '{"actionId": "%s"}' % aid},
                              {"b64": b64(b'{"actionId": "a", "type": "room.start", "x": "\xc3"}')},
                              body_json({"actionId": aid, "type": kind, "payload": rand_junk(rnd)})])
            t.act(room, actor, raw=raw, now=t.now)
        else:
            t.act(room, actor, kind, payload, aid=aid, round_id=rid, now=t.now)
        if rnd.random() < 0.005:
            t.run({"op": "shutdown"})
    for sid in streams:
        if t.sessions[sid]["open"]:
            t.drain(sid, now=t.now)
    return t


# ------------------------------------------------------------ text corpora

INTERESTING = (
    list(range(0x0621, 0x0656)) + [0x0670, 0x0640, 0x200C, 0x200D, 0x061C, 0x200F, 0x200E, 0x00A0, 0x3000, 0x2028,
                                   0x2029, 0x0085, 0x001C, 0x001F, 0x0009, 0x000B, 0x180E, 0x200B, 0xFEFF, 0x00AD]
    + list(range(0x0300, 0x0346)) + list(range(0x0591, 0x05A0)) + list(range(0xFB50, 0xFB60))
    + list(range(0xFEF5, 0xFEFD)) + [0xFDF2, 0xFDFA, 0xFDFB, 0xFDFC] + list(range(0x1100, 0x1104))
    + list(range(0x1161, 0x1164)) + list(range(0x11A8, 0x11AB)) + [0xAC00, 0xAC01, 0xD7A3, 0x3131]
    + [0x00DF, 0x1E9E, 0x03A3, 0x03C2, 0x03C3, 0x0130, 0x0131, 0x0049, 0x0069, 0x212A, 0x2126, 0x00C5, 0x212B]
    + list(range(0xFB00, 0xFB07)) + list(range(0xFF21, 0xFF26)) + list(range(0xFF41, 0xFF46))
    + [0x1D400, 0x1D7CE, 0x1F600, 0x1F981, 0x20000, 0x2F800, 0x2FFFF, 0x1FAE0, 0x0870, 0x08B5, 0x1F6DD, 0x10FFFF,
       0xE000, 0xF0000, 0x0378, 0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0x202A, 0x202E, 0x2066, 0x2069, 0x0000, 0x007F,
       0x0041, 0x0061, 0x0020, 0x0032, 0x0039, 0x1E00, 0x0F73, 0x0F75, 0x0F81, 0x0344, 0x0958, 0x2ADC, 0x1D15E,
       0x1D160, 0x0CCB, 0x0DDD, 0x1100, 0x302A, 0x0345, 0x1F80, 0x0390, 0x1FD3, 0xFB1D, 0xFB4E, 0x3392, 0x33FF,
       0x2460, 0x24B6, 0x1F130, 0x2153, 0x00BD, 0x01C4, 0x01C5, 0x01C6, 0x0587, 0xFB13, 0x1E96, 0x1F50, 0x0149]
)


def text_cases(rnd, content_strings, count):
    cases = list(content_strings)
    for s in content_strings[:400]:
        cases += [s.upper(), s.lower(), " %s " % s, unicodedata.normalize("NFD", s),
                  unicodedata.normalize("NFKD", s), s.replace(" ", "　  ")]
    cases += [g for g in GUESS_POOL if isinstance(g, str)]
    harakat = list(range(0x064B, 0x0653)) + [0x0670]
    for _ in range(count):
        style = rnd.random()
        if style < 0.3:
            n = rnd.randrange(1, 12)
            s = ""
            for _ in range(n):
                s += chr(rnd.choice(range(0x0621, 0x064B)))
                for _ in range(rnd.randrange(0, 4)):
                    s += chr(rnd.choice(harakat))
            cases.append(s)
        elif style < 0.55:
            cases.append("".join(chr(rnd.choice(INTERESTING)) for _ in range(rnd.randrange(0, 14))))
        elif style < 0.7:
            base = rnd.choice("aeiouAEOUsSابαᄀ")
            marks = [rnd.choice(list(range(0x0300, 0x0370)) + harakat + [0x0670, 0x05B0, 0x0E38, 0x302A])
                     for _ in range(rnd.randrange(1, 6))]
            cases.append(base + "".join(chr(m) for m in marks))
        elif style < 0.85:
            out = []
            for _ in range(rnd.randrange(0, 10)):
                cp = rnd.randrange(0x110000) if rnd.random() < 0.5 else rnd.randrange(0x3000)
                out.append(chr(cp))
            cases.append("".join(out))
        else:
            words = [rnd.choice(content_strings) for _ in range(rnd.randrange(1, 4))]
            sep = rnd.choice([" ", "  ", "\t", " ", "　", "\n", "\x1c", "​", ""])
            cases.append(sep.join(words))
    return cases


def split_pair(s):
    return any(0xD800 <= ord(a) <= 0xDBFF and 0xDC00 <= ord(b) <= 0xDFFF for a, b in zip(s, s[1:]))


def text_record(s):
    def clean(n):
        return TU.clean_text(s, n)
    upper = s.upper()
    return {"nfc": unicodedata.normalize("NFC", s), "nfd": unicodedata.normalize("NFD", s),
            "nfkc": unicodedata.normalize("NFKC", s), "nfkd": unicodedata.normalize("NFKD", s),
            "casefold": s.casefold(), "matchKey": TU.match_key(s), "split": s.split(), "strip": s.strip(),
            "len": len(s), "clean24": clean(24), "clean80": clean(80), "clean120": clean(120),
            "isText500": C.is_text(s), "code": upper if M.CODE_RE.fullmatch(upper) else None}


def sweep_record():
    """Per code point, everything that is not the identity, plus clean() rejection ranges."""
    entries, rejected, start = [], [], None
    for cp in range(0x110000):
        ch = chr(cp)
        e = {}
        for key, val in (("nfc", unicodedata.normalize("NFC", ch)), ("nfkc", unicodedata.normalize("NFKC", ch)),
                         ("nfd", unicodedata.normalize("NFD", ch)), ("nfkd", unicodedata.normalize("NFKD", ch)),
                         ("casefold", ch.casefold()), ("matchKey", TU.match_key(ch))):
            if val != ch:
                e[key] = [ord(c) for c in val]
        up = ch.upper()
        if up != ch and all(c in CODE_CHARS for c in up):
            e["code"] = [ord(c) for c in up]
        if e:
            e["cp"] = cp
            entries.append(e)
        bad = TU.clean_text(ch, 24) is None
        if bad and start is None:
            start = cp
        elif not bad and start is not None:
            rejected.append([start, cp - 1])
            start = None
    if start is not None:
        rejected.append([start, 0x10FFFF])
    return {"entries": entries, "cleanRejected": rejected}


# ------------------------------------------------------------ JSON corpora

JSON_SEEDS = [
    b'{}', b'{"a":1}', b'{"a":[1,2,{"b":null}],"c":"d"}', b'{"x":-0,"y":-0.0,"z":0e0}', b'{"n":1e400,"m":-1e400}',
    b'{"big":123456789012345678901234567890}', b'{"neg":-9007199254740993}', b'{"f":1.5e-320}',
    b'{"s":"\\ud83e\\udd81 \\u0041\\u00e9 \\ud800 \\udc00\\ud800"}', b'{"e":"\\/\\b\\f\\n\\r\\t\\"\\\\"}',
    b'{"1":1,"0":0,"b":2,"a":3,"10":4,"-1":5,"01":6}', b'{"a":1,"a":2,"b":3,"a":4}',
    b'{"__proto__":{"polluted":1},"constructor":{"prototype":1}}', b'{"k":NaN,"l":Infinity,"m":-Infinity}',
    b' \t\n\r{ "a" : [ ] , "b" : { } } \r\n', b'{"u":"\xd8\xa3\xd8\xad\xd9\x85\xd8\xaf"}',
    b'{"a":true,"b":false,"c":null}', b'{"deep":[[[[[[[[[[1]]]]]]]]]]}', b'{"a":1.0,"b":100,"c":1E2,"d":1e-2}',
    b'{"x":4.35,"y":0.1,"z":5e-324,"w":1.7976931348623157e308}',
]
JSON_BAD = [
    b'', b' ', b'[]', b'"x"', b'1', b'null', b'{', b'{"a"}', b'{"a":}', b'{"a":1,}', b'{,}', b'{"a":1 "b":2}',
    b"{'a':1}", b'{"a":01}', b'{"a":1.}', b'{"a":.5}', b'{"a":+1}', b'{"a":0x1}', b'{"a":nan}', b'{"a":inf}',
    b'{"a":-Inf}', b'{"a":tru}', b'{"a":"\x01"}', b'{"a":"\\x41"}', b'{"a":"\\u12"}', b'{"a":"\\u12G4"}',
    b'{"a":"\\ud800\\u12"}', b'{"a":1}x', b'{"a":1}{}', b'\xef\xbb\xbf{}', b'\xef\xbb\xbf\xef\xbb\xbf{}',
    b'{"a":"\xc0\x80"}', b'{"a":"\xed\xa0\x80"}', b'{"a":"\xf4\x90\x80\x80"}', b'{"a":"\xe2\x82"}', b'\xc3',
    b'{"a":"\xf0\x9f\x98"}', b'{"a":"\x80"}', b'{"a":"\xfe"}', b'{"a":1}\x00', b'\x00{}', b'{"a":"b"\xc2\xa0}',
    b'{"a":-}', b'{"a":-a}', b'{"a":1e}', b'{"a":1e+}', b'{"a":1E-}', b'{"\\ud800":1}', b'{"a":[1,]}',
    b'{"a":[,1]}', b'{"a":[1 2]}', b'{"a":"\t"}', b'{"a":"\x7f"}', b'{"a" :1}', b'{"a":1\x0c}', b'{"a":"\\U0041"}',
]


def json_cases(rnd, count):
    cases = list(JSON_SEEDS) + list(JSON_BAD)
    pieces = [b"{", b"}", b"[", b"]", b",", b":", b'"', b"\\", b"u", b"0", b"1", b"-", b".", b"e", b"E", b"+",
              b" ", b"\n", b"\t", b"null", b"true", b"NaN", b"Infinity", b"\xc3\xa9", b"\xd8", b"\xff", b"\\u00",
              b"\\ud83e", b"\\udd81", b'"a"', b"9" * 20, b"\x00", b"\xef\xbb\xbf"]
    for _ in range(count):
        base = bytearray(rnd.choice(JSON_SEEDS))
        for _ in range(rnd.randrange(1, 5)):
            op = rnd.random()
            pos = rnd.randrange(len(base) + 1)
            if op < 0.35:
                base[pos:pos] = rnd.choice(pieces)
            elif op < 0.6 and base:
                del base[min(pos, len(base) - 1)]
            elif base:
                base[min(pos, len(base) - 1)] = rnd.randrange(256)
        cases.append(bytes(base))
    for _ in range(count // 4):
        value = rand_json_value(rnd, 0)
        cases.append(json.dumps({"v": value, "k": rand_json_value(rnd, 0)},
                                ensure_ascii=rnd.random() < 0.5).encode("utf-8", "surrogatepass"))
    return cases


def rand_json_value(rnd, depth):
    r = rnd.random()
    if depth > 4 or r < 0.4:
        return rnd.choice([None, True, False, 0, -0.0, 1, -1, 2 ** 53, 2 ** 53 + 1, -(2 ** 63), 2 ** 70, 0.1, 1e300,
                           1e-320, float("nan"), float("inf"), "", "x", "مَ", "\U0001F981", "\ud800",
                           "\x00\x1f", "__proto__", rnd.random(), rnd.randrange(-10 ** 6, 10 ** 6)])
    if r < 0.7:
        return [rand_json_value(rnd, depth + 1) for _ in range(rnd.randrange(4))]
    out = {}
    for _ in range(rnd.randrange(4)):
        out[rnd.choice(["a", "b", "1", "0", "__proto__", "constructor", "م", "", "toString"])] = \
            rand_json_value(rnd, depth + 1)
    return out


def json_record(data, config):
    try:
        return {"value": tag(read_json(config, {"b64": b64(data)}, False))}
    except M.PartyError as exc:
        return {"error": exc.code}


def depth_record(depth, config):
    data = b'{"a":' + b"[" * depth + b"]" * depth + b"}"
    out = {}
    box = {}

    def target():
        try:
            read_json(config, {"b64": b64(data)}, False)
            box["r"] = "ok"
        except M.PartyError as exc:
            box["r"] = exc.code
    th = threading.Thread(target=target)
    th.start()
    th.join()
    out["thread"] = box["r"]
    return out


# --------------------------------------------------------- content corpora

def content_cases(rnd, count):
    cases = []
    for mode in ("who_am_i", "feud"):
        for src in ("accepted", "fixture"):
            cases.append((mode, BLOBS[src + "/" + mode]))
        cases.append((mode, None))
        cases.append((mode, b"\xef\xbb\xbf" + BLOBS["fixture/" + mode]))
        cases.append((mode, b"\xef\xbb\xbf\xef\xbb\xbf" + BLOBS["fixture/" + mode]))
        cases.append((mode, BLOBS["fixture/" + mode].replace(b'"easy"', b"NaN", 1).replace(b"40", b"Infinity", 1)))
        cases.append((mode, BLOBS["fixture/" + mode] + b"\xff"))
        cases.append((mode, b" " * (2 * 1024 * 1024 - 1) + b"{}"))
        cases.append((mode, b" " * (2 * 1024 * 1024 + 1)))
        cases.append((mode, b"[]"))
        cases.append((mode, b"{}"))
        cases.append((mode, b'{"schemaVersion": true}'))
    for _ in range(count):
        mode = rnd.choice(["who_am_i", "feud"])
        src = rnd.choice(["accepted", "fixture", "fixture"])
        data = json.loads(BLOBS[src + "/" + mode].decode("utf-8"))
        for _ in range(rnd.randrange(1, 4)):
            mutate_tree(rnd, data)
        text = json.dumps(data, ensure_ascii=rnd.random() < 0.3)
        cases.append((mode, text.encode("utf-8", "surrogatepass")))
    return cases


def mutate_tree(rnd, data):
    path, node = [], data
    while isinstance(node, (dict, list)) and node and rnd.random() < 0.75:
        key = rnd.choice(list(node.keys())) if isinstance(node, dict) else rnd.randrange(len(node))
        path.append((node, key))
        node = node[key]
    if not path:
        return
    parent, key = path[-1]
    r = rnd.random()
    if r < 0.2 and isinstance(parent, dict):
        del parent[key]
    elif r < 0.3 and isinstance(parent, list):
        parent.append(copy.deepcopy(parent[key]))
    elif r < 0.4 and isinstance(parent, list):
        del parent[key]
    elif r < 0.55 and isinstance(node, str):
        parent[key] = rnd.choice(["", " ", "　", node * 9, node + "́", node.upper(), "x" * 501,
                                  "x" * 65, "x" * 121, "x" * 201, "House answers", "house answers",
                                  "\ud800", unicodedata.normalize("NFKD", node) + "ﬁ"])
    elif r < 0.7 and isinstance(node, int) and not isinstance(node, bool):
        parent[key] = rnd.choice([0, -1, 100, 101, 1000, 1001, True, 1.0, 2 ** 64, None, "1"])
    elif r < 0.8 and isinstance(parent, dict):
        parent[rnd.choice(["extra", "ar", "aliases", "difficulty", "id"])] = rand_json_value(rnd, 3)
    else:
        parent[key] = rand_json_value(rnd, 3)
    if isinstance(parent, dict) and rnd.random() < 0.05:
        parent["schemaVersion"] = 1


def content_record(mode, data):
    tmp = tempfile.mkdtemp(prefix="l01a-content-")
    try:
        if data is not None:
            with open(os.path.join(tmp, mode + ".json"), "wb") as handle:
                handle.write(data)
        spec = MODES.REGISTRY[mode]
        st = C.load_mode(tmp, mode, spec.module.validate_item, getattr(spec.module, "validate_items", None))
        return {"status": st.status, "issues": st.issues, "pack": st.pack}
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------------------------ main

BLOBS = {}


def write_jsonl(path, records):
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=True, allow_nan=False) + "\n")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("oracle")
    ap.add_argument("out")
    ap.add_argument("--fuzz", type=int, default=200)
    ap.add_argument("--seed", default="l01a-2026-10-24")
    ap.add_argument("--text", type=int, default=6000)
    ap.add_argument("--json", type=int, default=4000)
    ap.add_argument("--content", type=int, default=600)
    ARGS = ap.parse_args()
    load_oracle(os.path.abspath(ARGS.oracle))
    if unicodedata.unidata_version != "13.0.0":
        sys.exit("oracle runtime must be unicodedata 13.0.0")
    for src, base in (("accepted", "party/content"), ("fixture", "tests/fixtures/content")):
        for mode in ("who_am_i", "feud"):
            with open(os.path.join(ARGS.oracle, base, mode + ".json"), "rb") as handle:
                BLOBS[src + "/" + mode] = handle.read()
    os.makedirs(ARGS.out, exist_ok=True)
    blob_header = {"blobs": {k: b64(v) for k, v in BLOBS.items()},
                   "blobSha256": {k: hashlib.sha256(v).hexdigest() for k, v in BLOBS.items()},
                   "python": sys.version.split()[0], "unidata": unicodedata.unidata_version,
                   "recorder": "oracle_record.py", "masterSeed": ARGS.seed}

    def room_suite(name, traces):
        def gen():
            yield dict(blob_header, suite=name)
            for t in traces:
                for rec in t.records():
                    yield rec
        write_jsonl(os.path.join(ARGS.out, name + ".py.jsonl"), gen())
        print("suite %s: %d traces, %d ops" % (name, len(traces), sum(len(t.ops) for t in traces)))

    golden = [g_whoami_full("accepted"), g_whoami_full("fixture"), g_feud_full("accepted"), g_feud_full("fixture"),
              g_buzz_race(), g_errors(), g_bodies(), g_lookup(), g_expiry(), g_streams(), g_retention(),
              g_whoami_two_rooms(), g_unicode_drift()] + g_content_states()
    room_suite("golden", golden)
    room_suite("fuzz", [fuzz_trace(n, ARGS.seed) for n in range(ARGS.fuzz)])

    rnd = random.Random(ARGS.seed + ":text")
    strings = []
    for src in ("accepted", "fixture"):
        for mode in ("who_am_i", "feud"):
            def walk(v):
                if isinstance(v, str):
                    strings.append(v)
                elif isinstance(v, list):
                    for x in v:
                        walk(x)
                elif isinstance(v, dict):
                    for x in v.values():
                        walk(x)
            walk(json.loads(BLOBS[src + "/" + mode].decode("utf-8")))
    strings = sorted(set(strings))
    cases = text_cases(rnd, strings, ARGS.text)
    def text_line(s):
        rec = {"in": s, "py": text_record(s)}
        if split_pair(s):
            # A Python str holding a lone high surrogate followed by a lone low
            # surrogate has no JS string form (JS joins them into one astral
            # code point), and json.loads already joins such escapes on the
            # wire. Kept in the file, compared as a declared category.
            rec["jsUnrepresentable"] = True
        return rec
    write_jsonl(os.path.join(ARGS.out, "text.py.jsonl"),
                [{"suite": "text", "cases": len(cases)}] + [text_line(s) for s in cases])
    print("suite text: %d strings (%d from content)" % (len(cases), len(strings)))
    write_jsonl(os.path.join(ARGS.out, "sweep.py.jsonl"), [{"suite": "sweep"}, {"py": sweep_record()}])
    print("suite sweep: done")

    rnd = random.Random(ARGS.seed + ":json")
    cfg = M.ServerConfig()
    jc = json_cases(rnd, ARGS.json)
    write_jsonl(os.path.join(ARGS.out, "json.py.jsonl"),
                [{"suite": "json"}] + [{"b64": b64(d), "py": json_record(d, cfg)} for d in jc])
    print("suite json: %d bodies" % len(jc))
    depths = [1, 10, 100, 500, 800, 899, 900, 901, 950] + list(range(955, 1005, 5)) + [1100, 2000, 5000]
    write_jsonl(os.path.join(ARGS.out, "depth.py.jsonl"),
                [{"suite": "depth"}] + [{"depth": d, "py": depth_record(d, cfg)} for d in depths])

    rnd = random.Random(ARGS.seed + ":content")
    cc = content_cases(rnd, ARGS.content)
    write_jsonl(os.path.join(ARGS.out, "content.py.jsonl"),
                [dict(blob_header, suite="content")] +
                [{"mode": m, "b64": None if d is None else b64(d), "py": content_record(m, d)} for m, d in cc])
    print("suite content: %d packs" % len(cc))

    rnd = random.Random(ARGS.seed + ":shuffle")
    seeds = [0, 1, 2 ** 32 - 1, 2 ** 32, 2 ** 63, 2 ** 64 - 1] + [rnd.getrandbits(64) for _ in range(500)]
    recs = [{"suite": "shuffle"}]
    for i, s in enumerate(seeds):
        n = [1, 2, 3, 7, 12, 24, 64, 84, 300][i % 9]
        x = list(range(n))
        random.Random(s).shuffle(x)
        recs.append({"seed": str(s), "n": n, "py": x})
    write_jsonl(os.path.join(ARGS.out, "shuffle.py.jsonl"), recs)
    write_jsonl(os.path.join(ARGS.out, "errors.py.jsonl"),
                [{"suite": "errors"}, {"py": {k: list(v) for k, v in E.ERRORS.items()}}])
    print("suites shuffle/errors: done")


if __name__ == "__main__":
    main()
