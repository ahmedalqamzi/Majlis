"""Impostor: pure initialize / reduce / project.

Each round the server draws one unseen authored word and secretly picks
exactly one impostor. Every crew player privately receives ``{role: crew,
word}``; the impostor privately receives ``{role: impostor}`` and nothing
about the item. Nobody else's role is ever projected to a player, and the
operator and display receive no role assignment and no item data until the
round is revealed (see ``IMPOSTOR.md``).

Round phases: ``discussion`` -> ``vote`` -> (``guess_word`` ->) ``reveal``.
Discussion is spoken in the room; the server only records votes, the single
impostor word guess and the server-side result. Scores are awarded exactly
once per round, keyed by roundId, independent of actionId.

Word matching is explicit: NFC, ``str.casefold`` and whitespace collapse /
trim only. There is no width folding, diacritic stripping, substring, alias
or fuzzy matching.
"""

import copy
import random
import unicodedata

from ..content import MAX_ID, is_text
from ..textutil import clean_text

MODE_ID = "impostor"
TITLE = "Impostor"
ACTION_PREFIX = "impostor."
MIN_PLAYERS = 3
MAX_PLAYERS = 12
MAX_WORD_LEN = 80
MAX_CATEGORY_LEN = 64
MAX_GUESS_LEN = 80
IMPOSTOR_POINTS = 3
CREW_POINTS = 2

DISCUSSION = "discussion"
VOTE = "vote"
GUESS_WORD = "guess_word"
REVEAL = "reveal"

# Round outcomes (public only once the round is resolved or ended).
IMPOSTOR_SURVIVED = "impostor_survived"
IMPOSTOR_GUESSED_WORD = "impostor_guessed_word"
CREW_WRONG_GUESS = "crew_wrong_guess"
CREW_FORFEIT = "crew_forfeit"
ABORTED = "aborted"


def word_key(value):
    """Exact comparison key: NFC, casefold, whitespace collapsed and trimmed."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def validate_item(item, path, issues):
    """Item ``{id, word, category}``. Unknown keys are dropped."""
    if not isinstance(item, dict):
        issues.append(path + ": must be an object")
        return None
    before = len(issues)
    if not is_text(item.get("id"), MAX_ID):
        issues.append(path + ".id: required non-empty string (max 64)")
    word = item.get("word")
    if not is_text(word, MAX_WORD_LEN) or clean_text(word, MAX_WORD_LEN) is None:
        issues.append(path + ".word: required non-empty printable string (max 80)")
    if not is_text(item.get("category"), MAX_CATEGORY_LEN):
        issues.append(path + ".category: required non-empty string (max 64)")
    if len(issues) != before:
        return None
    return {"id": item["id"], "word": item["word"], "category": item["category"]}


def validate_items(items, issues):
    """Cross-item check: every word must have a unique ``word_key``.

    Two items whose words differ only by case or whitespace would let the
    same word be dealt twice in one game, so such a pack is invalid. Issues
    name paths only, never the word.
    """
    owner = {}
    for index, item in enumerate(items):
        key = word_key(item["word"])
        first = owner.get(key)
        if first is not None:
            issues.append("items[%d].word: duplicates normalized word of items[%d] "
                          "(case/whitespace-insensitive)" % (index, first))
        else:
            owner[key] = index


def _round_id(game_no, round_no):
    return "g%dr%d" % (game_no, round_no)


def _pick_impostor(state, round_no):
    rng = random.Random("impostor:%d:%d:%d" % (state["seed"], state["gameNo"], round_no))
    return state["order"][rng.randrange(len(state["order"]))]


def _draw(state):
    """Pop the next item whose word key was not dealt in this game, or None."""
    used = set(state["usedKeys"])
    while state["deck"]:
        item = state["deck"].pop(0)
        key = word_key(item["word"])
        if key not in used:
            state["usedKeys"].append(key)
            return item
    return None


def _has_unseen(state):
    used = set(state["usedKeys"])
    return any(word_key(item["word"]) not in used for item in state["deck"])


def _start_round(state, round_no, item):
    state["item"] = item
    state["roundNo"] = round_no
    state["roundId"] = _round_id(state["gameNo"], round_no)
    state["impostor"] = _pick_impostor(state, round_no)
    state["roundPhase"] = DISCUSSION
    state["votes"] = {}
    state["nominee"] = None
    state["outcome"] = None
    state["roundPoints"] = None


def initialize(config, players, content):
    """Returns ``(state, None)`` or ``(None, error_code)``."""
    order = [p["id"] for p in players]
    if len(order) < MIN_PLAYERS:
        return None, "not_enough_players"
    if len(order) > MAX_PLAYERS:
        return None, "too_many_players"
    items = copy.deepcopy(content["items"])
    if not items:
        return None, "content_insufficient"
    random.Random(config["seed"]).shuffle(items)
    state = {
        "mode": MODE_ID,
        "title": content["title"],
        "gameNo": config["gameNo"],
        "seed": config["seed"],
        "order": order,
        "deck": items,
        "usedKeys": [],
        "awards": [],
        "scores": dict((p["id"], p["score"]) for p in players),
        "ended": False,
    }
    item = _draw(state)
    if item is None:
        return None, "content_insufficient"
    _start_round(state, 1, item)
    return state, None


def _tally(state):
    counts = dict((p, 0) for p in state["order"])
    for target in state["votes"].values():
        counts[target] += 1
    return counts


def _nominee(state):
    """The unique strictly highest nonzero tally, else None (tie or no votes)."""
    counts = _tally(state)
    best = max(counts.values())
    if best == 0:
        return None
    leaders = [p for p in state["order"] if counts[p] == best]
    return leaders[0] if len(leaders) == 1 else None


def _award(s, outcome, impostor_wins):
    """Resolve the round exactly once and enter reveal. ``s`` is a copy."""
    key = s["roundId"]
    if key in s["awards"]:
        return False
    s["awards"].append(key)
    points = {}
    for p in s["order"]:
        if impostor_wins:
            points[p] = IMPOSTOR_POINTS if p == s["impostor"] else 0
        else:
            points[p] = 0 if p == s["impostor"] else CREW_POINTS
        s["scores"][p] = s["scores"].get(p, 0) + points[p]
    s["roundPoints"] = points
    s["outcome"] = outcome
    s["roundPhase"] = REVEAL
    return True


def _resolve_vote(s):
    """Close voting on the copy ``s``: guess_word if caught, else reveal."""
    s["nominee"] = _nominee(s)
    if s["nominee"] is not None and s["nominee"] == s["impostor"]:
        s["roundPhase"] = GUESS_WORD
        return True
    return _award(s, IMPOSTOR_SURVIVED, True)


def reduce(state, actor, action, server_sequence):
    """Pure transition. Returns ``(new_state, None)`` or ``(state, error_code)``.

    The input state is never mutated; accepted transitions return a copy.
    Check order never depends on the actor's secret role before the vote is
    resolved, so an error code cannot reveal who the impostor is.
    """
    kind = action.get("type")
    payload = action.get("payload")
    if payload is None:
        payload = {}
    role = actor.get("role")
    pid = actor.get("playerId")
    round_id = action.get("roundId")
    if round_id is not None and round_id != state["roundId"]:
        return state, "stale_round"
    if not isinstance(payload, dict):
        return state, "invalid_payload"

    if kind == "game.end":
        # Ends the game. An unresolved round is aborted: no winner, no points.
        if role != "operator":
            return state, "forbidden"
        if state["ended"]:
            return state, "invalid_phase"
        s = copy.deepcopy(state)
        s["ended"] = True
        if s["outcome"] is None:
            s["outcome"] = ABORTED
        return s, None

    if state["ended"]:
        return state, "invalid_phase"

    phase = state["roundPhase"]

    if kind == "impostor.advance":
        if role != "operator":
            return state, "forbidden"
        s = copy.deepcopy(state)
        if phase == DISCUSSION:
            s["roundPhase"] = VOTE
            return s, None
        if phase == VOTE:
            _resolve_vote(s)
            return s, None
        if phase == GUESS_WORD:
            if not _award(s, CREW_FORFEIT, False):
                return state, "already_awarded"
            return s, None
        return state, "invalid_phase"

    if kind == "impostor.vote":
        if role != "player" or pid not in state["order"]:
            return state, "forbidden"
        if phase != VOTE:
            return state, "invalid_phase"
        target = payload.get("playerId")
        if not isinstance(target, str) or target not in state["order"]:
            return state, "unknown_player"
        if target == pid:
            return state, "self_vote"
        if pid in state["votes"]:
            return state, "already_voted"
        s = copy.deepcopy(state)
        s["votes"][pid] = target
        if len(s["votes"]) == len(s["order"]):
            _resolve_vote(s)
        return s, None

    if kind == "impostor.guess_word":
        if role != "player" or pid not in state["order"]:
            return state, "forbidden"
        if phase != GUESS_WORD:
            return state, "invalid_phase"
        # In guess_word the caught nominee is public, so this check reveals
        # nothing that the projection does not already show.
        if pid != state["impostor"]:
            return state, "forbidden"
        text = clean_text(payload.get("text"), MAX_GUESS_LEN)
        if text is None:
            return state, "invalid_payload"
        s = copy.deepcopy(state)
        if word_key(text) == word_key(s["item"]["word"]):
            awarded = _award(s, IMPOSTOR_GUESSED_WORD, True)
        else:
            awarded = _award(s, CREW_WRONG_GUESS, False)
        if not awarded:
            return state, "already_awarded"
        return s, None

    if kind == "round.next":
        if role != "operator":
            return state, "forbidden"
        if phase != REVEAL:
            return state, "invalid_phase"
        if not _has_unseen(state):
            return state, "content_exhausted"
        s = copy.deepcopy(state)
        item = _draw(s)
        _start_round(s, s["roundNo"] + 1, item)
        return s, None

    return state, "unknown_action"


def project(state, viewer):
    """Role projection. ``viewer`` is ``{role, playerId?}``. Pure.

    Before reveal nobody but crew players receives the word, and each crew
    player receives only its own ``{role, word}``. Item id and category are
    never projected. The role map is never projected; after reveal (or game
    end) only the impostor's id and the word are added for every view.
    """
    role = viewer.get("role")
    me = viewer.get("playerId") if role == "player" else None
    if me is not None and me not in state["order"]:
        role, me = "display", None
    order = state["order"]
    revealed = state["ended"] or state["roundPhase"] == REVEAL
    counts = _tally(state)
    view = {
        "title": state["title"],
        "round": state["roundNo"],
        "roundPhase": "ended" if state["ended"] else state["roundPhase"],
        "order": list(order),
        "votes": dict((p, state["votes"][p]) for p in order if p in state["votes"]),
        "tally": dict((p, counts[p]) for p in order),
        "votesNeeded": len(order),
        "nominee": state["nominee"],
        "outcome": state["outcome"],
        "roundPoints": dict(state["roundPoints"]) if state["roundPoints"] is not None else None,
        "reveal": ({"word": state["item"]["word"], "impostor": state["impostor"]}
                   if revealed else None),
    }
    if role == "operator":
        view["roundsRemaining"] = sum(
            1 for item in state["deck"] if word_key(item["word"]) not in set(state["usedKeys"]))
    elif role == "player":
        if me == state["impostor"]:
            view["you"] = {"role": "impostor"}
        else:
            view["you"] = {"role": "crew", "word": state["item"]["word"]}
    return view
