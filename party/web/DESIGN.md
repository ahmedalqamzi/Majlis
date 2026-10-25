# Majlis Party web design spec (Kimi k3.1-pro)

Provenance:
- Written by Kimi Code CLI, model `kimi-code/k3.1-pro` (provider openai, model k3.1-pro), session
  `session_0aa5b8df-aee0-4f11-9aba-cebf86170cf1`. Design only: in that session Kimi used only Read on
  its two inputs, the frozen protocol v1 contract and the head of the existing Majlis trivia page styles.
- Run 1 started 2026-10-24T15:37:34Z and stopped at the 16,384-token output cap. Run 2 resumed the same
  session at 15:46:02Z and finished the spec. Raw outputs below, verbatim and joined at the cut.
  sha256 run 1 `4eff9ed226be5d2f93a61a35cf7750e7a83a2b0b2b3da75d9130f81e4480d898`,
  run 2 `777e752d6407d3d4944cbe9d9de6731b92a9677207b4dd69ea51fb2626c533d1`.
- Code that implements this spec was written by Claude Opus 5.5 (Wonder).

Deviations in the implementation:
- Who Am I "Correct +1" is not behind a confirm dialog, to keep play quick. "Not it" undoes a pending
  guess. Destructive actions (end game, close room, award Feud points, reveal all) do confirm.
- The QR and join link also carry `&sim=1` in simulated mode only, so phones join the same fixture.
  That flag is not a role or an identity. Real mode encodes only `play.html?code=XXXXXX`.

---

# Majlis Party — visual + interaction specification

Same brand family as Majlis trivia: deep navy surfaces, warm gold primary actions, coral strikes/destructive actions, rounded game-show cards, and restrained motion. Operator and display are never treated as players.

## A. Design tokens

```css
--bg:#0D2742; --s1:#15365A; --s2:#1C426B; --s3:#25507E;
--line:#2C5A8A; --ink:#FFFFFF; --muted:#A8C2DD;
--pri:#FFC93C; --pri-ink:#2A1C00; --pri-edge:#C9930E;
--acc:#FF6B6B; --acc-ink:#2B0606; --good:#51CF66; --info:#4DABF7;
--tile:#FFFFFF; --tile-ink:#0D2742; --tile-edge:#C9D6E4;
--muted-on-tile:#4D6A88; --scrim:rgba(5,15,28,.78);
--t1:#FF6B6B; --t2:#4DABF7; --t3:#51CF66; --t4:#CC5DE8;
--shadow:0 10px 30px rgba(0,0,0,.25);
--r-sm:10px; --r:14px; --r-lg:22px; --r-xl:28px; --pill:999px;
--sp1:4px; --sp2:8px; --sp3:12px; --sp4:16px; --sp5:20px;
--sp6:24px; --sp8:32px; --sp10:40px; --sp12:48px;
```

- Fonts, with no download required: `--display:"Baloo Bhaijaan 2","Arial Rounded MT Bold","Trebuchet MS",sans-serif`; `--body:"Rubik","Segoe UI",system-ui,-apple-system,Tahoma,sans-serif`. Baloo/Rubik are optional installed-font first choices, not dependencies.
- Phone type: page title `32/36`, section title `24/30`, button `18/22 700`, body `16/24`, secondary `14/20`, labels `13/18 600`, room code `48/52 800`, buzzer `32/36 800`. Use tabular numerals for scores, points, and timers.
- TV type scales from 1280×720 to 1920×1080: room code `clamp(48px,8.89vh,96px)`; question `clamp(36px,6.67vh,72px)`; answers `clamp(28px,4.44vh,48px)`; names/scores `clamp(20px,3.33vh,36px)`; metadata `clamp(18px,2.5vh,27px)`. Never shrink answers below 28px to make a board fit.
- Phone content: maximum 480px, 16px gutters, safe-area-aware top/bottom padding. Operator: maximum 1180px; Feud becomes board + 320px control column above 800px. TV: 24px at 720p and 36px at 1080p outer gutters.
- Primary buttons are gold with dark text and a 5px bottom edge; press moves down 3px. Secondary buttons use `--s2`; destructive actions use coral with `--acc-ink`, **not white text**.
- Focus: `3px solid #FFC93C`, 3px offset on dark surfaces; use `#0D2742` on gold/white surfaces. Never remove focus outlines.
- Motion: press 90ms, hover/state 160ms, banners/toasts 200ms, answer flip 360ms, strike 420ms; standard easing `cubic-bezier(.2,.8,.2,1)`. With `prefers-reduced-motion:reduce`, remove pulses, shakes, 3D flips, parallax, and confetti; apply state changes immediately.

## B. Layouts and wireframes

All screens share a compact header containing brand/mode, phase where relevant, and a textual connection indicator. The operator entry is simply optional operator name + gold **Create room**, followed by this lobby.

### Operator lobby

```text
┌ Majlis Party                         ● Connected ┐
│ JOIN WITH ROOM CODE                              │
│ K7M4Q2                         ┌───────────────┐ │
│ Six characters                 │      QR       │ │
│                                └───────────────┘ │
│ PLAYERS (4)                     Scan to join     │
│ ● Aisha   Connected    ● Omar   Connected        │
│ ○ Salman  Disconnected ● Noora  Connected        │
├ CHOOSE MODE ─────────────────────────────────────┤
│ (•) Who Am I — Playable   ( ) Majlis Feud        │
│ [Impostor — Not implemented yet] [Draw & Guess…] │
│ [All remaining unavailable cards remain visible] │
├──────────────────────────────────────────────────┤
│ [Start Who Am I — gold]              [Room menu] │
└──────────────────────────────────────────────────┘
```

- Empty list copy: “No players yet — share the code.” Show connected/disconnected with dot **and text**.
- Start is the sole gold lobby action; disable with an explanatory requirement such as “At least 3 players needed.” Room menu contains destructive **End room**.
- QR has a white quiet zone of at least four modules and an accessible label. It encodes only the player join URL containing `?code=K7M4Q2`.

### Operator — Who Am I

```text
┌ Who Am I · Round 2 · K7M4Q2          [Pause] ┐
│ ASSIGNMENTS — OPERATOR ONLY                   │
│ ● Aisha · 10 points · Ready                   │
│   Omar / عمر                   [Award correct] │
│ ● Noora · 20 points · Guess pending           │
│   Pearl / لؤلؤ                [Award correct] │
│ ● Salman · 0 points                           │
│   Falcon / صقر                [Award correct] │
├───────────────────────────────────────────────┤
│ [Reveal characters — gold] [Next round]       │
│ [Pause]                             [End room] │
└───────────────────────────────────────────────┘
```

Show every assignment, both labels when `ar` exists, connection, score, and any ready/guess state supplied by the snapshot. **Reveal characters** requires confirmation; **Award correct** confirms player name + points before submission. Next round becomes gold only when it is the recommended valid phase action.

### Operator — Feud

```text
┌ Majlis Feud · Round 2 · K7M4Q2       [Pause] ┐
│ HOUSE ANSWERS · Question wraps across 2 lines │
│ Buzzer: OPEN / Aisha buzzed first             │
│ [Open buzzer — gold] [Reset buzzer]           │
├ FULL BOARD — OPERATOR ONLY ───────────────────┤
│ 1  Revealed answer                  30 [Shown] │
│ 2  Hidden answer text               24 [Reveal 2] │
│    Aliases: alias one / alias two              │
│ 3  Hidden answer text               18 [Reveal 3] │
├ ROUND CONTROL ────────────────────────────────┤
│ Strikes: X  X  3rd-slot-empty                  │
│ [Strike +] [Steal]                             │
│ Award to: [Choose player ▾] [Award 54 points]  │
│ Scores: Aisha 54 · Noora 30 · Salman 0         │
│ [Next round] [Pause]               [End room]  │
└───────────────────────────────────────────────┘
```

Phone control order is question → buzzer → full answer/alias list → strikes/steal → player award → scores → next/pause/end. On wider screens the answer list is main content and the remaining controls form the side column.

- Only the current recommended phase action is gold. Open, reveal-one, strike, steal, pause, and resume need no confirmation.
- Confirm **Reset buzzer** only when it would clear a recorded winner; confirm **Next round** only if points/unresolved play would be abandoned.
- **End room** is coral, spatially separated, and always confirmed with Cancel initially focused.
- Strike disables at three. All availability and scoring still follow server validation; never optimistically reveal, strike, steal, or award.

### Player — join and lobby

```text
┌ MAJLIS PARTY ────────────────┐      ┌ ROOM K7M4Q2 · Connected ┐
│ Join the game                │      │ You're in, Aisha!        │
│ Room code                    │      │ Waiting for the operator │
│ [ K7M4Q2                   ] │      │ to choose and start…     │
│ [stable code error here]     │      │ PLAYERS                  │
│ Your name                    │      │ ● Aisha (you)            │
│ [ Aisha                    ] │      │ ● Noora   ○ Salman       │
│ [stable name error here]     │      │                          │
│ [Join room — gold]           │      │ Keep this page open.     │
│ Use the same Wi-Fi as host.  │      │                          │
└──────────────────────────────┘      └──────────────────────────┘
```

Code is prefilled from `?code=`, editable, six characters, uppercase-normalized, `dir=ltr`, with a visible label. Name uses `dir=auto`. Join changes to **Joining…** while pending; preserve both inputs after rejection.

### Player — Who Am I

```text
┌ Who Am I · Round 2                  ● Connected ┐
│ YOUR CARD                                      │
│                 ?                              │
│ Your character is hidden - ask yes/no questions│
│ OTHER PLAYERS                                  │
│ ● Aisha · 10                                   │
│   Omar / عمر                                   │
│ ● Noora · 20                                   │
│   Pearl / لؤلؤ                                 │
│                     [I'm ready]                │
│             [I think I know — gold]            │
└─────────────────────────────────────────────────┘
```

Own mystery card stays first; other-player rows include name, connection, score, and assignment. Actions are sticky above the safe area, await the server, and never reveal anything locally. Guess may use one confirmation sheet: “Say your guess aloud; the operator decides.” In reveal/end, replace `?` with own character **only if present in the new authorized projection**.

### Player — Feud

```text
┌ HOUSE ANSWERS · Round 2             ● Connected ┐
│ Question text, comfortably wrapped              │
│ [1  Revealed answer                       30]   │
│ [2  ?                                     ]     │
│ [3  ?                                     ]     │
│ Strikes: X  X  X                                │
│ Aisha buzzed first                              │
│ Scores: You 20 · Aisha 54 · Noora 30            │
│ ┌─────────────────────────────────────────────┐ │
│ │                   BUZZ!                     │ │
│ └─────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

Buzzer is full-width, minimum 152px high, sticky above the safe area. Its five states are:

- **Locked:** `--s2`, “Buzzer locked,” disabled.
- **Open:** gold/dark, “BUZZ!”, enabled; a subtle 1.6s ring pulse, stopped on any result.
- **Sending:** `--s3`, spinner + “Sending…”, disabled and `aria-busy=true`; no winner inferred.
- **You won:** green/navy, check icon + “You buzzed first!”, disabled.
- **Someone else won:** soft blue surface, player name + “buzzed first,” disabled; not presented as an error.

Hidden slots contain only rank and `?`; answer text, aliases, and points appear only after that answer is revealed.

### Display — lobby and Who Am I

```text
┌ MAJLIS PARTY                    Connected ┐   ┌ WHO AM I · ROUND 2 · ASK & GUESS ┐
│ JOIN THE MAJLIS                           │   │ Aisha       10     ● Connected    │
│ K7M4Q2                  ┌───────────────┐ │   │ Noora       20     ● Connected    │
│ Scan to join            │      QR       │ │   │ Salman       0     ○ Offline      │
│ 4 players connected     └───────────────┘ │   │                                │
│ Aisha · Noora · Omar · Salman             │   │ Waiting for operator reveal…    │
│ Waiting for operator to start…            │   │                                │
└───────────────────────────────────────────┘   └────────────────────────────────┘
```

During play, display Who Am I contains
only player names, scores, connection state, and phase. No character labels, assignment placeholders, or hidden answer content. During reveal/end, character labels may appear only when included in the display’s authorized projection.

### Display — Feud board

```text
┌ HOUSE ANSWERS · ROUND 2                  STRIKES: X X · ┐
│ Question text, large and wrapped across at most 2 lines │
│ ┌─────────────────────────┐ ┌─────────────────────────┐ │
│ │ 1  Revealed answer   30 │ │ 5  ?                    │ │
│ └─────────────────────────┘ └─────────────────────────┘ │
│ ┌─────────────────────────┐ ┌─────────────────────────┐ │
│ │ 2  Revealed answer   24 │ │ 6  ?                    │ │
│ └─────────────────────────┘ └─────────────────────────┘ │
│ ┌─────────────────────────┐ ┌─────────────────────────┐ │
│ │ 3  ?                    │ │ 7  ?                    │ │
│ └─────────────────────────┘ └─────────────────────────┘ │
│              AISHA BUZZED FIRST / BUZZER OPEN            │
│ Aisha 54       Noora 30       Omar 20       Salman 0     │
└──────────────────────────────────────────────────────────┘
```

- Use a full-height grid: header/question, flexible answer board, buzzer status, scores. Two answer columns for 5+ answers, one for 4 or fewer; preserve server rank order in an ordered list, without CSS visual reordering.
- Revealed slots are white/navy with a gold points badge; hidden slots are numbered navy blanks with no points. Keep question and answer text wrapped, never ellipsized.
- Buzzer status always comes from the server. Display has no gameplay controls, aliases, or operator-only information.

### End / scoreboard

```text
┌ GAME ENDED ──────────────────────────────┐
│                 MAJLIS CHAMPION           │
│                   AISHA                   │
│ FINAL SCORES                              │
│  1  Aisha                         120     │
│  2  Noora                          95     │
│  3  Omar                           70     │
│ Operator: [Create new room]               │
│ Player:   [Back to join]                  │
│ Display:  final scores remain on screen   │
└───────────────────────────────────────────┘
```

Use the authoritative scores, visibly label ties, and mark winners with text as well as gold. Ended views stop ordinary gameplay controls and reconnection loops.

### Shared state shell

- **Connecting:** centered spinner and “Connecting to room…”; disable actions until the first authorized snapshot.
- **Connected:** dot plus “Connected”; never communicate connection through colour alone.
- **Reconnecting:** banner reading “Reconnecting… showing last update.” Keep the last snapshot fully legible; dim and disable only its controls. Retry automatically and offer **Retry now**.
- **Transient error:** “Couldn’t reach the room. Check your connection and try again.” Preserve the last state and show **Retry now**; without prior state, use a centered error panel.
- **Paused:** “Paused by operator” banner; keep the board visible, disable player actions, and make **Resume** the operator’s gold action.
- **Not found:** inline code error, “Room not found. Check the 6-character code,” with **Check again**.
- **Name taken:** “That name is already taken. Choose another.” Focus the name input and retain the code.
- **Room full:** “This room is full. Ask the operator for another room,” with **Check again** and **Back**.
- **Room closed:** “This room is closed. Ask for a new code.” Replace gameplay with a terminal panel; do not retry indefinitely.
- Map stable server error codes to this copy; never display stack traces or arbitrary raw error payloads.
- Fixture mode adds a persistent, nonanimated top ribbon on every viewport: gold background, navy 12px bold text, exact copy `SIMULATED FIXTURE - not a real server`. Keep it in normal/sticky layout flow so it never covers content or connection banners.

## C. Interaction details

- On an open-buzzer tap, show pressed feedback immediately, optionally call `navigator.vibrate(20)`, then show **Sending…**. Disable duplicate taps and reuse the same `actionId` for uncertain network retries.
- Render the winner only from the action result plus authoritative projected snapshot. Where supported, a server-confirmed own win may vibrate `[40,50,40]`; another winner may use one short pulse. Haptics are optional and never the sole feedback.
- Answer flip: rotate the visible blank 0→90° over 180ms, populate the newly authorized revealed content at the midpoint, then rotate 90→0° over 180ms. Never pre-populate a hidden backface.
- Strike: new coral X scales `.7→1.08→1` with a 2px tray shake over 420ms; no full-screen flash. Existing strikes remain static.
- Animate only newly revealed answer IDs or newly added strikes. Initial load, reconnect, and snapshot refresh must not replay animations.
- Toasts appear above phone sticky controls, at operator top-right, and below TV content. Success toasts last about 3 seconds; queue at most two and never cover the buzzer. Important errors remain inline until resolved.
- All interactive targets are at least 48×48 CSS px with 8px separation; inputs are at least 52px high. No hover-, swipe-, or long-press-only operation.
- Keep the overall English UI `dir=ltr`. Use `dir=auto` on player names, character labels, questions, answers, name input, and user-content toasts; wrap names inside `<bdi>` when embedded in sentences.
- When `ar` exists, show English and Arabic on separate lines; mark Arabic `lang="ar" dir="rtl"`. Do not hide it behind a tooltip. Use logical CSS properties, allow wrapping, and avoid `row-reverse` or truncation.
- Room codes remain `dir=ltr` with letter spacing; points and scores use tabular numerals. Arabic text receives approximately 1.5 line-height.

## D. Accessibility

- Use landmarks, real headings, ordered answer lists, native buttons, labelled inputs, and native radios for playable modes.
- Connection state uses one `role="status" aria-live="polite" aria-atomic="true"` region; announce transitions, not every revision. Blocking errors use `role="alert"`.
- The time-critical server-decided buzzer result uses a dedicated `role="alert" aria-live="assertive" aria-atomic="true"` region. Do not also make the button and scoreboard live, which would duplicate announcements.
- Hidden answer accessible text is “Answer 2, hidden.” A separate polite live region announces only new reveals: “Answer 2 revealed: [text], 24 points.” Announce strikes as “Strike 2 of 3”; decorative Xs are `aria-hidden`.
- Join errors use `aria-invalid` and `aria-describedby`; focus the relevant field after submission failure.
- Operator controls work in logical DOM order with Tab/Shift+Tab and Enter/Space. Labels include context, such as “Reveal answer 3” and “Award 54 points to Aisha”; there are no single-letter shortcuts.
- Confirmation dialogs trap focus, close with Escape, return focus to their invoker, and initially focus Cancel for destructive actions. If an action removes its invoking control, move focus to the nearest stable heading or status.
- Minimum contrast: 4.5:1 for normal text and 3:1 for large text, focus indicators, and meaningful graphics. Useful pairs: white/navy ≈15.2:1; muted/navy ≈8.3:1; primary-ink/gold ≈10.8:1; accent-ink/coral ≈6.7:1; navy/green ≈7.5:1.
- White on coral and gold on white do not meet normal-text contrast. Team colours are decorative accents unless their text pairing is separately verified.

## E. Not-yet-implemented mode picker

- Playable Who Am I and Majlis Feud cards are selectable radios with a visible **Playable** badge.
- Show all eight, in this order: Impostor, Draw & Guess, Drawing Chain, Bluff, Werewolf, Taboo, Wavelength-style, Escape Room.
- Each unavailable card is noninteractive with `aria-disabled="true"`, a visible **Not implemented yet** badge, and the exact supporting line **“Coming later - not playable yet”**.
- Use a navy surface, readable muted text, and dashed border rather than fading the whole card. Do not provide fake previews, sample gameplay, progress percentages, player counts, or start buttons.
- Use one column on phones and two on larger operator screens; all unavailable modes remain reachable through ordinary scrolling, not a hidden carousel.

## F. Privacy-in-UI rules

- Before reveal/end, a player’s own Who Am I assignment must be absent from their payload and DOM. Do not place it in hidden elements, `aria-label`, `data-*`, script JSON, CSS backfaces, comments, or offscreen containers.
- Players and operator may render other assignments as authorized; display renders none before reveal/end.
- Player/display Feud projections contain only individually revealed answer text and points. Never retain the full bank, unrevealed points, aliases, or accepted-answer metadata in their DOM or client fixture state.
- `display:none`, `aria-hidden`, zero opacity, and conditional CSS are not privacy boundaries. If unauthorized content is present, the projection is already wrong.
- Fixture/mock data must follow the same role projections as real snapshots; the simulation ribbon never excuses secret exposure.
- Tokens never appear in visible UI, URLs, QR codes, logs, errors, toasts, analytics, or markup. Use them only in authorization headers and private credential handling; a room code is not authorization.
- QR content is solely the join URL with `?code=XXXXXX`. Do not add role, player identity, or token parameters.
- Render only the latest authorized role snapshot. On room, role, or identity change, remove secret-bearing DOM from the previous view; reconnect may retain only the last projection authorized for that same identity.
- Keep **HOUSE ANSWERS** visible on every Feud board. Never add survey claims, respondent counts, or fabricated sources.
- Display remains read-only with no debug/operator panel; operator remains a trusted nonparticipant and never appears in the player score list.