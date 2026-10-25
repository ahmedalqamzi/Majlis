# party/native: L01B native host candidate

A Swift Network.framework HTTP/1.0 + SSE transport around one JavaScriptCore
context that runs the accepted L01A shared core (`party/core/majlis-core.js`,
`party/core/unicode13.generated.js`) plus the adapter `majlis-native.js`. It is
a reusable library-plus-CLI candidate for the operator-phone host. It is not
integrated into any iOS or Android app, wrapper or UI.

| Path | Role |
| --- | --- |
| `Sources/Engine.swift` | Bare `JSContext`; evaluates the three embedded scripts once, then only `JSValue.call(withArguments:)` on fixed `MajlisNative` functions with data-only arguments |
| `Sources/Server.swift` | Listener, connection state machine, routing, limits, SSE pump. One serial queue (`majlis.engine`) owns everything |
| `Sources/HTTP.swift` | Strict request-head parser and response writers |
| `Sources/Support.swift` | OS CSPRNG pools, monotonic clock, token-bucket limiters, static allowlist |
| `Sources/main.swift` | CLI, banner, signals, stdin operator bridge |
| `majlis-native.js` | Adapter: wire API, projection, replay, sessions, operator bridge |
| `build.sh OUT` | Builds `majlis-party-native` (production) and `majlis-party-native-test` (`-D MAJLIS_TEST_LOOPBACK`) with the Command Line Tools only |
| `harness/` | Acceptance harnesses (below). Not part of any shipped build |

## Surfaces

**production** (the only surface of `majlis-party-native`). LAN-capable with
`--allow-lan`. From the network it serves the player pages and the player API
only:

- `POST /api/rooms` does not exist (404 `not_found`). The network cannot issue
  host or display tokens.
- `/host.html`, `/tv.html`, `/js/host.js` and `/js/tv.js` are not served (404).
- Only player principals authenticate from the network. Operator actions
  (`room.*`, `round.next`, award/reveal/dismiss…) from a player bearer are 403
  `forbidden`. The core enforces this; the transport adds nothing.
- Rooms are created and driven only through the private in-process operator
  bridge (`--operator-stdin`). It reads one JSON request per stdin line and
  writes one JSON reply per stdout line:
  - `{"op":"create","body":{}}` → `{ok,op,code,revision}`
  - `{"op":"action","code":C,"body":{actionId,type,payload,roundId}}` → `{ok,op,status,result,replayed}`
  - `{"op":"state","code":C}` → operator view
  - `{"op":"forget","code":C}`

  The host token stays in the adapter (`S.operatorTokens`). The display token
  is dropped at creation. Neither is ever returned. Lines over 64 KiB are
  refused (`body_too_large`), and the same JSON parser with its depth guard
  applies. A future app embeds this as an in-process call; stdin is the CLI
  stand-in.
- `index.html` and `js/index.js` are still served. Their "create room" call
  now gets 404. The player page (`play.html`) is the intended entry point.
  Trimming the index page belongs to the app-integration increment.

**test-loopback** (only in `majlis-party-native-test`). This is the full
Python wire API, including `POST /api/rooms` with host and display tokens, so
the reference suites can run unchanged. The build refuses any non-loopback
bind (`test build binds loopback only`, exit 2). It also accepts
`--surface production`, which runs the production surface on loopback. It is
the only build with these:

- the stdin `#csprng-fail on|off` fault switch
- the `stats` bridge op (test-loopback surface only)

`build.sh` fails if the production binary contains any test marker string.

Optional TV pairing is deferred. There is no operator mirroring.

## Engine boundary

- No request text is ever passed to `evaluateScript`. Request bytes cross as
  `Uint8Array` and are parsed by the core's JSON parser:
  - strict UTF-8
  - Python `json` semantics (NaN is accepted, as in the reference)
  - depth guard: an outer object plus 899 inner arrays parses; 900 is
    `invalid_json`
- The JSContext has no injected natives, timers or console.
- All engine calls, timers and bridge lines run on one serial queue, so the
  core sees a total order. Time comes from a monotonic clock
  (`clock_gettime_nsec_np(CLOCK_MONOTONIC)`), never wall time.

## Entropy

Every operation draws a fresh pool from `SecRandomCopyBytes`, sized for its
worst case:

| Operation | Pool (bytes) | What it covers |
| --- | --- | --- |
| create | 160 | 6 × 16 room-code retries, plus host and display tokens of 32 each |
| join | 112 | 32 token, plus 5 × 16 player-id retries |
| action | 8 | |
| operator line | 160 | same as create |

If a draw fails, the operation answers `internal_error` without calling the
engine, so no partial credential or state can exist. Pool exhaustion inside the
core also throws `internal_error`. Pools are wiped with `memset_s` after use.
The attack suite proves both properties with the test-only fault switch: no new
room, session, player or revision while the switch is on, and normal service
once it is off.

## Transport limits (defaults)

| Limit | Default |
| --- | --- |
| Request deadline (head and body) | 15 s, never extended by trickled bytes |
| SSE write timeout | 10 s |
| Body | 16 KiB |
| Concurrent connections | 320; past that the raw `503 server_busy` from the reference |
| Streams | 4 per principal, 64 per room, 256 in total |
| Requests | 30/s, burst 120 per address |
| Joins | 1/min, burst 30 per address |
| Actions | 10/s, burst 30 per principal |
| Creates | 10/min, burst 10; test-loopback HTTP only, since the bridge has no network caller |

All limits are CLI flags with the same names as the Python server.

## Declared transport deviations from `party/server/httpd.py`

The core semantics are unchanged (see the L01A parity record). The transport is
deliberately stricter or simpler in these places:

1. **Strict head parser.** The reference accepts many of these; the attack
   suite records the reference's answers next to the native ones. Refused:
   - HTTP/0.9 two-part request lines and `HTTP/1.1x` (400)
   - HTTP/2+ (505)
   - obs-fold, control bytes and non-ASCII bytes in the head (400)
   - unknown methods (501)
   - `Transfer-Encoding` of any kind (411 `length_required`)
   - duplicate or non-digit `Content-Length` (400)
   - `//path` (404)
2. **Smaller head limits.** Request line 8 KiB (414), head 16 KiB or 100
   fields (431).
3. **Timeouts close silently.** A request that misses its deadline is closed
   without a `408` body. A peer EOF mid-body answers `bad_request`.
4. **SSE teardown is immediate** when the peer closes. The reference notices
   at the next heartbeat. A stalled reader is dropped after the write timeout.
5. **HTTP/1.0 responses with `Connection: close`**, one request per
   connection, exactly like the reference's effective behaviour.
6. **Bind** uses `allowLocalEndpointReuse` (the reference sets
   `SO_REUSEADDR`). A second instance on an occupied port still fails with
   `bind_failed` and exit 2.
7. **N5 (accepted in L01A):** the deliberate 900-container resource bound. At
   depth 900 the reference recurses and accepts, while native answers
   `invalid_json`. The historical raw comparison stays red on purpose. No
   threshold was tuned.

## Harnesses (`harness/`)

| Harness | What it proves |
| --- | --- |
| `native_shim.sh` + `reference_wire.py` | The reference wire unittest suite runs against the test-loopback build |
| `production_lan_e2e.py` | Production binary on the Mac's LAN address, driven by the operator bridge, with three isolated headless-Chrome players. Covers full Who Am I and Feud, buzz races, scoring, refresh/Last-Event-ID resume, close, and privacy scans of JSON, SSE, DOM, URLs, console and the access log |
| `attack_suite.py` | Raw-socket framing, path, query, Host/Origin/Sec-Fetch, body, Unicode, depth, auth, replay and stale-action probes, run against production LAN, native test-loopback and the Python reference. Also covers SSE caps and teardown, connection cap, deadlines and slowloris, rate limiters, expiry, CSPRNG failure, backpressure, bridge hardening and CLI refusals. Room state and health are checked after every rejected group |
| `negative_controls.py` | Builds one-defect mutants outside the checkout (Host guard off, room route opened in both the Swift router and the adapter, bridge token leak, access-log leak, depth guard off, room-action authorization off, connection cap off, CSPRNG failure ignored, SSE write timeout off, Feud answers projected to players). Each must turn its expected checks red. One extra layer check opens only the Swift room route: the adapter's `requireTest()` still refuses, so the harness must stay green |

All evidence redacts capabilities to `<token sha256-prefix>`.

## Limits of this evidence

Everything above ran on one Mac. "LAN" means a socket bound to the Mac's Wi-Fi
interface address with clients on the same host, so the traffic never crosses
the radio. This is not phone, Wi-Fi, offline, iOS background or WAN evidence.
