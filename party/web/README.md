# Majlis Party web frontend (first pair: Who Am I + Majlis Feud)

Static operator, player and TV pages for the Majlis Party room server, protocol v1. They are served
same-origin by the in-repo server `party/server` (the accepted first-pair backend, archive sha256
`4338932740f8e52f6a99a06fa7d4776c4119960d8d9700483657576a41c1770a`). The contract is Skull's frozen
`CONTRACT.md` (sha256 `a30cfe5f8a5fb3db489e078a40e2cf80a29478797ef2f41c1e7aaf3a799fbf53`), as resolved by
`server-review/INTEGRATION-DECISIONS.md`. This tree is separate from `squareup/` (Majlis trivia) and
does not change it.

| Page | Who uses it | Credential |
|---|---|---|
| `index.html` | everyone | none |
| `host.html`, then `host.html#code=XXXXXX` | the operator, on a phone or laptop | operator token from `POST /api/rooms` |
| `play.html?code=XXXXXX` (the QR) | each player, on their own phone | player token from `POST /api/rooms/{code}/join` |
| `tv.html#code=XXXXXX` | optional TV window opened from the host browser | display token from `POST /api/rooms` |

The server refuses any query string on `host.html` and `tv.html`, accepts only `?code=` on
`play.html`, and refuses `sim=1`. Host and TV therefore carry the room in the fragment, which never
reaches the server.

## Rules this frontend keeps

- The server decides everything: buzz order, scores, assignments, reveals. Pages send actions and
  render the role-projected snapshot they receive. No page holds full game state or picks a buzz winner.
- Tokens travel only in the `Authorization: Bearer` header, including the event stream (fetch-based SSE,
  not `EventSource`, so no token ever goes in a URL). They are stored in `localStorage` per role and room
  (`majlisParty.v1.{role}.{CODE}`) for resume, and never written to the DOM, URL or QR.
- The QR encodes only the join URL `play.html?code=XXXXXX`. In simulated mode it also carries `&sim=1`.
- Who Am I: a player's own character is never rendered before reveal. The page only draws what the
  server projection contains, and the server leaves the player's own assignment out.
- Feud: hidden answers are never in the player or TV projection; only the operator page shows the house board.
- Eight further modes appear in the operator's picker as "Not implemented yet" and cannot be chosen.

## Run

```
python3 -m party.server --web-dir party/web --content-dir party/content            # loopback only
python3 -m party.server --web-dir party/web --content-dir party/content \
  --host 0.0.0.0 --port 8080 --allow-lan --allowed-origin http://<lan-ip>:8080      # LAN preview
```

Run from the repository root with Python 3.9+. Open `/host.html` on the operator device. Players scan
the QR (same Wi-Fi for a LAN preview). The server serves only an allowlist of files, so new
JavaScript files must be added to its allowlist before they can load. GitHub Pages cannot serve
`/api/*`: the pages need this server, or an equivalent same-origin host.

Simulated fixture (UI development only): serve `party/web` with any static server and open
`host.html?sim=1`. Every page then shows the gold ribbon "SIMULATED FIXTURE - not a real server".
`sim/sim-worker.js` is a SharedWorker that imitates the server's wire shapes within one browser
profile. It is not the backend, the real server does not serve it, and its results are not evidence
that the real server works.

## Wire used (resolved with the accepted server)

- **Who Am I:**
  - Actions:
    - player: `whoami.guess {text}` (non-empty) and `whoami.ready {ready}`
    - operator: `whoami.award {playerId}`, `whoami.dismiss {playerId}`, `whoami.reveal {playerId?}` (no id reveals all)
    - operator: `round.next`
  - Projection:
    - `roundPhase` is `asking`, `revealed` or `ended`.
    - `assignments` is keyed by player id.
    - The operator gets the `guesses` map. A player gets `you.guess`, plus `you.assignment` once revealed.
    - Scores come from `players[]`.
- **Feud (house answers):**
  - Projections:
    - The operator board comes from `answers[]`.
    - Public `slots[]` carry a zero-based `index`, with `id`, `text` and `points` only once revealed.
    - Fields: `roundPhase`, `buzzer{open,winner}`, `control`, `stealFrom`, `roundPoints`, `awardedTo`, `awardedPoints`.
  - The page does no winner or scoring logic. `feud.award {playerId}` gives the server's current round points.
  - The Award confirm says "current N points". It is cancelled unsent if the round points change while it is open.
  - Validation is not atomic: the server awards whatever is revealed when the award arrives.
- **Round binding:**
  - Every action carries the `roundId` the page showed when the person acted.
  - Confirm dialogs are cancelled unsent if the round, or the round points for Award, changed while they were open.
- **Lifecycle:**
  - "Close room" in the lobby, and "Start a new room" after a game, send `room.close`.
  - `closed` is terminal: pages stop reconnecting, tell players "The host closed room X" and drop the credential.
  - "Leave room" and "Done" keep a player's credential, so "Back to room" on that phone resumes the seat. Nothing frees a seat or a name.
- **Connection:**
  - Fetch-based SSE carries `Last-Event-ID`.
  - Reconnect backoff runs 0.5–8 s, and "Retry now" reconnects at once.
  - The stream drops at once on the browser's `offline` event and retries on `online`.
  - A stream silent for 40 s (the server's heartbeat is 15 s) is treated as dead.
- **Buzzer:**
  - Acts on press (`pointerdown`), so a snapshot that re-renders the button mid-tap cannot swallow an on-time tap.
  - Keyboard and assistive-technology activation still arrive as a click.
  - Losing a race shows the winner on the buzzer, with no error toast.
- **Errors:** Server error codes map to plain messages in `js/ui.js` `describeError`. Fatal codes stop the stream.

## Tests

```
# Backend (113): from the repository root
python3 -m unittest discover -s tests

# Real network: starts party.server on a free loopback port and drives headless Chrome
MAJLIS_SERVER_PY=python3 python3 party/web/tests/real_e2e.py . <out-dir> [qr_decoder]
swiftc -O -sdk /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -target arm64-apple-macosx14.0 \
  party/web/tests/qr_decode.swift -o /tmp/majlis_qr_decode   # optional independent QR reader (CoreImage)
```

`real_e2e.py` needs Google Chrome and `websocket-client`.

**Setup.**
- The operator and the TV share one browser context.
- Each of three players gets its own isolated browser context, so storage is separate, like separate phones.
- Viewports: 393x852, 360x780, 734x393 (landscape), 1280x800 and 1920x1080.
- Clicks are trusted CDP input. Each is first hit-tested at the control's centre with `elementFromPoint`.

**What it checks.**
- Locators, QR decoding (CoreImage and BarcodeDetector) and join errors.
- Who Am I privacy of characters and guess text in every received byte and the DOM, before and after reveal, with positive controls.
- Feud:
  - hidden answers and aliases never reach the players or the TV
  - the trusted-touch buzz, and three near-simultaneous 3-way buzz races
  - strikes, steal, award and duplicate or stale awards
  - stale confirms
  - wrong-round, missing-round and wrong-role actions
  - pause, resume, end and close
  - `content_exhausted` on the last board
- Connection:
  - refresh in each role
  - offline, Retry and the return online
  - no new stream after the end
- A token leak scan of the DOM, URLs, console, CDP requests and the server access log.

These results are real-network on one Mac. They are not physical-phone or public-host evidence.

`sim_e2e.py` is the earlier simulated-fixture suite. Its 106 results belong to commit 7495a88, before
integration. It still asserts the pre-integration wire and locators (`tv.html?code=…&sim=1`, `who_am_i.*`,
`stage`), so on this tree it stops at its first old-locator step. It is kept for that record, and
`real_e2e.py` supersedes it.

## Accepted limits (protocol v1)

- **TV pairing.** The display token is issued to the operator and handed to `tv.html` only within
  the same browser ("Open TV display", then mirror or HDMI). A separate TV device would need a
  pairing step that protocol v1 does not have.
- **Change game.** Switching games needs a new room: after a game ends, the operator starts a new room
  and players rejoin with the new code or QR. There is no reset or back-to-lobby action.
- **After the game ends**, pages close their streams. A player who stays on "Game over" does not see a
  later room close until they reopen the room; "Back to room" then shows "Room closed".
- Physical-phone, LAN-from-another-device and public-hosting runs are separate gates, not covered here.
