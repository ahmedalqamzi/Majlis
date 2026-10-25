# Majlis party suite: backend candidate (protocol v1)

This is an isolated backend candidate for `CONTRACT.md` (protocol v1). It is a Python 3.9+
standard-library HTTP + Server-Sent Events server with no dependencies.

**Status:** only `who_am_i` and `feud` are implemented. The other eight contract modes
(`impostor`, `draw_guess`, `drawing_chain`, `bluff`, `werewolf`, `taboo`, `wavelength`,
`escape_room`) are declared with `implemented: false`. Selecting or starting any of them is
rejected with `mode_unimplemented`; they are never presented as playable.

> **State is in process memory only.** Every room, token, score and replay record is lost
> when the process stops or restarts. Production persistence and recovery have not been
> evaluated (CONTRACT.md "First runtime").

## Run

All commands run from the workspace root (the directory containing `party/` and `tests/`).

```sh
PY=/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3

# Loopback, fixed port 8765, content from ./party/content (may be absent -> content_missing)
$PY -m party.server

# Loopback, ephemeral port (the chosen port is printed)
$PY -m party.server --port 0

# Explicit content directory
$PY -m party.server --port 0 --content-dir party/content

# Also serve the frontend allowlist from an explicit party/web directory (same origin)
$PY -m party.server --port 0 --web-dir party/web

# Validate content only (exit 0 if every implemented mode's pack is ok, else 1)
$PY -m party.server --check-content --content-dir party/content

# Authorised LAN preview only: requires --allow-lan, plus exact origin(s) when binding 0.0.0.0
$PY -m party.server --allow-lan --host 192.168.1.20 --port 8765
$PY -m party.server --allow-lan --host 0.0.0.0 --port 8765 --allowed-origin http://192.168.1.20:8765
```

Once it is listening, the server prints exactly one JSON line to stdout. It never prints a
token:

```json
{"content": {"feud": "ok", "who_am_i": "content_missing"}, "event": "listening", "host": "127.0.0.1", "persistence": "memory-only: all rooms are lost when this process stops", "port": 54321, "protocol": 1, "url": "http://127.0.0.1:54321", "web": "disabled"}
```

`web` is `"disabled"` (APIs only, the default) or `"allowlist"` when `--web-dir` is given.
The banner never contains a filesystem path.

`SIGTERM`/`SIGINT` shut it down cleanly (`{"event": "stopped"}`). With
`--exit-on-stdin-eof` the server also exits when its stdin closes. The tests use that flag so
no server outlives its parent. Startup errors print `{"event":"error","error":"invalid_config"|"bind_failed",...}`
to stderr and exit with status 2. A non-loopback `--host` without `--allow-lan` is refused.
A `--web-dir` that is missing or not a directory gives `invalid_config` with the fixed
detail `--web-dir must be an existing directory` (the path is not echoed).

Every `ServerConfig` limit has a matching CLI flag: `--help` lists them all.

## Test

```sh
PY=/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3
cd <workspace root>
$PY -m unittest discover -s tests -v
# a single file:
$PY -m unittest discover -s tests -p 'test_http_flows.py' -v
```

| File | Covers |
| --- | --- |
| `tests/test_content.py` | Loader and validator: fixtures ok, missing directory or file gives a stable `content_missing`, invalid JSON/encoding/size/schema gives `content_invalid` without crashing, issue strings never echo content values. |
| `tests/test_reducers.py` | Purity of both modes: input never mutated, deterministic output, projection purity. Per-viewer privacy, stale round, dedup, roles. |
| `tests/test_rooms.py` | Room reducer: ten-mode registry, start gating, forbidden roles, roundId rules, pause/end/reset/close. Manager: codes/tokens, resume, replay including failures, retention bound, room cap, expiry (fake clock), token expiry, stream limits/presence, threaded buzz race. |
| `tests/test_http_flows.py` | Real subprocess server. Operator + 3 players + display over separate connections and raw SSE sockets. Complete Who Am I and Feud loops (create/join/mode/start/score/next/end), concurrent buzz races (one winner each), replay/duplicate/stale protection, full-payload and whole-stream privacy, reconnect identity/presence/Last-Event-ID, SSE id/state/heartbeat, stream limits, stalled client. |
| `tests/test_http_security.py` | Wrong-room/missing/forged/role tokens, tokens in URLs, Host/Origin/Sec-Fetch-Site, no CORS, body/content-type/route validation, join rules and 3–12 bounds, rate and size limits, room idle/closed expiry and token expiry during live SSE, `content_missing`, access-log redaction, CLI banner/refusals/`--check-content`/clean exit. |
| `tests/test_http_static.py` | `--web-dir` on synthetic temp roots: every entry page and asset (body, MIME, CSP, headers), private/unknown paths, raw percent-encoded/backslash/traversal paths, symlinked files and directories (inside and outside the root, swapped after startup), FIFOs, query boundaries, method/Host/Origin/rate guards, API and SSE still live, access-log redaction, default API-only 404s, clean failure for a missing or non-directory `--web-dir`. |

Fixtures under `tests/fixtures/content/` are synthetic and labelled **TEST** throughout. They
are not authored game content and must not be shipped as content.

## Modules

| Module | Role |
| --- | --- |
| `__main__.py` | CLI. |
| `httpd.py` | `ThreadingHTTPServer` subclass, routing, Host/Origin checks, body limits, SSE writer, connection cap. |
| `static.py` | Optional `--web-dir` adapter: exact path allowlist, descriptor-based no-symlink file reads, HTML CSP. |
| `manager.py` | `ServerConfig`, `RateLimiter`, `Room`, `RoomManager`: locking, receipt sequence, credentials, replay log, presence, streams, expiry sweeper. |
| `room_logic.py` | Pure room reducer (`reduce_room`, `apply_join`) and role projection (`project_room`). |
| `modes/__init__.py` | Registry of all ten mode ids plus the public catalog. |
| `modes/who_am_i.py`, `modes/feud.py` | Pure `validate_item`, `initialize(config, players, content)`, `reduce(state, actor, action, serverSequence)`, `project(state, viewer)`. |
| `content.py` | Pack loading/validation. |
| `errors.py` | Stable error codes and fixed messages. |
| `textutil.py` | Name/guess normalisation and comparison keys. |

Each accepted action runs a pure reducer on the current state. The room swaps state and
increments `revision` exactly once, under the room lock. A rejected action leaves the state
object untouched, because reducers copy before changing anything.

## API catalog

All bodies are JSON (`Content-Type: application/json`, `Content-Length` required, no chunked
bodies). All responses carry `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY` and `Connection: close`. API and SSE responses use the restrictive CSP
`default-src 'none'; frame-ancestors 'none'`. Only static HTML gets the page CSP described
below. No `Access-Control-*` header is ever sent.
Credentials travel **only** in `Authorization: Bearer <token>`.

### Endpoints

| Method and path | Auth | Request | Success |
| --- | --- | --- | --- |
| `GET /api/health` | none | — | `200 {"ok":true,"protocol":1}` |
| `GET /api/modes` *(additive)* | none | — | `200 {"protocol":1,"modes":[{"id","title","implemented","content"}]}`, where `content` is `"ok"`, `"content_missing"`, `"content_invalid"`, or `null` for unimplemented modes |
| `POST /api/rooms` | none | `{operatorName?, minPlayers?, maxPlayers?}` | `201 {"code","hostToken","displayToken","revision"}` |
| `POST /api/rooms/{code}/join` | none (new player) | `{name}` | `201 {"playerId","playerToken","revision"}` |
| `POST /api/rooms/{code}/join` | player bearer (resume) | `{}` (any name is ignored) | `200 {"playerId","playerToken","revision","resumed":true}` |
| `GET /api/rooms/{code}/state` | any role bearer | — | `200` role-projected snapshot |
| `GET /api/rooms/{code}/events` | any role bearer | optional `Last-Event-ID` (advisory) | `200 text/event-stream` |
| `POST /api/rooms/{code}/actions` | any role bearer | `{actionId, type, roundId?, payload?}` | `200 {"accepted":true,"revision"}` |

* **Room code:** 6 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, drawn with `secrets`,
  nonsequential and case-insensitive in paths. It is a locator, never authorisation.
* **Tokens:** `secrets.token_urlsafe(32)` (43 characters), independent of code and name.
  Only SHA-256 hashes are kept in memory. `hostToken` is the **operator** role and
  `displayToken` is the **display** role. Each player token is bound to one `playerId`.
  Player ids are random (`p` + 10 hex digits).
* **Names:** NFC, whitespace collapsed, 1–24 characters. Control and bidi-override
  characters are rejected. Duplicates are compared case- and width-insensitively (NFKC +
  casefold).

### Static frontend (`--web-dir` only)

The server serves only APIs by default, so `/`, `/host.html` and the rest return `404 not_found`. With
`--web-dir DIR`, it also serves this exact `GET` allowlist from `DIR` (meant to be `party/web`)
on the same origin:

| Path | Content-Type |
| --- | --- |
| `/` (alias of `/index.html`), `/index.html`, `/host.html`, `/play.html`, `/tv.html` | `text/html; charset=utf-8` |
| `/css/party.css` | `text/css; charset=utf-8` |
| `/js/{index,host,play,tv,ui,transport,session,modes,views,qr}.js` | `text/javascript; charset=utf-8` |

* Every other path returns `404 not_found`, and nothing is listed. That includes
  `js/sim-transport.js`, `sim/`, `tests/`, `*.md`, content packs, source files, directories
  and any other extension. The raw request path must equal an allowlisted path exactly. Nothing
  is decoded or normalised, so percent-encoding, backslashes, `.`/`..` segments, trailing
  slashes and case variants are all refused.
* Files are read for each request through a root directory descriptor that is opened once at
  startup. Each component is opened with `O_NOFOLLOW` relative to that descriptor, so **every
  symlink is refused**, whether it points to a file or a directory, inside or outside the root.
  Swapping a directory for a symlink after startup, or renaming the root, cannot redirect a
  request. Only regular files of 2 MiB or less are served. FIFOs and devices give 404.
* **Query strings.** The only query string accepted anywhere is `/play.html?code=XXXXXX`:
  exactly one `code` parameter with 6 characters from the room-code alphabet (upper case).
  Any other query, on any static or API path, gives `400 query_not_allowed`. Examples are
  `host.html?code=` and `tv.html?code=` (see Limitations), `?sim=1`, and extra or repeated
  parameters.
* HTML responses carry `Content-Security-Policy: default-src 'none'; script-src 'self';
  style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src
  'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`. That allows the local
  ES modules and CSS, inline QR/SVG style attributes, and same-origin API/SSE. It blocks
  external scripts, framing, `<base>`, plugins and workers. CSS/JS responses and every
  API/SSE response keep the restrictive `default-src 'none'; frame-ancestors 'none'`.
* Static requests pass the same Host, Origin, Sec-Fetch-Site and per-IP rate checks as the APIs,
  and accept only `GET`. The access log records them as `static` or `unmatched`, never by
  path.

### Snapshot (all roles)

```json
{"protocol":1,"code":"K7QM2D","revision":12,"role":"operator|player|display",
 "self":null | {"id":"p1a2b3c4d5","name":"Ahmed"},
 "mode":null | "who_am_i" | "feud","phase":"lobby|playing|paused|ended|closed",
 "roundId":null | "g1r2","players":[{"id","name","connected","score"}],
 "game":null | {...mode projection...},
 "operatorName":null | "…","playerLimits":{"min":3,"max":12},
 "modes":[...operator only...]}
```

`self` is null for the operator and display. `connected` is true while the player has at
least one open event stream. Every state, event and reconnect snapshot is built by
`project_room`, which calls the mode's `project` for that viewer. Action responses contain
only `accepted`, `revision` and `error`, never state.

### SSE

```
retry: 3000

id: 12
event: state
data: {...snapshot...}

: heartbeat

event: error
data: {"ok":false,"error":{"code":"room_expired"|"token_expired","message":"…"}}
```

* The first event is always the current projected snapshot. `Last-Event-ID` is advisory: it
  is read but never used as permission or to skip state.
* Events coalesce. A stream always sends the latest snapshot, so a client that falls behind
  skips intermediate revisions rather than queueing them. `id` is always the snapshot
  `revision` and increases strictly within a stream.
* A heartbeat comment is sent after `--heartbeat` seconds (default 15) without other
  output. At heartbeat time the server also checks whether the peer has closed the
  connection.
* Room expiry and token expiry are re-checked on every wake-up, and waits are bounded by
  both deadlines. A live stream receives `event: error` and is then closed.
* Writes happen outside the room lock with a socket timeout of `--sse-write-timeout`
  (default 10 s). A slow or stalled client blocks only its own handler thread.

### Actions

The envelope is `{actionId, type, roundId?, payload?}`.

* `actionId` is required: 1–64 characters of `[A-Za-z0-9._:-]`. It is scoped per
  authenticated principal (operator, display, or each player).
* `type` has the form `prefix.name`. `payload` must be an object and defaults to `{}`.

| Type | Role | roundId | Payload | Notes |
| --- | --- | --- | --- | --- |
| `room.set_mode` | operator | — | `{mode}` | Only in `lobby`/`ended`. Unknown mode gives `unknown_mode`; any of the eight unimplemented modes gives `mode_unimplemented`. |
| `room.start` | operator | — | `{}` | Only in `lobby`/`ended`. Needs a selected mode with `ok` content and between `minPlayers` and `maxPlayers` players. Starts game N+1 with fresh round ids. |
| `room.pause` / `room.resume` | operator | — | `{}` | `playing` ↔ `paused`. Every mode action and `round.next` returns `paused` while paused. |
| `room.end` | operator | — | `{}` | Ends the current game. `phase` becomes `ended` and the mode applies its end reveal. |
| `room.reset` *(additive)* | operator | — | `{clearScores?: bool}` | Returns to `lobby` and discards the game. Scores are kept unless `clearScores`. **Players are kept**: no player is removed, no name is freed, and no token is reissued or restored. |
| `room.close` *(additive)* | operator | — | `{}` | `phase` becomes `closed`: joins return `room_closed`, every action is rejected, and the room expires after `--closed-room-grace`. |
| `round.next` | operator | **required** | `{}` | Delegated to the mode. Gives `content_exhausted` when no items remain. |
| `whoami.ready` | player | **required** | `{ready?: bool = true}` | Only while `roundPhase` is `asking`. |
| `whoami.guess` | player | **required** | `{text}` (1–80 chars) | Recorded as the token's player's pending guess, replacing any earlier one; any `playerId` in the payload is ignored. Never auto-checked against the answer. Only the operator and the guessing player ever see the text. |
| `whoami.dismiss` | operator | **required** | `{playerId}` | Clears that player's pending guess (judged wrong). Scores, assignments, `revealed`, `solved` and awards are untouched, and the player may guess again. No pending guess gives `no_pending_guess`; an unknown player gives `unknown_player`. Replaying the same `actionId` returns the stored result and never clears a guess submitted later. |
| `whoami.reveal` | operator | **required** | `{playerId?}` | Reveals one player, or everyone when omitted. |
| `whoami.award` | operator | **required** | `{playerId, points?: 0–100 = 1}` | Deduplicated by `roundId + playerId`; a repeat gives `already_awarded`. Also reveals that player. |
| `feud.open_buzzer` | operator | **required** | `{}` | Only from `closed` with no winner. |
| `feud.buzz` | player | **required** | `{}` | The first buzz the room accepts while the buzzer is open wins. Later ones get `buzzer_taken`; buzzes while closed get `buzzer_closed`. |
| `feud.reset_buzzer` | operator | **required** | `{}` | Clears the winner and control and closes the buzzer. Not allowed after an award. |
| `feud.reveal` | operator | **required** | `{answerId}` or `{text}` | `text` matches the answer text or aliases (NFKC, casefold, whitespace-normalised). Allowed after an award for show, but it adds no points. |
| `feud.strike` | operator | **required** | `{}` | While `answering`/`steal`, up to 3 strikes. |
| `feud.steal` | operator | **required** | `{playerId}` | Only at 3 strikes, while `answering`. Moves control to another player. |
| `feud.award` | operator | **required** | `{playerId?}` (defaults to the controlling player) | Awards the sum of answers revealed so far. Deduplicated by `roundId + boardId`, so a repeat gives `already_awarded`/`round_awarded`. |

Response: `{"accepted":true,"revision":N}` with HTTP 200, or
`{"accepted":false,"revision":N,"error":{"code","message"}}` with the code's HTTP status.
The `X-Action-Replayed` header is `1` when the stored result was returned.

**Replay.** The result of every action that reaches the room is stored per principal and
`actionId`, including rejections such as `forbidden`, `stale_round`, `invalid_payload` and
`invalid_action`. Each principal keeps the newest `--action-retention` results (default 128).
Retained entries per room are therefore at most `(maxPlayers + 2) × retention`. A replay
returns the stored status and body byte-for-byte and applies nothing. Replay lookup happens
before validation, so a replayed action returns its original result even if it would now be
stale. Requests rejected **before** the room sees them are not recorded and may be retried
with the same `actionId`: auth failures, malformed JSON, oversized bodies, rate limiting and
an invalid `actionId`.

**Ordering.** Under the room lock each action takes the next value of a per-room monotonic
receipt sequence before it is reduced. Client clocks, timestamps and claimed ids are
ignored. The actor is always derived from the token.

### Who Am I projections (`game`)

* **Common:** `title`, `round`, `roundPhase` (`asking|revealed|ended`), `order` (seating =
  join order), `ready[]`, `revealed[]`, `solved[]`, `pendingGuesses[]` (ids of players with
  a pending guess, in seating order; ids only, never text).
* **Player:** `assignments` holds every *other* player's `{label, ar?}`, keyed by playerId in
  seating order. `you` is `{revealed, ready, solved, guess, assignment?}`. `guess` is this
  player's own pending guess text, or null. `assignment` is present only after this player
  is revealed (by `whoami.reveal`, by `whoami.award` as an explicit individual reveal, or at
  game end). Until then, the player's own label, `ar`, item id and difficulty appear nowhere
  in any response or event they receive. There is no `guesses` key, so other players' guess
  text is never sent.
* **Display:** `assignments` contains only revealed players, so it is empty before any reveal.
  There is no `guesses` key.
* **Operator:** every assignment with `{id,label,ar?,difficulty}`, `guesses{playerId:text}`
  (every pending guess), `roundsRemaining` and `awardPoints`.
* Guess text stays private after individual reveals, after reveal-all and at game end.
  `round.next` clears every pending guess.
* Characters are drawn from a deck shuffled with a per-game seed from `secrets`. The seed
  and the deck never leave the server. Item ids and difficulty are never sent to players
  or the display. Nothing in a player's projection is ordered by assignment.

### Feud projections (`game`)

* **Everyone:** `title`, `label:"House answers"`, `answerSource:"house"`, `board`,
  `boardCount`, `question`, `roundPhase` (`closed|buzzer_open|answering|steal|awarded|ended`),
  `slots[]`, `strikes`, `maxStrikes`, `roundPoints`, `buzzer{open,winner}`, `control`,
  `stealFrom`, `awardedTo`, `awardedPoints`.
* An unrevealed slot is exactly `{index, revealed:false}`. A revealed slot adds
  `{id,text,points}`. Aliases are never sent to players or the display.
* **Operator only:** `boardId`, `boardsRemaining`, `answers[{id,text,aliases,points,revealed}]`.
* No respondent counts or survey claims are made or accepted. Unknown content keys (e.g.
  `respondents`) are dropped at load time.

## Errors

The error body is `{"ok":false,"error":{"code":"…","message":"…"}}`. Messages are fixed
strings and never contain request data, content, state or tokens. Unexpected exceptions
return `500 internal_error`, and only the exception class name is logged (no traceback).

| HTTP | Codes |
| --- | --- |
| 400 | `bad_request`, `invalid_json`, `query_not_allowed`, `invalid_config`, `invalid_name`, `invalid_action_id`, `invalid_action`, `unknown_action`, `invalid_payload`, `round_required`, `unknown_mode`, `unknown_player`, `unknown_answer` |
| 401 | `unauthorized` (missing, malformed, unknown or other-room token: deliberately indistinguishable), `token_expired` |
| 403 | `forbidden`, `origin_forbidden`, `host_forbidden` |
| 404 | `not_found`, `room_not_found` (unknown or expired room) |
| 405 | `method_not_allowed` |
| 409 | `name_taken`, `room_full`, `room_closed`, `game_in_progress`, `stale_round`, `invalid_phase`, `paused`, `wrong_mode`, `mode_unimplemented`, `mode_not_selected`, `content_missing`, `content_invalid`, `content_insufficient`, `content_exhausted`, `not_enough_players`, `too_many_players`, `already_revealed`, `no_pending_guess`, `already_awarded`, `buzzer_closed`, `buzzer_taken`, `no_match`, `max_strikes`, `steal_not_available`, `no_control`, `round_awarded` |
| 410 | `room_expired` (only inside SSE `event: error`) |
| 411 / 413 / 415 | `length_required` / `body_too_large` / `unsupported_media_type` |
| 429 | `rate_limited` (with `Retry-After`), `too_many_streams` |
| 500 / 503 | `internal_error` / `too_many_rooms`, `server_busy` |

## Security checks (in request order)

1. **Host.** There must be exactly one `Host` header, and it must be one of the server's own
   origins: `127.0.0.1:<port>` and `localhost:<port>` for loopback, the bound IP, or each
   `--allowed-origin`. This guards against DNS rebinding.
2. **Origin.** When present, `Origin` must exactly equal an allowed origin. `null`, a
   trailing slash, a different scheme or a different port is rejected.
3. **Sec-Fetch-Site.** When present, it must be `same-origin` or `none`.
4. **Query strings.** Any query string on any route is rejected with `query_not_allowed`
   before authentication. A token in a URL is therefore never accepted and never reaches a
   log. The single exception is the static `/play.html?code=XXXXXX` when `--web-dir` is
   enabled. It carries a room code only, and API routes never accept a query.
5. **Request rate.** A per-IP token bucket applies.
6. **Route limits.** Creation and join each have their own per-IP limit, and actions have a
   per-principal limit.
7. **Body.** `Content-Type: application/json` and `Content-Length` ≤ `--max-body-bytes` are
   required.
8. **Credentials.** The bearer token is checked against the room's hashed credentials and
   its expiry.
9. **Role.** Role checks happen inside the pure reducers.

Requests without an `Origin` header (curl, tests, native clients) are allowed. Bearer
credentials are never attached automatically by a browser, and a JSON content type forces a
CORS preflight, which the server never approves.

**Logging.** `BaseHTTPRequestHandler` request logging is disabled. `--access-log` writes
`access <METHOD> <route-template> <status>` lines, e.g.
`access GET /api/rooms/:code/state 200`. These lines never include paths, codes, query
strings, headers, names or tokens.

## Limits, expiry and cleanup

| Flag (default) | Meaning |
| --- | --- |
| `--min-players 3`, `--max-players 12` | Server defaults for new rooms. Each room may override with `minPlayers`/`maxPlayers`, but **the supported range is hard-bounded to 3 ≤ min ≤ max ≤ 12**. Anything else gives `invalid_config` (server flags exit with status 2). A join beyond max gives `room_full`; starting below min gives `not_enough_players`. |
| `--room-idle-ttl 7200` | A room expires this long after its last *accepted* mutation (create, join, or accepted action). Reads, streams, rejected actions and replays do not extend it. |
| `--room-max-age 43200` | Absolute room lifetime. |
| `--closed-room-grace 60` | Lifetime after `room.close`. |
| `--token-ttl 43200` | Each token is valid for this long after issue. Resume does not refresh it. Expiry is enforced on every request **and** on live SSE streams. |
| `--sweep-interval 30` | Background sweep that removes expired rooms and wakes their streams. Expiry is also checked lazily on every access, so the cutoff is deterministic: a room is unusable at `min(created+maxAge, lastActivity+idleTtl[, closedAt+grace])`. |
| `--max-rooms 64` | Live rooms; above this, creation returns `too_many_rooms` (expired rooms are purged first). |
| `--action-retention 128` | Replay entries kept per principal. |
| `--max-body-bytes 16384` | Request body cap. |
| `--max-streams-per-principal 4`, `--max-streams-per-room 64`, `--max-streams-total 256` | SSE caps. |
| `--max-connections 320` | Concurrent connections. Excess connections get an immediate `503 server_busy` and are closed. |
| `--request-timeout 15`, `--sse-write-timeout 10` | Per-socket read/write timeouts (slowloris and stalled readers). |
| `--request-rate 30 --request-burst 120` | Per-IP requests per second. |
| `--create-per-minute 10 --create-burst 10`, `--join-per-minute 60 --join-burst 30` | Per-IP creation/join limits. |
| `--action-rate 10 --action-burst 30` | Per-principal action rate. |

**Expiry is atomic with mutation.**

* Joins, actions, resume, snapshots and streams read the clock only after acquiring the room
  lock. A request that waited on the lock until after the room's or token's deadline is
  rejected (`room_not_found` / `token_expired`). It is never applied, gets no receipt
  sequence, and is not recorded for replay.
* The table operations (`get_room`, the purge inside `create_room`, and the sweeper) decide
  and mark expiry while holding the manager lock and then that room's lock (manager → room).
  The clock is read inside the room lock. A room whose mutation commits while the sweeper
  waits therefore cannot be deleted based on a stale `last_activity`.
* Nothing acquires the manager lock while holding a room lock, so the order cannot invert.

**Identifier validation** uses `re.fullmatch` for room codes, tokens, `actionId`, action
`type`, `roundId` and the room route, so a trailing newline (`"a1\n"`) is rejected. A
rejected identifier never mutates state. A malformed `type` or `roundId` is still recorded
as a failed result under its valid `actionId`.

## Content

Packs are loaded once at startup from `--content-dir`, default `party/content` (resolved
relative to this package). That directory may legitimately be absent. A missing directory or
file reports `content_missing` consistently (banner, `/api/modes`, operator `modes`,
`room.start`). Anything that fails validation reports `content_invalid`. Validation issues
name JSON paths only (`--check-content` prints them). The server never crashes on bad
content.

Each pack has the contract envelope `{schemaVersion:1, mode, title, licenseNote, items}`.
`mode` must equal the file name, the file must be UTF-8 and at most 2 MiB, and there may be
at most 2000 items with unique ids.

* `who_am_i` items have the form `{id, label, ar?, difficulty}`.
  * **Display-text uniqueness.** Every `label` and `ar` is canonicalised: NFKC, then
    casefold, then whitespace collapsed and trimmed (`textutil.match_key`). Each canonical
    text may belong to only one item, in either field. So `"Cat"`, `" cat "`, fullwidth
    `"Ｃａｔ"`, and an `ar` of another item equal to `"cat"` all collide, and the pack is
    `content_invalid` with an issue such as
    `items[4].label: collides with display text of items[1]`. The issue never contains the
    text.
  * `label` equal to `ar` *within the same item* is allowed.
  * Containment is not rejected (e.g. "Lion" vs "Sea Lion"): these are distinct items, and
    the other item's full label is what is served.
  * This guarantees that no other player's *served assignment* is the exact
    (canonical) text of the viewer's own secret.
* `feud` items have the form
  `{id, question, label:"House answers", answers:[{id, text, aliases:[text], points}]}`.
  The label must match exactly. Each board has 1–20 answers with unique answer ids,
  integer `points` from 0 to 1000, and `aliases` as an array (which may be empty). Answer
  order is preserved as the board order. Boards are played in pack order.

Unknown keys are ignored and are never projected.

## Limitations

* The server keeps everything in memory in a single process, so a restart loses everything.
  There is no HTTPS (LAN preview is plain HTTP), and no public deployment has been verified.
* It binds IPv4 only.
* Presence is stream-based. A player whose network drops silently can show `connected:true`
  until the next heartbeat write fails, which takes roughly one or two heartbeat intervals.
* Tokens cannot be revoked or rotated, and there is no kick or rename. A player who still
  has a valid token resumes with `POST /join` and that bearer token, in any phase. A player
  whose token is lost (cleared storage, another browser) **cannot** recover that seat:
  joining again by name gives `name_taken`. `room.reset` keeps all players, frees no name
  and restores no capability, so it does not help. The way out is for the host to
  `room.close` the room (or let it expire) and create a new room that everyone joins
  again. There is no reset/reclaim authorization flow.
* Per-IP rate limits treat all devices behind one NAT address as one client. The defaults
  are sized for one household.
* Feud is played by individual players, not teams. Who Am I guesses are recorded for the
  host to judge and are not matched automatically.
* Content is read only at startup, so changes need a restart.
* If a Who Am I pack holds exactly as many items as there are players and players know the
  pack, they could deduce their own item by elimination. Packs should be much larger than
  12, and the projection never exposes the pack or the deck.
* This candidate includes no frontend and no QR generation. `--web-dir` only serves an
  existing `party/web` allowlist. It has been tested on loopback with synthetic files, not in a
  browser, on a phone, over LAN or in any deployment.
* The static adapter accepts a query string only on `play.html`. The current frontend also
  writes `host.html?code=` and `tv.html?code=` (history state and the TV pop-up). A reload or
  pop-up at those URLs gets `400 query_not_allowed` until the frontend stops using them
  or that boundary is reviewed. `?sim=1` and `js/sim-transport.js` are deliberately not
  served.
* The `--web-dir` root path itself is resolved once at startup, so a symlinked root is
  followed as the operator's explicit choice. Hard links inside the root cannot be told
  apart from regular files. Only the allowlisted names are ever opened.
* Requests carrying `Sec-Fetch-Site: cross-site`/`same-site`, such as a link from another
  site, are refused for pages too. Typed URLs, QR scans (`none`) and same-origin links work.
* `whoami.dismiss` targets the player's *current* pending guess. If the player replaces the
  guess just before the host's dismiss arrives, the newer guess is cleared and the player
  must resubmit. Dismiss does not carry a guess version.
* **Secret-origin data vs player communication.** The Who Am I privacy guarantee covers
  data that *originates from the secret store*: assignments, item ids, difficulty, the deck
  and the seed. These never appear in a player's projection before that player's reveal.
  Text that players or the host type themselves is *communication*. Player names and
  `operatorName` are relayed to the room as typed. Guess text goes only to the operator
  (`game.guesses`) and back to its author (`game.you.guess`), never to other players or the
  display, including after a reveal. Someone who knows a player's character can still type it
  as a name, or show it to the host as a guess. The server makes no attempt to filter that, and
  no mechanism can guarantee against it. Such text lives only under `players[].name`,
  `operatorName`, the operator's `game.guesses` and the author's own `you.guess`, never under
  `assignments` or `you.assignment`. Validation and rejection errors never echo submitted or
  secret text.

## Proposed additive clarifications (not silent contract changes)

These are the conventions this candidate uses where CONTRACT.md is silent. They are
proposals for Skull and Wonder to review, not contract changes. No new mode ids or contract
fields are claimed. CONTRACT.md itself is unchanged.

1. **Roles.** `role` is `operator` (from `hostToken`), `player` or `display`.
2. **Error shape.** Errors are `{"ok":false,"error":{"code","message"}}`. For actions,
   `error` is the same `{code,message}` object, and rejected actions use non-200 statuses.
   The `X-Action-Replayed` header is added.
3. **roundId.** It is **required** for every mode action and for `round.next`, so a delayed
   request can never land in a later round. When supplied to other room actions during a
   game it must match. The formats are `g<game>r<round>` (Who Am I) and `g<game>b<board>`
   (Feud), and are unique per game within a room.
4. **actionId.** It is always required, has the charset/length above, is scoped per
   principal, and replay precedes validation.
5. **`room.end`.** It ends the current *game* and triggers the reveal. A new game may be
   set up or started from `ended`. `room.end` does not close a lobby. `room.reset` and
   `room.close` are distinct additive host actions. `room.reset` returns to `lobby` and keeps
   every player, name and token as they are. It is not a way to recover a lost token.
   `room.close` closes the room.
6. **Joins.** New joins are accepted only in `lobby`/`ended`; joining during `playing`/
   `paused` gives `game_in_progress`. Resume is `POST /join` with the player's bearer token
   and works in any phase, including mid-game.
7. **Presence.** `connected` means "has an open event stream". Presence changes increment
   `revision`, so SSE ids remain one-to-one with snapshot contents.
8. **Additive snapshot fields.** These are `operatorName` and `playerLimits`, plus `modes`
   for the operator only. `GET /api/modes` is also additive.
9. **Query strings.** All query strings are rejected on API routes, not only token-like
   parameters. With `--web-dir`, only the static `play.html?code=XXXXXX` is accepted.
10. **Player bounds.** The engineering default is 3–12 players, and the server enforces it
    as a hard range per room (CONTRACT.md "First runtime").
11. **Who Am I scoring.** An award is an explicit individual reveal of the identified
    player; points default to 1. Guess text is visible only to the operator (`guesses`) and to
    its author (`you.guess`). Everyone sees `pendingGuesses` ids. `whoami.dismiss {playerId}`
    (operator, roundId required) clears a pending wrong guess without any other change.
12. **Feud scoring.** `feud.award` awards the sum of answers revealed before the award; a
    steal needs 3 strikes. The number of answer slots is public, but slot contents are not.
13. **Content leniency.** `difficulty` may be a short string or an integer from 0 to 100.
    Unknown keys are ignored. Feud `aliases` is a required array (it may be empty).
