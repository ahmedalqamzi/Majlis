# Top-up brief: grow existing Majlis categories toward about 300 questions

You are extending existing trivia question banks for Majlis, an English team trivia party game played mostly in the UAE.

1. Read /home/claude/majlis/squareup/AGENT_RULES.md and follow its quality, tiering and content rules (the output path there is outdated; use the paths below).
2. For each category id you are given, the file is /home/claude/majlis/squareup/q/<id>.json and currently has about 100 questions. Read ALL existing questions first.
3. APPEND new questions so each file grows toward about 300 total, keeping roughly the 35/35/30 tier ratio overall (about 105 at 200, 105 at 400, 90 at 600). The game's owner said: about 300, but quality beats quantity. Stop short rather than pad with weak, near-duplicate, time-sensitive or unverified questions.
4. Never change or delete existing questions. Never change the file's "id" or "name". Keep any existing fields (yt/start/end/svg/ar/alt) untouched.
5. New questions must not repeat or closely paraphrase existing ones in the same file, and should avoid repeating questions in obviously related files (read them if relevant, e.g. dubai vs abudhabi, anime vs naruto, soccer vs worldcup/epl). Avoid using the same answer more than about 3 times per file.
6. Add "ar" (Arabic script) whenever the answer is an Arabic, Emirati or Gulf word, name, place, dish or term, and "alt" for real alternative spellings or dialect forms (0 to 4).
7. No video ("yt") fields in new questions.
8. Verify facts you are not certain of with WebSearch/WebFetch or drop them. Prefer dropping.
9. Write with a Python script: json.dump(obj, f, ensure_ascii=False, indent=1). Reload and check: every question has q/a/p, p in {200,400,600}, no duplicate question text, original questions all still present unchanged.
10. Use your own scratch directory /tmp/claude-0/<your agent name>/ for scripts. Do not edit any other file in q/, do not touch /mnt/project-files, do not install packages, do not run git, and never call any mcp__hearthbot__ tool.

Final reply: one line per category with id, old total, new total and counts per tier, plus a short list of facts you kept but are less sure of.
