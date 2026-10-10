# Soundtrack category brief (Majlis trivia)

Majlis is an English team trivia party game played mostly in the UAE. You build ONE "guess the soundtrack" category.
In the app, a question with "audio": true plays a YouTube clip from "start" to "end" seconds with the video HIDDEN
(players only hear it); the video is shown after the answer is revealed.

Output: /home/claude/majlis/squareup/q/<id>.json written with Python json.dump(obj,f,ensure_ascii=False,indent=1):
{"id":"<id>","name":"<Name>","questions":[{"q":"Which anime is this opening from?","a":"Attack on Titan","p":200,
  "alt":["Shingeki no Kyojin"],"yt":"<11-char id>","start":0,"end":25,"audio":true}, ...]}
And /home/claude/majlis/squareup/video_check_<id>.json: {"<yt id>": {"status":"ok","title":"<oembed title>","channel":"<oembed author_name>"}, ...}
The build DROPS any question whose yt id is not in a video_check file with status "ok".

Finding and verifying clips (mandatory for every clip)
- Find candidate videos with WebSearch (e.g. "<anime> opening creditless official youtube"). Take ids only from real result URLs; never invent ids.
- Verify EACH id with WebFetch https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=<id>&format=json
  It must return a title/author that clearly matches the piece. Errors (401 = embedding disabled, 404 = gone) mean drop it.
- Strongly prefer official uploads that stay up and allow embedding: publisher/studio/label channels (e.g. Crunchyroll, Aniplex USA,
  TOHO animation, Netflix, Nintendo, PlayStation, Xbox, Square Enix Music, the composer or artist's official channel, "- Topic" channels, VEVO).
- Clip: 15-30 s. Choose start at the most recognisable part (main theme / chorus / hook), skipping silent intros and
  logos. Avoid start points where lyrics sing the answer's name, except at 200 points. Make sure start < song length.

Questions
- Up to 100 questions (aim 60-100, quality over quantity); about 35% p=200, 35% p=400, 30% p=600.
  200: iconic themes almost everyone knows; 400: regular fans; 600: deep cuts but still fair.
- Vary wording: "Which anime is this opening from?", "Name the game this music comes from.", "Which film is this theme from?",
  "This ending theme belongs to which anime?". Answer = the work's title (short, common English title), with "alt" for
  other accepted names (0-4). One question per distinct piece; no duplicates; at most 3 pieces from the same work.
- Family-friendly only; no explicit lyrics.
Reload your files to check counts, uniqueness and that every yt id is "ok" in your check file.
Final reply: one line: id, count per tier, number of clips verified/dropped.
