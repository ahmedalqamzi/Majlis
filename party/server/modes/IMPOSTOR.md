# Impostor mode: wire reference (backend candidate N03-I)

Module: `party/server/modes/impostor.py`. Mode id `impostor`, action prefix `impostor.`.
This follows the frozen supplement `impostor-v1.md` (Skull, 24 October 2026). It is an
engine + HTTP/SSE candidate only. **No frontend renders it yet, and it has not been accepted
as a playable game on any browser, phone, TV or network.**

Everything below sits inside the generic room protocol in `../README.md`: same envelope
`{actionId, type, roundId?, payload?}`, the same per-principal replay log, the room lock and
receipt sequence, `paused`/`stale_round`/`round_required`/`wrong_mode` checks in
`room_logic.reduce_room`, and the same `project_room` snapshot. Only `game` is mode-specific.

## Content

Pack `impostor.json` with the standard envelope `{schemaVersion:1, mode:"impostor", title,
licenseNote, items}`. Each item is:

| Field | Rule |
| --- | --- |
| `id` | non-empty string, max 64; unique within the pack (`items[i].id: duplicate id`) |
| `word` | non-empty string, max 80, printable (no control/bidi-override characters) |
| `category` | non-empty string, max 64 (not unique; never projected) |

* **Duplicate word semantics.** Two items whose `word` has the same *word key* make the pack
  `content_invalid`, with the issue `items[j].word: duplicates normalized word of items[i]
  (case/whitespace-insensitive)`. The issue never contains the word.
* Unknown item keys (for example `aliases`, `hint`) and unknown top-level keys (the authored
  pack's informative `rules` block) are dropped at load time and never projected. In
  particular the authored `rules.actions` names (`impostor.start_round`, `impostor.end`)
  are **not** wire actions; the supplement's action set below is authoritative.
* Minimum 1 item; each round consumes one. The authored pack has 40 items, so a game
  can have at most 40 rounds.

**Word key** (used for duplicate detection, the "unseen" rule and guess matching):
Unicode NFC, then `str.casefold()`, then whitespace collapsed to single spaces and trimmed.
Nothing else. There is no NFKC/width folding, no diacritic stripping, no punctuation removal,
no plural/stemming, no substring or edit-distance matching and no aliases. So `" kARAK
  tea "` matches `Karak tea`, but `Karak`, `Karak teas`, `karak-tea`, `Kárak tea` and
fullwidth `Ｋａｒａｋ ｔｅａ` do not. NFC only identifies canonically equivalent encodings of
the same characters (precomposed `é` and `e` + combining acute).

## Players and roles

* 3–12 players (`MIN_PLAYERS = 3`; the room's hard bounds are 3–12). Fewer gives
  `not_enough_players` on `room.start`.
* Each round the server picks **exactly one** impostor among the players. The pick is
  `random.Random("impostor:<seed>:<gameNo>:<round>")` over seating order, where `seed` is the
  64-bit `secrets` value drawn by the manager for `room.start`. It is a fresh random draw every
  round, so the same player can be impostor twice in a row; nobody is excluded. The seed,
  deck and pick never leave the server.
* The deck is the pack shuffled once per game with the same seed. `round.next` draws the next
  item whose word key has not been dealt in this game. Words never repeat within one game
  (`room.start` → `room.end`/`room.reset`/`room.close`). A new `room.start` reshuffles the
  whole pack.

## Round phases (`game.roundPhase`)

```
room.start ──► discussion ──impostor.advance──► vote
                                                 │  every player voted, or impostor.advance
                                                 ▼
                         ┌─ nominee == impostor ─► guess_word ─┬─ impostor.guess_word (match)  ─► reveal  impostor +3
                         │                                     ├─ impostor.guess_word (no match) ─► reveal  each crew +2
                         │                                     └─ impostor.advance (forfeit)    ─► reveal  each crew +2
                         └─ no nominee / nominee is crew ──────────────────────────────────────► reveal  impostor +3
reveal ──round.next──► discussion (next round)          any phase ──room.end──► ended
```

* **Nomination:** the player with the unique strictly highest non-zero tally. A tie at the top,
  or zero votes, nominates nobody.
* **Scoring** happens exactly once per round, keyed by `roundId` (`game.awards` server-side),
  independent of `actionId`. Every score is computed by the server; clients never send points.
  The impostor gets 3 and crew 0 for `impostor_survived`/`impostor_guessed_word`. Each crew
  player gets 2 and the impostor 0 for `crew_wrong_guess`/`crew_forfeit`.
* **No timers.** Discussion is spoken in the room, not recorded or transcribed. The operator
  moves a stalled round on with `impostor.advance`.
* `room.end` in any phase ends the game. An **unresolved** round (discussion, vote or
  guess_word) gets `outcome: "aborted"` with no winner and no points, and its word and
  impostor are revealed. Ending in `reveal` keeps that round's outcome and adds nothing.
* `room.pause` freezes the round: every `impostor.*` action and `round.next` returns `paused`
  until `room.resume`. Votes, roles, word and phase are unchanged. `room.reset` and
  `room.close` discard the game exactly as for other modes. Scores already synced to
  `players[].score` are kept by `room.reset` unless `clearScores`.

## Actions

All `impostor.*` actions and `round.next` require the current `roundId` (`round_required`
when it is missing, `stale_round` when it differs). Round ids are `g<game>r<round>`, for
example `g1r1`, `g1r2`, `g2r1`.

| Type | Role | Phase | Payload | Effect |
| --- | --- | --- | --- | --- |
| `impostor.advance` | operator | `discussion` | `{}` | → `vote` |
| `impostor.advance` | operator | `vote` | `{}` | Resolve with the votes cast so far (none → no nominee). |
| `impostor.advance` | operator | `guess_word` | `{}` | Forfeit the guess: `crew_forfeit`, each crew +2, → `reveal`. |
| `impostor.vote` | player | `vote` | `{playerId}` | Record the token's player's single vote for another player. When every player has voted the round resolves at once. Any voter id in the payload is ignored; the voter is always the token's player. |
| `impostor.guess_word` | player (the caught impostor) | `guess_word` | `{text}` (1–80 chars after NFC + whitespace collapse) | Compare word keys. Match: `impostor_guessed_word`, impostor +3. Otherwise: `crew_wrong_guess`, each crew +2. → `reveal`. One guess only. |
| `round.next` | operator | `reveal` | `{}` | Next unseen word, fresh impostor pick, votes/nominee/outcome/points cleared, → `discussion`. |

Every player votes, the impostor included. Votes cannot be changed or withdrawn.

### Check order and errors (after the room-level checks)

Room level first (`../README.md`): `display` → `forbidden`; closed room → `room_closed`;
mismatched `roundId` → `stale_round`; `paused`; not playing → `invalid_phase`; other mode's
prefix → `wrong_mode`; no `roundId` → `round_required`. Then in the engine:

| Action | Checks in order → code |
| --- | --- |
| any | non-object payload → `invalid_payload`; game ended → `invalid_phase` |
| `impostor.advance` | not operator → `forbidden`; in `reveal` → `invalid_phase` |
| `impostor.vote` | not a player of this game → `forbidden`; phase ≠ `vote` → `invalid_phase`; `playerId` not a string id of a player in this game → `unknown_player`; own id → `self_vote`; already voted → `already_voted` |
| `impostor.guess_word` | not a player → `forbidden`; phase ≠ `guess_word` → `invalid_phase`; not the caught impostor → `forbidden`; bad `text` → `invalid_payload` |
| `round.next` | not operator → `forbidden`; phase ≠ `reveal` → `invalid_phase`; no unseen word left → `content_exhausted` |
| other `impostor.*` | `unknown_action` |

Before the vote resolves, no check depends on the actor's secret role. Impostor and crew
get the same code for the same request in `discussion` and `vote`, so an error cannot
reveal who the impostor is. The only role-dependent check (`guess_word` by a non-impostor →
`forbidden`) runs once the caught nominee is already public.

New codes added by this mode (409, fixed messages without request or secret data):
`self_vote` ("Players may not vote for themselves."), `already_voted` ("This player has
already voted this round."). Reused codes: `forbidden` 403, `invalid_phase` 409,
`invalid_payload` 400, `unknown_player` 400, `unknown_action` 400, `stale_round` 409,
`round_required` 400, `paused` 409, `content_exhausted` 409, `not_enough_players` 409,
`content_missing`/`content_invalid`/`content_insufficient` 409. `already_awarded` 409 is
defensive and unreachable through the public transitions.

**Replay.** Replays use the generic per-principal log: the same `actionId` returns the stored
status and body byte-for-byte with `X-Action-Replayed: 1`, applies nothing and never awards
again. That holds after resolution and after `round.next` too. A *new* `actionId` after
resolution is a new request and is rejected by phase (`invalid_phase`), or by round
(`stale_round`) once the round has moved on.

## Projection (`game`)

Fields for **every** viewer (operator, display, each player):

| Field | Type | Meaning |
| --- | --- | --- |
| `title` | string | Pack title |
| `round` | int | 1-based round number in this game |
| `roundPhase` | `"discussion"\|"vote"\|"guess_word"\|"reveal"\|"ended"` | |
| `order` | `[playerId]` | Seating (join) order |
| `votes` | `{voterId: targetId}` | Votes cast so far this round, in seating order of voters. Player ids only. |
| `tally` | `{playerId: int}` | Votes received, for every player |
| `votesNeeded` | int | Number of players (every player votes) |
| `nominee` | `playerId\|null` | Set when the vote resolves (null for a tie/no votes). In `guess_word` this is the caught impostor, as gameplay requires. |
| `outcome` | `null\|"impostor_survived"\|"impostor_guessed_word"\|"crew_wrong_guess"\|"crew_forfeit"\|"aborted"` | null until the round is resolved or ended |
| `roundPoints` | `{playerId:int}\|null` | Points this round gave each player; null until resolved (stays null for `aborted`) |
| `reveal` | `null\|{word, impostor}` | null until `reveal` or `ended`; then the round's word and impostor id for everyone |

Role-specific additions:

| Viewer | Extra fields |
| --- | --- |
| operator | `roundsRemaining`: int, unseen words left after this round |
| display | none |
| crew player | `you: {role:"crew", word}` (its own word only) |
| impostor | `you: {role:"impostor"}` (no word, id, category or anything derived from the item) |

Guarantees (tested on every snapshot, SSE event, raw SSE byte stream and action/error body in
every phase, see `tests/test_impostor_http.py` and `tests/test_impostor_reducer.py`):

* Before `reveal`/`ended`, the operator, the display and the impostor receive **no** pack
  `word`, `id` or `category`, of this round or any other. A crew player receives only its own
  `word`, and only at `game.you.word`.
* No viewer ever receives a role map. Before reveal, no viewer receives anyone's role except a
  player's own `you.role`. After reveal, everyone receives only `reveal.impostor`.
* Item `id` and `category` are never projected, not even after reveal.
* Apart from `you` (and the operator's `roundsRemaining`), every viewer's `game` is identical,
  and it does not depend on which seat holds the impostor role.
* Guess text is never relayed to any view, before or after reveal. Only `outcome` shows
  whether it matched.
* Error messages are fixed strings (`errors.py`).

## Not covered

* No frontend, QR, TV or phone rendering exists for this mode in this candidate. Nothing here
  claims a playable third game until UI, network and device acceptance is done.
* There are no timers, no discussion recording, no chat and no multi-round match scoring
  beyond the room's cumulative `players[].score`.
* Player names and `operatorName` are typed by people and relayed as typed. A person who
  types the word as a name is communication, not a server leak (same policy as Who Am I).
* Words are not deduplicated across games in the same room: each `room.start` reshuffles the
  full pack.
* A player who joins between games plays the next game. Mid-game joins are refused
  (`game_in_progress`), as in other modes.
* The impostor pick is uniformly random per round, not a rotation, so the same player can be
  picked in consecutive rounds.
