# Majlis trivia: rules for question-writing agents

You write question banks for an English team trivia party game played mostly in the UAE.

## Output
One JSON file per category at /home/claude/squareup/q/<id>.json, written with a Python script using
json.dump(obj, f, ensure_ascii=False, indent=1), then reloaded to verify counts and uniqueness.
Use your own scratch directory /tmp/claude-0/<your first category id>/ and never shared folders.

{"id":"<id>","name":"<Name>","questions":[{"q":"Question?","a":"Short answer","p":200}, ...]}

Optional fields per question:
- "ar": the answer in Arabic script. REQUIRED whenever the answer is an Arabic, Emirati or Gulf word, name, place, dish, tradition, person or term (e.g. a: "Khoos", ar: "الخوص"). Use correct Arabic spelling.
- "alt": list of other accepted answers: common English spellings and local dialect variants or names (e.g. ["Khoos","Khous","Sa'af weaving"]). Only include variants you are confident are really used; Emirati/Gulf dialect forms are welcome when real. Keep 0 to 4 items.
- "yt","start","end": only if the category brief asks for videos; ids MUST appear in real WebSearch result URLs; never invent ids. Skip videos if you cannot find real ids quickly.

## Counts and difficulty
- Exactly 100 per category: 35 at p=200, 35 at p=400, 30 at p=600.
- Points must match difficulty, judged by a casual party crowd:
  - 200: most people or any casual fan know it (main characters, the most famous facts).
  - 400: regular fans; needs real interest.
  - 600: deep fans or specialists; obscure but fair and verifiable.
  Tier honestly. Known bad example: "Who is the Straw Hats' shipwright?" (Franky) is 200, not 600.

## Quality
- Open-ended questions with one short unambiguous answer (1 to 6 words). No multiple choice (except a "True or False" category where answers are "True" or "False").
- Never put the answer, or an obvious giveaway, in the question.
- No duplicates or near-duplicates. Vary question styles.
- Only facts you are certain of. No time-sensitive facts: no "current" holders, champions, rankings, records that can be broken, prices, follower counts, "latest". Settled history up to 2025 is fine.
- If not fully sure, verify with WebSearch or drop it. Prefer dropping.
- Never quote song lyrics, poems, or film/TV dialogue. Titles are fine.
- Metric units only. Neutral, respectful tone. No politics, no contested borders, no religious rulings, no offensive content. No pork or alcohol questions.
- Compute any math in Python before saving.

## Finish
Work fast. Reply with one line per category: id and counts per tier.
