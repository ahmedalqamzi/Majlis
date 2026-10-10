# Majlis Trivia Game: Handover

Handover date: 10 October 2026 (Asia/Dubai).
Owner: Khaleefa.
Purpose: continue building this project on a different Claude account. Everything needed is in **Majlis-Handover.zip**. This file explains what exists, how it works, what is unfinished and what the owner has asked for.

---

## 1. What the project is

**Majlis** is an English team trivia party game for Android, inspired by the format of the Arabic app "Tarbaa / Tarabi3" (ترَبَّع), with a better UI and original questions.

**Game format**
- **Teams:** 2 to 4, sharing one device with a host.
- **Board:** each game uses 6 categories. Each category has 6 tiles: 200, 400 and 600, one of each on the left and right of the category card.
- **Turn:** a team picks a tile, the question opens full screen with a countdown timer (30, 45, 60 or 90 s).
- **Answer:** the host taps "Show answer", then taps which team got it right (any team can steal) or "No one".
- **Abilities:** each team drafts 3 of 16 abilities before the game, usable once each.
- **End:** when all 36 tiles are played, or the host ends early. The highest score wins.

**Current delivery**
- A single self-contained HTML file (`index.html`, about 1.9 MB) with all questions inlined.
- Published as a claude.ai Artifact on the old account. That link is private to the old account and cannot be edited from a new account. **The new account must republish it** (section 5).
- An Android Studio / GitHub Actions project that wraps the same HTML in a WebView. **No APK has been built yet** (section 6).

---

## 2. Status snapshot

| Item | Status |
|---|---|
| Categories | 85 of a target of 160+ (owner asked for at least 4x the original 37, so minimum 148) |
| Text questions | 8,471 |
| Picture questions | 158 (SVG, generated, copyright-free) |
| Video questions | 77 kept, verified as live YouTube videos (8 broken ones are excluded at build time) |
| Total in build | 8,621 |
| Arabic answers ("ar") | Present in 33 UAE / Arab categories |
| Abilities | 16 built and wired into scoring |
| Themes | 4 (Daylight default, Midnight, Gulf, Sadu) |
| 16:9 landscape layout | Built (for the unfolded Samsung Galaxy Z TriFold and tablets/TVs) |
| APK | Project ready, not compiled (blocked in the old workspace) |
| Web artifact | Published, version 5, private to the old account |

---

## 3. Package contents (Majlis-Handover.zip)

```
squareup/                      Game source and data
  HANDOVER.md                  This file
  majlis2.template.html        CURRENT app template (HTML + CSS + JS). Contains the placeholder __DATA__
  build.py                     Merges all data into the template; writes index.html and the Android asset
  index.html                   Latest built game (open in any browser to test)
  AGENT_RULES.md               Rules given to every question-writing agent (format, tiers, quality)
  q/<id>.json                  85 category question banks (one file per category)
  qimg/*.json                  Picture questions (SVG inline) merged into existing categories at build
  video_check_a.json           YouTube verification results (batch A)
  video_check_b.json           YouTube verification results (batch B)
  manifest.json, icon-192.png, icon-512.png   Web app manifest and icons (published with the artifact)
  app.template.html            OLD v1 template ("Square Up", navy/gold). Not used. Kept for reference only
  majlis.template.html         OLD v2 template (Sadu red/ochre only). Not used. Kept for reference only
majlis-android/                Android project (WebView wrapper)
  settings.gradle, build.gradle, gradle.properties
  app/build.gradle             compileSdk 34, minSdk 24, AGP 8.5.2, androidx.webkit 1.11.0
  app/src/main/AndroidManifest.xml
  app/src/main/java/com/majlis/trivia/MainActivity.java
  app/src/main/assets/index.html   Copy of the built game (build.py writes it)
  app/src/main/res/...         App name, theme, launcher icons (all densities)
  .github/workflows/build-apk.yml  GitHub Actions: builds Majlis.apk and attaches it to a Release
  README.md
```

Note: `build.py` runs from any folder and writes the Android asset to `../majlis-android/app/src/main/assets/index.html` relative to itself, so keep `squareup/` and `majlis-android/` side by side as they are in the zip.

---

## 4. Data format

### 4.1 Category file `q/<id>.json`
```json
{
  "id": "heritage",
  "name": "UAE Culture & Heritage",
  "questions": [
    {"q": "What is the craft of weaving date palm leaves into baskets and mats called?",
     "a": "Khoos", "p": 200, "ar": "الخوص", "alt": ["Khous"]}
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `q` | yes | Question text. For Emoji Guess it is the clue type (Movie, Country...) |
| `a` | yes | Short answer, 1 to 6 words |
| `p` | yes | 200, 400 or 600. Target split per category: 35 / 35 / 30 |
| `ar` | optional | Answer in Arabic script. Shown under the English answer |
| `alt` | optional | Up to 4 accepted alternatives: other spellings and Emirati / Gulf dialect variants. Shown as "Also accepted" |
| `yt`, `start`, `end` | optional | YouTube video id and clip window in seconds |
| `emoji` | optional | Emoji puzzle string, shown large (Emoji Guess category, not yet created) |
| `svg` | optional | Inline SVG picture (only in `qimg/` files) |

### 4.2 Picture file `qimg/*.json`
```json
{"id": "geography", "questions": [{"q": "Name this country from its outline.", "a": "Italy", "p": 200, "svg": "<svg viewBox=\"0 0 600 400\">...</svg>"}]}
```
`id` must match an existing category; questions are appended to it at build time. All SVGs use `viewBox="0 0 600 400"`, a white rounded background, and render as `<img src="data:image/svg+xml,...">`.

### 4.3 Build
```bash
cd squareup
python3 build.py          # prints "85 cats 8621 q"
node -e "const s=require('fs').readFileSync('index.html','utf8');new Function(s.match(/<script>([\s\S]*)<\/script>/)[1]);console.log('js ok')"
```
`build.py` does the following:
1. Loads both video check files; any question whose `yt` id is not marked `"ok"` is dropped.
2. Loads every `q/*.json`, keeping only known fields.
3. Appends every `qimg/*.json` question to its category.
4. Replaces `__DATA__` in `majlis2.template.html` with the JSON.
5. Writes `index.html` and the Android asset copy.

---

## 5. Republishing the web version on the new account

1. Unzip, run `python3 build.py`.
2. Publish `squareup/index.html` as a new Artifact, with supporting files `manifest.json`, `icon-192.png`, `icon-512.png` (same relative names), icon word "game".
3. The old artifact (`https://claude.ai/artifact/BGMS8LNcFpsx82fPGSfx3V`) belongs to the old account and cannot be updated from the new one.

**Artifact hosting limits found**
- Service workers are not supported, so the web version needs internet to open.
- YouTube iframes are blocked by the artifact's content policy. The app detects this through the `securitypolicyviolation` event and swaps to a "Play the clip" button that opens YouTube at the start time. In the APK the iframe works.
- `requestFullscreen` may be refused inside the Claude app viewer. The workaround is Chrome, then "Add to Home screen".
- Only Google Fonts load. The app uses Bricolage Grotesque (display), Readex Pro (body, supports Arabic) and Reem Kufi (Sadu theme display).

---

## 6. Getting the APK (not done yet)

The old workspace could not reach Google's Android servers (`dl.google.com`, `maven.google.com`) or Maven Central, and its GitHub token was invalid. So no APK was compiled. Two ready paths:

**A. GitHub Actions (fastest, recommended, about 7 minutes)**
1. Owner creates an empty private repo (e.g. `majlis-app`).
2. Push the contents of `majlis-android/` to it.
3. The workflow `.github/workflows/build-apk.yml` runs on push. It sets up Java 17 and the Android SDK, runs `gradle :app:assembleRelease` (Gradle 8.9, release signed with the debug key), and uploads `Majlis.apk` as an artifact and a GitHub Release `build-<n>`.
4. Owner opens the Release on the phone and installs `Majlis.apk` (allow "Install unknown apps").

There is no Gradle wrapper in the project because generating it offline failed. The workflow installs Gradle itself; Android Studio uses its own.

**B. Android Studio:** open `majlis-android/`, sync, then Build > Build APK(s).

**Android wrapper behaviour (MainActivity.java)**
- Serves `assets/index.html` through `WebViewAssetLoader` at `https://appassets.androidplatform.net/assets/index.html`, so localStorage works and YouTube embeds get a proper https origin.
- JavaScript and DOM storage on; media autoplay allowed; screen kept on.
- Links to other hosts (e.g. YouTube "Open on YouTube") open in the external browser.
- Back button calls `window.onAndroidBack()` in the page. It closes a modal, then the question, then moves draft to setup and setup/board/end to home. It returns false on home, so the app exits.

**For a Play Store release later:** add a real release keystore and signing config, bump `versionCode`, and consider an adaptive icon.

---

## 7. App architecture (`majlis2.template.html`)

Single file, vanilla JavaScript, no frameworks. Rendering is string templates into `#app`.

### 7.1 Views
`home` > `setup` > `draft` (abilities) > `board` > question overlay `#qv` > `end`. The current view is mirrored to `document.body.dataset.view` (used by the 16:9 CSS).

### 7.2 Storage (localStorage, all wrapped in try/catch)
| Key | Content |
|---|---|
| `mj2_setup` | team names, picked categories, timer, Surprise exclusions, ability picks per team, sound on/off |
| `mj2_game` | current game (teams, scores, abilities used, tiles, turn, undo history) |
| `mj2_used` | per category, indexes of questions already shown, so repeats are avoided until a tier runs out |
| `mj2_skin` | selected theme |

### 7.3 Setup screen
- **Teams:** 2 to 4, editable names, default "Team Falcon", "Team Oryx", then "Team Dhow", "Team Pearl".
- **Timer:** 30, 45, 60 or 90 s.
- **Categories:** grouped (section 7.7), with a search box.
- **Surprise exclusion:** the dice button on each card excludes **only that one category** from Surprise. The owner explicitly rejected group-level exclusion, so do not add it back. Excluding a category also unpicks it.
- **Surprise me / Reroll:** "Surprise me" picks 6 random non-excluded categories. Once picked, the button becomes "Reroll", which prefers 6 categories not in the previous pick.
- **Picked categories:** shown as removable chips.

### 7.4 Draft screen (abilities)
Each team must pick exactly 3 of 16. A "Random 3" button is available per team. Start is disabled until every team has 3.

### 7.5 The 16 abilities (all once per game)
| # | id | Name | When | Who can use | Effect as implemented |
|---|---|---|---|---|---|
| 1 | time | Extra Time | question | answering team | +30 s |
| 2 | double | Double Up | question | answering team | x2 if the answering team is correct |
| 3 | swap | Swap Question | question | answering team | Replace with an unused question of the same value |
| 4 | ease | Ease It | question | answering team | Replace with an unused 200-level question, keep tile points |
| 5 | hint | Hint | question | answering team | Shows first letter of each word plus blanks |
| 6 | two | Two Guesses | question | answering team | Label only (honour rule) |
| 7 | turn | Take the Turn | board (team sheet) | any team | That team becomes the picker now |
| 8 | first | First Steal | question | non-answering team | Label "X steals first" (honour rule) |
| 9 | pass | Pass It | question | answering team | Choose a team; it becomes the answering team |
| 10 | block | Block | question | answering team | Choose a team; its award button is disabled for this question |
| 11 | risk | Risk It | question | answering team | x3 if correct; lose tile value if nobody or another team is awarded |
| 12 | insure | Insurance | question | answering team | +half tile value if not correct |
| 13 | pick | Pickpocket | question | answering team | If correct, also takes up to 200 from the leading rival (never below 0) |
| 14 | sabo | Sabotage | board (team sheet) | any team | Chosen rival's next question timer is halved |
| 15 | room | Ask the Room | question | answering team | Label "Ask the room: 15 sec" (honour rule) |
| 16 | freeze | Time Freeze | question | answering team | Clock does not tick for 20 s |

Double Up and Risk It cannot stack: once a multiplier is active, the other is disabled.

### 7.6 Scoring and turns
- The answering team is correct: `+tile x multiplier` (plus Pickpocket).
- Another team steals: `+tile` (no multiplier). If the answering team had Risk It: `-tile`. If Insurance: `+tile/2`.
- No one: Risk It `-tile`, Insurance `+tile/2`.
- The tile shows the winning team's colour and name, or "Missed".
- Turn passes to the team after the **picker** (not after a Pass It target).
- **Undo:** the last 20 awards are reversible from the board.
- **Manual adjust:** -100 / +100 buttons on each score chip.
- Timer: tap the ring to pause. At 0 it beeps, vibrates and shows "Time is up. Others can steal."

### 7.7 Groups, colours and icons
Groups and colours are defined in `GROUPS`. Categories not listed fall into an automatic "More" group.

| Group | Colour | Category ids |
|---|---|---|
| UAE | #D63B3B | uaehistory, heritage, emiratidialect, emiratifood, uaesymbols, abudhabi, dubai, sharjah, rak, alain, eastcoast, ajmanuaq, uaenature, uaeislands, uaefastfood, uaetv, uaespace, uaesports, ramadan, uaebiz, uaetransport, uaemalls |
| Arab World | #C07A00 | arabworld, saudi, oman, qatar, kuwait, bahrain, egypt, levant, maghreb, arabic, proverbs, islamicgolden, islamichistory, khaleejimusic, arabicmusic, khaleejitv, egyptiancinema |
| Anime | #D2397E | anime, onepiece, jjk, naruto, dragonball, aot, demonslayer, deathnote, hxh, mha, bleach, ghibli, fma, chainsawman, sololeveling, conan, spacetoon, manga |
| Gaming | #13A065 | videogames, gaminghistory, tga, warcraft, pokemon, tcg, minecraft, fortnite, cod, gta, eafc, zelda, mario, fromsoft, lol, esports, playstation, xbox |
| Screen & Music | #5361E8 | movies, oscars, sitcoms, superheroes, music, rap90s |
| Sports & Motors | #0E93B5 | soccer, worldcup, f1, padel, sports, cars |
| Knowledge | #7A4DE0 | general, geography, history, japan, science, math, gcse, food, brands, riddles, emoji |

Ids that don't exist yet are filtered out automatically. **When adding categories, add their ids to `GROUPS` and an icon to `ICON`.**

- `IC` holds about 60 original line icons (24x24 stroke SVG paths).
- `ICON` maps category id to icon key. Missing ids fall back to a star.
- Icons are generic glyphs on purpose: no brand logos, no copyrighted characters.

### 7.8 Themes (skins)
Selected on the home screen, stored in `mj2_skin`, applied as `document.documentElement.dataset.skin`.
- **Daylight (default):** light grey-white, cobalt `#2F5BFF` and tangerine.
- **Midnight:** near-black, bright accents.
- **Gulf:** deep teal with sand gold.
- **Sadu:** Al Sadu weave pattern band, red/ochre/ivory, Reem Kufi font.

All colours are CSS tokens (`--bg --s1 --s2 --s3 --line --ink --muted --pri --acc --t1..t4 --on-team ...`). The top band is a 4-colour stripe, or a zigzag Sadu weave in the Sadu skin.

### 7.9 Question screen
- Header: category badge and points (with multiplier), plus the timer ring.
- Tags row: answering team and active ability labels.
- Body: question text, then the SVG image / emoji / YouTube clip if present, then the hint, then the answer card.
- Answer card: English answer, Arabic answer (RTL) and "Also accepted: ...".
- Footer before reveal: ability buttons per team, Back, and Show answer.
- Footer after reveal: one award button per team (labels show the points each would get) and No one.

### 7.10 16:9 landscape layout
Active at `min-width:880px and min-aspect-ratio:4/3`.
- **Board:** fills exactly one screen as a 3 x 2 grid of category cards; tiles and fonts scale with `vw`.
- **Question:** becomes a 2-column grid (question and media left, controls right).
- A **Full screen** button is on the home and board screens (`toggleFS()`).

### 7.11 Other features
- Sound (WebAudio beeps) can be toggled on the setup screen.
- Screen wake lock is requested when a game starts.
- Confetti plays on the end screen. Rematch goes back to the draft with the same setup.
- `prefers-reduced-motion` is respected.

---

## 8. Category inventory (85 built)

Columns: id, name, questions (200/400/600), Arabic answers, video clips.

**UAE (14 built)**
| id | name | total | ar | video |
|---|---|---|---|---|
| uaehistory | UAE History | 100 | 56 | 0 |
| heritage | UAE Culture & Heritage | 100 | 76 | 0 |
| emiratidialect | Emirati Dialect | 100 | 100 | 0 |
| emiratifood | Emirati Food | 99 (35/35/29) | 90 | 0 |
| uaesymbols | UAE National Symbols | 100 | 70 | 0 |
| abudhabi | Abu Dhabi | 100 | 48 | 0 |
| dubai | Dubai | 100 | 53 | 0 |
| sharjah | Sharjah | 100 | 74 | 0 |
| rak | Ras Al Khaimah | **72 (34/25/13), needs 28 more** | 48 | 0 |
| eastcoast | Fujairah & East Coast | 100 | 62 | 0 |
| ajmanuaq | Ajman & Umm Al Quwain | 100 | 72 | 0 |
| uaenature | UAE Nature & Places | 100 + 18 map pictures | 62 | 0 |
| uaefastfood | UAE Fast Food | 100 | 36 | 0 |
| uaetv | UAE TV & Pop Culture | 100 | 52 | 0 |

`alain` (Al Ain) was planned in the same batch as Sharjah and RAK but **was not created**.

**Arab World (13 built)**
arabworld (100 + 20 map pictures, ar 93), saudi (ar 79), oman (93), bahrain (90), egypt (97), levant (97), maghreb (97), arabic (53), proverbs (100), islamicgolden (81), islamichistory (83), khaleejimusic (95), arabicmusic (89). All have 100 questions.

**Anime (18 built, all 100)**
anime (6 videos), onepiece, jjk (3 videos), naruto, dragonball, aot, demonslayer, deathnote, hxh, mha, bleach, ghibli, fma, chainsawman, sololeveling, conan, spacetoon (13 ar), manga.

**Gaming (18 built, all 100)**
videogames (3 videos), gaminghistory (4), tga (5), warcraft (5), pokemon (4), tcg, minecraft, fortnite, cod, gta, eafc, zelda, mario, fromsoft, lol, esports, playstation, xbox.

**Screen & Music (6 built, all 100)**
movies (10 videos), oscars (5), sitcoms (5), superheroes (4), music (8), rap90s (5).

**Sports & Motors (6 built, all 100)**
soccer (2 videos), worldcup (5), f1 (5), padel, sports (non-soccer), cars (6).

**Knowledge (10 built)**
general, geography (+45 outline pictures), history (World History), japan, science (+30 diagram pictures), math (+30 diagram pictures), gcse (GCSE Mix, +15 diagram pictures), food (ar 24), brands, riddles (Riddles & Logic).

### 8.1 Picture questions (158, all generated, copyright-free)
| File | Target category | Count | Content |
|---|---|---|---|
| geography_outlines.json | geography | 45 | "Name this country from its outline" (Natural Earth public-domain data, d3-geo). Note: Somalia map also fills Somaliland |
| arabworld_maps.json | arabworld | 20 | "Which country is highlighted?" on a MENA map (with ar). Map crops at about 60°E |
| uae_maps.json | uaenature | 18 | Highlighted emirate (7), emirate outline (7), neighbours Oman, Saudi Arabia, Qatar and Musandam (with ar) |
| math_diagrams.json | math | 30 | Angles, areas, circles, Pythagoras, bar chart, coordinates, 3D faces/edges, fractions, number line |
| gcse_diagrams.json | gcse | 15 | Gradient, intercept, polygon angles, series circuit, distance-time, velocity-time, bearings |
| science_diagrams.json | science | 30 | Solar system planet (8), periodic table cell (10), circuit symbols (6), Moon phases (6) |

The owner **removed and does not want a Flags category.** Flag images must not come back. Country outlines are fine.

### 8.2 Video questions
- 85 YouTube ids were gathered by agents from search-result URLs and checked with YouTube oEmbed: 77 ok, 8 bad.
- Bad ids are listed in the `video_check_*.json` files and excluded at build time.
- Clip `start`/`end` windows were guessed by agents and **not checked against the footage**.
- Some ok videos are re-uploads rather than official channels: Inside Out 2 `8rjvKzYeSp0`, Halo Infinite `Gip3MErg2pc`, Oppenheimer `5ZF3FlooDh4`, Moonlight `nRlbYUJdnkM`.

---

## 9. Content quality process

- **Rules file:** every writing agent read `AGENT_RULES.md`. Key rules: 100 per category at 35/35/30; short unambiguous answers; no answer in the question; no time-sensitive facts; verify or drop uncertain facts; no lyrics, poems or dialogue; metric units; no politics or religious rulings; no pork or alcohol; Arabic `ar` and dialect `alt` for Arabic terms.
- **Difficulty rubric:**
  - 200: most casual fans know it.
  - 400: regular fans.
  - 600: specialists.
  - The owner flagged the One Piece shipwright (Franky) at 600 as wrong. It was fixed to 200 and **all original 23 categories were re-tiered** against this rubric.
  - New categories were written with the rubric from the start but have **not** had a separate recalibration pass.
- **Arabic answers:** added to heritage, uaehistory, uaenature, arabworld, abudhabi, dubai, uaefastfood and food, plus all new UAE / Arab categories at creation.
- **Copyright stance:** the owner believes the original Tarbaa questions are public knowledge and wanted them copied. They were not copied because the question list is not accessible (it lives inside the Android app, with no public listing). If the owner provides screenshots of Tarbaa categories, matching original questions can be written. All questions here are original wording.

### 9.1 Spot-check list (agents flagged these as unverified or uncertain)
- **UAE History:** 1996 (permanent constitution), 1968 events; the Qasr Al Hosn date is ambiguous (1761 watchtower vs 1795).
- **Abu Dhabi:** 1758 Umm Shaif find, 1891 Al Jahili Fort, 1968 Al Ain Zoo, 1981 Book Fair, 2010 Sheikh Zayed Bridge, 1761 Qasr Al Hosn.
- **Dubai:** 1901 free port, 1896 Sheikh Saeed house, 1979 World Trade Centre, 1985 Emirates first-aircraft lessor.
- **UAE Culture & Heritage:** dialect and pearling terms Chebab (جباب), Fattam, Habban, Qaitt, Diyyin, Khabt, Jireesh, Mahaibes; dhow types Shuai, Baghlah.
- **UAE Nature:** Jebel Jais about 1,934 m, Jebel Hafeet about 1,249 m, Moreeb about 300 m, Rub al Khali about 650,000 km²; crab plover on Bu Tinah; Madha enclave; 2018 Jebel Ali Ramsar date. Also #80 (Sweihan 51.8 °C record) and #81 (2024 rainfall record) are "record" style, which breaks the rules; consider replacing.
- **UAE Fast Food:** Pickl founder (q76); Church's Texas Chicken "San Antonio" (founded there, HQ now Atlanta, q79); Alshaya runs Shake Shack UAE; Americana runs Hardee's; Talabat brand colour; Samoon bread for Chips Oman; Akkawi cheese.
- **Bahrain / Arab:** Fijiri, Sawt, Garangao, Jallab, Gulf Air 1950, Palestine's 2015 Asian Cup debut.
- **Jujutsu Kaisen:** Hakari's domain name, Kamo/Choso blood technique, Panda Gorilla Mode, Ogi Zenin, Judgeman, Death Painting Wombs count of nine.
- **Hunter x Hunter:** exam number 405, "287th" Hunter Exam, 2011 and 1999 director credits. **Death Note:** English voice cast, Gelus and Sidoh details.
- **Chainsaw Man:** Quanxi "first devil hunter", Reze's Soviet origin, Aki's Curse Devil cost. **Solo Leveling:** Go Gun-hee title, Kamish and Ashborn, "National-level" rank.
- **Dragon Ball / Demon Slayer:** some answers are anime-only or manga-only.
- **Padel:** service line 6.95 m and some Spanish shot names unverified.
- **PlayStation:** too many "In which year" questions; vary them.

---

## 10. Backlog (prioritised)

### P1. Reach the category target (owner request: at least 4x, so 148+)
Run agents with `AGENT_RULES.md` (one agent writes 4 categories, about 10 to 15 minutes; up to 20 agents at once). Then add each new id to `GROUPS` and `ICON` in the template, run `build.py`, republish. These were planned but **not written** (the run was interrupted):

**UAE and Arab:**
- alain | Al Ain: oases, UNESCO sites Hili/Hafit/Bidaa Bint Saud, Jebel Hafeet, Al Jahili Fort, Al Ain Zoo, falaj, Al Ain Club.
- uaespace | UAE Space Program: Hope Probe, Hazza Al Mansoori, Sultan Al Neyadi, MBRSC, Rashid rover, Mars 2117.
- uaesports | UAE Sports: clubs, Ahmed Khalil, Omar Abdulrahman, Ali Mabkhout, Ahmed Al Maktoum 2004 gold, jiu-jitsu, camel racing, UAE Tour.
- ramadan | Ramadan & Eid Traditions: cultural only (iftar, suhoor, Hag Al Leila, mesaharati, Ramadan cannon, eidiya). No religious rulings.
- uaebiz | UAE Companies & Brands: Emirates, Etihad, flydubai, Air Arabia, ADNOC, Emaar, Aldar, DP World, e&, Careem, Noon, Aramex.
- uaetransport | UAE Roads & Transport: E11, E311, Salik, Darb, Dubai Metro, Etihad Rail, airports, plates.
- uaeislands | UAE Islands & Coast: Yas, Saadiyat, Sir Bani Yas, Delma, Palm Jumeirah, The World.
- uaemalls | UAE Malls & Shopping: Dubai Mall, Mall of the Emirates and Ski Dubai, Yas Mall, Global Village, souks.
- qatar | Qatar. kuwait | Kuwait.
- khaleejitv | Khaleeji TV & Drama. egyptiancinema | Egyptian Cinema.
- Fix **rak** (add 28 to reach 35/35/30) and **emiratifood** (add one 600).

**Gaming:**
- nintendo, finalfantasy, assassins (Assassin's Creed), residentevil.
- witcher, overwatch, roblox, mobilegames.
- fighting (Fighting Games), vrgaming (VR & Virtual Reality), retro (Retro & Arcade).
- emoji (Emoji Guess, special format with an `emoji` field; no flag emoji).

**Screen and music:**
- mcu, dc, starwars, harrypotter.
- lotr, disney, pixar, fastfurious.
- bond, nolan, horror, netflix (include Arabic originals like Jinn, AlRawabi School for Girls).
- got (Game of Thrones), breakingbad, simpsons, cartoons (Classic Cartoons).
- bollywood, kdrama, kpop, villains (Movie Villains).
- hiphop00 (2000s Hip Hop), pop00 (2000s Pop), rock, instruments.

**Sports and motors:**
- epl, laliga, ucl (Champions League), realmadrid.
- barca, messironaldo, nba, tennis.
- boxing, ufc, olympics, cricket.
- golf, wwe, jiujitsu, equestrian (Horse & Camel Racing).
- uaefootball, gulffootball, cycling, supercars.
- jdm, carbrands, motorsport (Rally & Dakar).

**Knowledge:**
- capitals, landmarks, space, humanbody, animals.
- dinosaurs, inventions, chemistry, physics.
- Wave 3 ideas: biology, tech, internet, ai, apple, economics, languages, mythology, egyptancient, rome (Ancient Rome & Greece), ww2, art, literature, aviation, oceans, asia, europe, africa, americas, fashion, watches, sneakers, coffee, desserts, worldfood, fitness, kids (Kids Corner), guessyear (Guess the Year), truefalse (True or False), travel.

Suggested group placement:
- The new UAE / Arab ids are already in `GROUPS`.
- Gaming ids go to Gaming; film, TV and music to Screen & Music; sports and cars to Sports & Motors; the rest to Knowledge.
- Consider splitting Knowledge into "Science" and "World" once it grows.

### P2. APK
Do section 6A as soon as the owner provides a GitHub repo name. Re-run the workflow after every content update (push).

### P3. Quality
- **Recalibration pass:** re-tier the 62 categories created after the first pass (rubric in `AGENT_RULES.md`).
- **Spot-check list:** verify section 9.1 with web search; fix or drop.
- **Video clips:** review start/end windows; replace re-upload videos with official channel uploads where possible.
- **Duplicates:** deduplicate overlapping questions across related categories (e.g. soccer vs worldcup, anime vs naruto/jjk, arabic vs islamicgolden "sifr").

### P4. Features requested or proposed
- **Requested by owner, done:** icons, reroll, 16 abilities with draft, Arabic answers with dialect variants, per-category Surprise exclusion, 16:9 full-screen layout, images only where relevant.
- **Proposed, not built:**
  - "My Questions": owner adds custom questions with photos from the phone gallery, compressed and stored on the device. This is the legal route for landmark, car or character photos.
  - Bundled fonts for full offline use in the APK.
  - Online multiplayer (Tarbaa supports remote play).
- **Name:** "Majlis" is in use. The owner suggested it himself. 20 alternatives were offered: Majlis Night, Sadu, Dallah, Gahwa Night, Fareej, Barjeel, Al Sablah, Ayyala, Nokhada, Smart Majlis, Majlis Masters, Falcon Quiz, Qahwa Quiz, The 600 Club, Steal the Board, Square Off, Tile Rush, Board Night, Know or Go.

---

## 11. Owner decisions and feedback log (chronological)

1. Wanted an English remake of Tarbaa for Android, built fast with multiple agents. Started with 12 categories.
2. Chose an installable web app first ("done in 10 minutes"); gave full creative control.
3. Asked for about 100 questions per category, images and YouTube video questions, and Surprise exclusion.
4. Difficulty must match points (One Piece Franky example).
5. Added categories: Warcraft, Sitcoms, 90s Rap, Mathematics, GCSE, JJK, Gaming History, Japan, Oscars, The Game Awards, UAE Fast Food, several UAE / Abu Dhabi categories, UAE Culture & Heritage, Padel. Also "things I'd find interesting".
6. **No Flags category.** "Remove it, I don't want to see it."
7. **Disliked both early colour schemes:** navy + sand gold + coral, then red + ochre + brown "Sadu". Now on 4 selectable themes with a light default. Get feedback before another redesign.
8. Exclusion must be **per single category**, not by group type.
9. Wants **at least 4x the categories** (148+).
10. Wants **Arabic answers plus local dialect variants** for UAE / Arab terms.
11. **Images only where relevant**, not on every question.
12. Wants category icons, a Surprise reroll, and an ability draft (3 per team from 16).
13. Wants it **downloadable as an APK**; asked for the fastest way; then asked for the artifact.
14. Wants **16:9 full screen** for a Samsung Galaxy Z TriFold.
15. Asked for this handover to continue on another Claude account.

**Owner working style**
- Wants speed and parallel agents ("multi agent to expedite").
- Sends short follow-up messages mid-task and often interrupts; deliver a working link early, then iterate.

**Writing preferences:**
- Concise, structured, high-signal; plain language; no em dashes; metric units; neutral tone.
- Cite sources with links when giving factual research.

---

## 12. Environment notes from the old workspace

- **Blocked:** shell access to dl.google.com, maven.google.com, Maven Central, Wikimedia (commons and upload). This is why there are no photo questions and no compiled APK.
- **Allowed:** npm, pip, GitHub (raw.githubusercontent.com, api.github.com). Natural Earth map data came from GitHub; npm packages world-atlas, d3-geo, topojson-client and i18n-iso-countries were used for maps.
- **YouTube:** watch pages returned HTTP 429. The oEmbed endpoint (`https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=<id>&format=json`) via WebFetch worked for verification.
- **Agents:** at most 20 subagents can run at once. Give each agent its own scratch directory; agents writing to a shared scratch folder overwrote each other once.

---

## 13. Prompt to paste into the new Claude account

> I am continuing an English team trivia party game called **Majlis** (inspired by the Arabic app Tarbaa). I've attached **Majlis-Handover.zip**. Read `squareup/HANDOVER.md` fully first, then unzip, run `python3 squareup/build.py`, and publish `squareup/index.html` as an artifact with `manifest.json`, `icon-192.png`, `icon-512.png`. Then continue the backlog in section 10, starting with P1 (new categories using `AGENT_RULES.md`, multi-agent, up to 20 at once, 4 categories per agent), registering each new id in `GROUPS` and `ICON`, rebuilding and republishing after each batch. Keep my decisions in section 11 (no flags category, per-category exclusion only, Arabic plus dialect answers, images only where relevant). Writing style: concise, structured, no em dashes.
