# Picture tagging brief (Majlis trivia)

Majlis is an English team trivia party game played mostly in the UAE. The app will load pictures at runtime from
English Wikipedia: for a question tagged with an article title, it fetches that article's lead image
(https://en.wikipedia.org/api/rest_v1/page/summary/<title>) and shows it. You choose the article titles.

Two optional fields per question:
- "apic": shown with the ANSWER when it is revealed. The English Wikipedia article whose lead image shows the answer itself:
  a person, place, building, landmark, city, animal, plant, dish, object, vehicle, character, film, show, game, album, team, club, brand, event.
- "pic": shown with the QUESTION. The article whose lead image shows the question's subject. ONLY when the picture
  does not reveal or strongly hint the answer. Example: "In which city is the Burj Khalifa?" -> pic "Burj Khalifa" (fine).
  "Which tower in Dubai is the tallest in the world?" -> NO pic (picture shows the answer); use apic "Burj Khalifa".
  "Who directed Inception?" -> pic "Inception" (the poster shows the title, which the question already names) and apic "Christopher Nolan".
  Never a pic whose lead image is likely to contain the answer as text (posters, covers, logos, signs) or show the answer person.

Rules
- Only titles of real English Wikipedia articles you are confident exist and are specifically about that subject
  (exact spelling and capitalisation, disambiguated form when needed, e.g. "Mercury (planet)", "Inception", "Pikachu").
  Character articles are fine when they exist; if a character has no own article, skip it (do not fall back to the series).
- Skip when the answer is a number, year, date, word, phrase, colour, nickname, abstract idea, generic term, True/False,
  or anything without a clear picture; skip if unsure of the title. Quality over coverage: wrong pictures are worse than none.
  Typical coverage is 20-60% of a category; text-heavy categories may get very few.
- Never tag prophets, companions or other religious figures, or portraits of historic Islamic figures; mosques and places are fine.
  No gore, nudity, or disturbing images (e.g. horror villains' gory stills, crime victims, war atrocities). Skip living private individuals.
- Do not change any question text, answers or other fields.

How
1. For each of your categories run: python3 /home/claude/majlis/squareup/tools/dump.py <id>   (index | points | question | answer)
2. Write a map file in YOUR scratch dir (given in your prompt): {"<index>": {"apic": "...", "pic": "..."}, ...}
3. Apply: python3 /home/claude/majlis/squareup/tools/apply_pics.py <id> <map.json>   (fix any PROBLEMS it prints)
You may verify a doubtful title with WebFetch https://en.wikipedia.org/wiki/<Title> (works); do not spend long on it.
Do not edit any other file. Work fast. Final reply: one line per category: id, questions, pic count, apic count.
