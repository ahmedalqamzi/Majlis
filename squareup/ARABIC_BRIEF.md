# Arabic display brief (Majlis trivia)

Majlis is an English trivia party game played in the UAE. The owner wants every Arabic word that appears in English letters
to be shown as "English word - Arabic word". The app does this from two fields:
- "ar" (on the question): the Arabic for the ANSWER. Shown as "<answer> - <ar>".
- "qar" (on the question): list of [term, arabic] pairs for Arabic words written in English letters inside the QUESTION text.
  The app shows the first occurrence of each term as "term - arabic". The term must match the question text exactly (case included).

Your job, per category:
1. Run: python3 /home/claude/majlis/squareup/tools/dump_ar.py <id>   (index | question | answer | ar | qar)
2. Answers: if the answer is an Arabic/Emirati/Gulf word, name, place, dish, tradition, person, title, show, song or term
   written in English letters and has no "ar", add it. Check existing "ar" values: fix wrong spelling; remove (null) an "ar" that is
   just a translation of a plain English answer (e.g. answer "Camel" -> no ar; "Dubai" -> ar "دبي" is fine since it is an Arabic name;
   numbers, years, English common nouns -> no ar). Use the standard Arabic spelling used in the UAE/Gulf.
3. Questions: for each Arabic word/name written in English letters inside the question (e.g. "Al Ayyala", "majlis", "Sheikh Zayed",
   "luqaimat", "Burj Khalifa"), add a qar pair. Skip widely-used English words of Arabic origin that are normal English
   (e.g. "coffee", "algebra", "sugar") and skip non-Arabic names. Do not add qar for a term that is the answer.
   At most 3 terms per question; prefer the most important.
4. Write a map in YOUR scratch dir: {"<index>": {"ar": "...", "qar": [["term", "عربي"]]}} (only questions you change), then
   python3 /home/claude/majlis/squareup/tools/apply_ar.py <id> <map.json>   and fix any PROBLEMS it prints.
Only be as certain as a native speaker would be; if unsure of a spelling, skip it. Do not change question text, answers or other fields.
Do not edit any other files. Final reply: one line per category: id, ar count, qar count, and anything notable.
