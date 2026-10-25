"""In-memory rooms, capability credentials, action replay, presence, expiry.

Concurrency model
-----------------
* ``RoomManager._lock`` guards the room table only.
* Each ``Room`` has one lock (wrapped in a Condition). Every mutation of that
  room (join, action, presence) happens under it. Each action receives the
  next value of the room's monotonic receipt sequence while holding the lock,
  so actions, including racing ``feud.buzz`` requests, are applied in one
  total order.
* Lock order is manager -> room -> stream-counter. Nothing acquires the
  manager lock while holding a room lock.
* Time is read only after the room lock is held. A request that waited on the
  lock past the room's or token's expiry is rejected, never applied.
* Expiry is decided and marked under that room's lock. The manager table
  (get_room / create_room / sweep) takes manager -> room, so a removal can
  never act on a stale ``last_activity`` read while a mutation was committing.
* No socket I/O ever happens under a lock. Event streams build their
  projection under the room lock, release it, and then write. A slow client
  blocks only its own thread.

All state is process memory. Restarting the process loses every room.
"""

import collections
import hashlib
import re
import secrets
import threading
import time

from . import room_logic
from .errors import PartyError, error_body, status_for
from .textutil import clean_name

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
# Always applied with fullmatch(): "$" alone would accept a trailing newline.
CODE_RE = re.compile(r"[A-Z2-9]{6}")
ACTION_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,64}")
ACTION_TYPE_RE = re.compile(r"[a-z_]{1,24}(?:\.[a-z_]{1,32}){1,2}")
ROUND_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,32}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{32,128}")


class ServerConfig(object):
    """Server limits. All durations are seconds."""

    DEFAULTS = {
        "min_players": 3,
        "max_players": 12,
        "room_idle_ttl": 2 * 3600.0,
        "room_max_age": 12 * 3600.0,
        "token_ttl": 12 * 3600.0,
        "closed_room_grace": 60.0,
        "max_rooms": 64,
        "action_retention": 128,
        "heartbeat_interval": 15.0,
        "sweep_interval": 30.0,
        "sse_write_timeout": 10.0,
        "request_timeout": 15.0,
        "max_body_bytes": 16 * 1024,
        "max_streams_per_principal": 4,
        "max_streams_per_room": 64,
        "max_streams_total": 256,
        "max_connections": 320,
        "request_rate": 30.0,
        "request_burst": 120,
        "create_per_minute": 10.0,
        "create_burst": 10,
        "join_per_minute": 60.0,
        "join_burst": 30,
        "action_rate": 10.0,
        "action_burst": 30,
        "access_log": False,
    }

    def __init__(self, **overrides):
        unknown = set(overrides) - set(self.DEFAULTS)
        if unknown:
            raise ValueError("unknown config keys: %s" % ", ".join(sorted(unknown)))
        values = dict(self.DEFAULTS)
        values.update(overrides)
        for key, value in values.items():
            setattr(self, key, value)
        self._validate()

    def _validate(self):
        lo, hi = room_logic.HARD_MIN_PLAYERS, room_logic.HARD_MAX_PLAYERS
        if not (lo <= self.min_players <= self.max_players <= hi):
            raise ValueError("player limits must satisfy %d <= min <= max <= %d" % (lo, hi))
        for key in ("room_idle_ttl", "room_max_age", "token_ttl", "heartbeat_interval",
                    "sweep_interval", "sse_write_timeout", "request_timeout"):
            if not getattr(self, key) > 0:
                raise ValueError("%s must be positive" % key)
        if self.closed_room_grace < 0:
            raise ValueError("closed_room_grace must not be negative")
        for key in ("max_rooms", "action_retention", "max_body_bytes", "max_streams_per_principal",
                    "max_streams_per_room", "max_streams_total", "max_connections",
                    "request_burst", "create_burst", "join_burst", "action_burst"):
            if int(getattr(self, key)) < 1:
                raise ValueError("%s must be >= 1" % key)
        for key in ("request_rate", "create_per_minute", "join_per_minute", "action_rate"):
            if not getattr(self, key) > 0:
                raise ValueError("%s must be positive" % key)


class RateLimiter(object):
    """Token bucket per key with a bounded key table (LRU eviction)."""

    def __init__(self, rate_per_second, burst, clock=time.monotonic, max_keys=4096):
        self.rate = float(rate_per_second)
        self.burst = float(burst)
        self.clock = clock
        self.max_keys = max_keys
        self._buckets = collections.OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key):
        """Returns ``(allowed, retry_after_seconds)``."""
        with self._lock:
            now = self.clock()
            tokens, last = self._buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + max(0.0, now - last) * self.rate)
            if tokens >= 1.0:
                tokens -= 1.0
                allowed, retry = True, 0.0
            else:
                allowed, retry = False, (1.0 - tokens) / self.rate
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return allowed, retry


def hash_token(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def new_token():
    return secrets.token_urlsafe(32)


class Principal(object):
    """An authenticated capability. Holds a token hash, never the token."""

    __slots__ = ("role", "player_id", "token_hash", "expires_at")

    def __init__(self, role, player_id, token_hash, expires_at):
        self.role = role
        self.player_id = player_id
        self.token_hash = token_hash
        self.expires_at = expires_at

    @property
    def key(self):
        return "player:" + self.player_id if self.role == "player" else self.role

    def actor(self):
        return {"role": self.role, "playerId": self.player_id}


class Room(object):
    def __init__(self, code, state, now):
        self.code = code
        self.state = state
        self.revision = 1
        self.receipt_seq = 0
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.tokens = {}          # token hash -> Principal
        self.actions = {}         # principal key -> OrderedDict(actionId -> result)
        self.streams = {}         # principal key -> open stream count
        self.presence = {}        # player id -> open stream count
        self.stream_total = 0
        self.created_at = now
        self.last_activity = now
        self.closed_at = None
        self.expired = False

    def expires_at(self, config):
        deadline = min(self.created_at + config.room_max_age, self.last_activity + config.room_idle_ttl)
        if self.closed_at is not None:
            deadline = min(deadline, self.closed_at + config.closed_room_grace)
        return deadline

    def retained_action_count(self):
        return sum(len(log) for log in self.actions.values())


def _validate_limit(body, key, default):
    value = body.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise PartyError("invalid_config")
    return value


def parse_action(body):
    """Validate action envelope shape. Returns ``(action, error_code)``."""
    kind = body.get("type")
    if not isinstance(kind, str) or not ACTION_TYPE_RE.fullmatch(kind):
        return None, "invalid_action"
    round_id = body.get("roundId")
    if round_id is not None and (not isinstance(round_id, str) or not ROUND_ID_RE.fullmatch(round_id)):
        return None, "invalid_action"
    payload = body.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return None, "invalid_payload"
    return {"type": kind, "roundId": round_id, "payload": payload}, None


class RoomManager(object):
    def __init__(self, config, contents, clock=time.monotonic):
        self.config = config
        self.contents = contents
        self.clock = clock
        self._rooms = {}
        self._lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._stream_total = 0
        self._stop = threading.Event()
        self._sweeper = None

    # ------------------------------------------------------------------ rooms

    def room_count(self):
        with self._lock:
            return len(self._rooms)

    def create_room(self, body):
        cfg = self.config
        operator_name = body.get("operatorName")
        if operator_name is not None:
            operator_name = clean_name(operator_name)
            if operator_name is None:
                raise PartyError("invalid_name")
        min_players = _validate_limit(body, "minPlayers", cfg.min_players)
        max_players = _validate_limit(body, "maxPlayers", cfg.max_players)
        if not (room_logic.HARD_MIN_PLAYERS <= min_players <= max_players <= room_logic.HARD_MAX_PLAYERS):
            raise PartyError("invalid_config")
        with self._lock:
            self._purge_expired_locked()
            now = self.clock()
            if len(self._rooms) >= cfg.max_rooms:
                room = None
            else:
                code = self._unique_code()
                state = room_logic.new_room_state(code, operator_name, min_players, max_players)
                room = Room(code, state, now)
                host_token, display_token = new_token(), new_token()
                room.tokens[hash_token(host_token)] = Principal(
                    "operator", None, hash_token(host_token), now + cfg.token_ttl)
                room.tokens[hash_token(display_token)] = Principal(
                    "display", None, hash_token(display_token), now + cfg.token_ttl)
                self._rooms[code] = room
        if room is None:
            raise PartyError("too_many_rooms")
        return {"code": room.code, "hostToken": host_token, "displayToken": display_token,
                "revision": room.revision}

    def _unique_code(self):
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._rooms:
                return code

    def get_room(self, code):
        if not isinstance(code, str):
            raise PartyError("room_not_found")
        code = code.upper()
        if not CODE_RE.fullmatch(code):
            raise PartyError("room_not_found")
        with self._lock:
            room = self._rooms.get(code)
            if room is None:
                raise PartyError("room_not_found")
            if self._expire_if_due(room):
                del self._rooms[code]
                raise PartyError("room_not_found")
        return room

    def _expire_if_due(self, room):
        """Atomically decide and mark expiry under the room lock.

        The clock is read only after the lock is held, so the decision sees
        any mutation that committed while this call waited. Caller holds the
        manager lock (order manager -> room). Returns True when expired.
        """
        with room.cond:
            if room.expired or self.clock() >= room.expires_at(self.config):
                room.expired = True
                room.cond.notify_all()
                return True
            return False

    def _purge_expired_locked(self):
        removed = 0
        for code, room in list(self._rooms.items()):
            if self._expire_if_due(room):
                del self._rooms[code]
                removed += 1
        return removed

    def sweep(self):
        """Remove expired rooms and wake their streams. Returns count removed."""
        with self._lock:
            return self._purge_expired_locked()

    def start(self):
        if self._sweeper is not None:
            return
        def loop():
            while not self._stop.wait(self.config.sweep_interval):
                try:
                    self.sweep()
                except Exception:
                    pass
        self._sweeper = threading.Thread(target=loop, name="room-sweeper", daemon=True)
        self._sweeper.start()

    def shutdown(self):
        self._stop.set()
        with self._lock:
            rooms = list(self._rooms.values())
        for room in rooms:
            with room.cond:
                room.cond.notify_all()

    # ------------------------------------------------------------ credentials

    def _check_live_locked(self, room, principal=None, now=None):
        now = self.clock() if now is None else now
        if room.expired or now >= room.expires_at(self.config):
            room.expired = True
            room.cond.notify_all()
            raise PartyError("room_not_found")
        if principal is not None and now >= principal.expires_at:
            raise PartyError("token_expired")

    def authenticate(self, room, token):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise PartyError("unauthorized")
        digest = hash_token(token)
        with room.lock:
            self._check_live_locked(room)
            principal = room.tokens.get(digest)
            if principal is None:
                raise PartyError("unauthorized")
            self._check_live_locked(room, principal)
            return principal

    def join(self, room, body, token=None):
        """Returns ``(status, response)``. A bearer token resumes, never duplicates."""
        cfg = self.config
        if token is not None:
            principal = self.authenticate(room, token)
            if principal.role != "player":
                raise PartyError("forbidden")
            with room.lock:
                self._check_live_locked(room, principal)
                return 200, {"playerId": principal.player_id, "playerToken": token,
                             "revision": room.revision, "resumed": True}
        name = clean_name(body.get("name"))
        if name is None:
            raise PartyError("invalid_name")
        token = new_token()
        digest = hash_token(token)
        with room.cond:
            now = self.clock()  # read after the lock: a join that waited past expiry fails
            self._check_live_locked(room, now=now)
            existing = set(p["id"] for p in room.state["players"])
            player_id = "p" + secrets.token_hex(5)
            while player_id in existing:
                player_id = "p" + secrets.token_hex(5)
            new_state, err = room_logic.apply_join(room.state, player_id, name)
            if err:
                raise PartyError(err)
            room.state = new_state
            room.tokens[digest] = Principal("player", player_id, digest, now + cfg.token_ttl)
            room.revision += 1
            room.last_activity = now
            room.cond.notify_all()
            return 201, {"playerId": player_id, "playerToken": token, "revision": room.revision}

    # --------------------------------------------------------------- snapshot

    def _project_locked(self, room, principal):
        viewer = {"role": principal.role, "playerId": principal.player_id}
        return room_logic.project_room(room.state, viewer, room.revision, room.presence, self.contents)

    def snapshot(self, room, principal):
        with room.lock:
            self._check_live_locked(room, principal)
            return self._project_locked(room, principal)

    # ---------------------------------------------------------------- actions

    def submit_action(self, room, principal, body):
        """Apply or replay one action. Returns ``(status, result, replayed)``.

        ``result`` is ``{accepted, revision, error?}``. Results (including
        rejections) are retained per principal and actionId, so a replay
        returns the original result without applying anything.
        """
        action_id = body.get("actionId")
        if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
            raise PartyError("invalid_action_id")
        action, shape_error = parse_action(body)
        with room.cond:
            now = self.clock()  # read after the lock: an action that waited past expiry fails
            self._check_live_locked(room, principal, now)
            log = room.actions.get(principal.key)
            if log is None:
                log = room.actions[principal.key] = collections.OrderedDict()
            if action_id in log:
                status, result = log[action_id]
                return status, result, True
            room.receipt_seq += 1
            sequence = room.receipt_seq
            err = shape_error
            new_state = None
            if err is None:
                seed = secrets.randbits(64) if action["type"] == "room.start" else 0
                try:
                    new_state, err = room_logic.reduce_room(
                        room.state, principal.actor(), action, sequence, self.contents, seed)
                except Exception:
                    new_state, err = None, "internal_error"
            if err is None:
                room.state = new_state
                room.revision += 1
                room.last_activity = now
                if new_state["phase"] == room_logic.CLOSED and room.closed_at is None:
                    room.closed_at = now
                room.cond.notify_all()
                entry = (200, {"accepted": True, "revision": room.revision})
            else:
                entry = (status_for(err), {"accepted": False, "revision": room.revision,
                                           "error": error_body(err)})
            log[action_id] = entry
            while len(log) > self.config.action_retention:
                log.popitem(last=False)
            return entry[0], entry[1], False

    # ---------------------------------------------------------------- streams

    def open_stream(self, room, principal):
        cfg = self.config
        with self._stream_lock:
            if self._stream_total >= cfg.max_streams_total:
                raise PartyError("too_many_streams")
            self._stream_total += 1
        try:
            with room.cond:
                self._check_live_locked(room, principal)
                key = principal.key
                if room.streams.get(key, 0) >= cfg.max_streams_per_principal \
                        or room.stream_total >= cfg.max_streams_per_room:
                    raise PartyError("too_many_streams")
                room.streams[key] = room.streams.get(key, 0) + 1
                room.stream_total += 1
                if principal.role == "player":
                    previous = room.presence.get(principal.player_id, 0)
                    room.presence[principal.player_id] = previous + 1
                    if previous == 0:
                        room.revision += 1
                        room.cond.notify_all()
        except Exception:
            with self._stream_lock:
                self._stream_total -= 1
            raise

    def close_stream(self, room, principal):
        with room.cond:
            key = principal.key
            count = room.streams.get(key, 0) - 1
            if count > 0:
                room.streams[key] = count
            else:
                room.streams.pop(key, None)
            room.stream_total = max(0, room.stream_total - 1)
            if principal.role == "player":
                remaining = room.presence.get(principal.player_id, 0) - 1
                if remaining > 0:
                    room.presence[principal.player_id] = remaining
                else:
                    room.presence.pop(principal.player_id, None)
                    if not room.expired:
                        room.revision += 1
                        room.cond.notify_all()
        with self._stream_lock:
            self._stream_total = max(0, self._stream_total - 1)

    def next_stream_event(self, room, principal, last_revision, heartbeat_at):
        """Block until there is something to send on a stream.

        Returns ``("state", revision, snapshot)``, ``("heartbeat",)``,
        ``("error", code)`` or ``("close",)``. The snapshot is projected under
        the room lock; the caller writes it after the lock is released.
        """
        with room.cond:
            while True:
                now = self.clock()
                if self._stop.is_set():
                    return ("close",)
                if room.expired or now >= room.expires_at(self.config):
                    room.expired = True
                    return ("error", "room_expired")
                if now >= principal.expires_at:
                    return ("error", "token_expired")
                if room.revision != last_revision:
                    return ("state", room.revision, self._project_locked(room, principal))
                if now >= heartbeat_at:
                    return ("heartbeat",)
                deadline = min(heartbeat_at, principal.expires_at, room.expires_at(self.config))
                room.cond.wait(max(0.01, min(deadline - now, 5.0)))
