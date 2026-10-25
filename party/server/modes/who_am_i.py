"""Who Am I: pure initialize / reduce / project.

Every player is assigned one character per round. A player's projection
contains every other player's assignment and never their own until that
player is revealed (host reveal, host award, or game end). The display sees
only revealed assignments; the operator sees everything as a nonparticipant.

Guess text is private: the operator sees every pending guess (``guesses``),
each player sees only its own (``you.guess``), and everyone else sees only
which players have a pending guess (``pendingGuesses``). This holds after
reveals and at game end too.

Assignments are drawn from a deck shuffled with a server-supplied seed, so
item order in the pack does not predict who holds what. Other players'
assignments are keyed by player id in seating (join) order, so the shape of a
projection never depends on which character the viewer holds.
"""

import copy
import random

from ..content import MAX_ID, is_int, is_text
from ..textutil import clean_text, match_key

MODE_ID = "who_am_i"
TITLE = "Who Am I"
ACTION_PREFIX = "whoami."
MIN_PLAYERS = 2
DEFAULT_POINTS = 1
MAX_AWARD_POINTS = 100
MAX_GUESS_LEN = 80


def validate_item(item, path, issues):
    """Item ``{id,label,ar?,difficulty}``; difficulty is a short string or int."""
    if not isinstance(item, dict):
        issues.append(path + ": must be an object")
        return None
    before = len(issues)
    if not is_text(item.get("id"), MAX_ID):
        issues.append(path + ".id: required non-empty string (max 64)")
    if not is_text(item.get("label"), 120):
        issues.append(path + ".label: required non-empty string (max 120)")
    ar = item.get("ar")
    if ar is not None and not is_text(ar, 120):
        issues.append(path + ".ar: must be a non-empty string when present")
    difficulty = item.get("difficulty")
    if not (is_text(difficulty, 32) or is_int(difficulty, 0, 100)):
        issues.append(path + ".difficulty: required short string or integer 0-100")
    if len(issues) != before:
        return None
    out = {"id": item["id"], "label": item["label"], "difficulty": difficulty}
    if ar is not None:
        out["ar"] = ar
    return out


def validate_items(items, issues):
    """Cross-item check: no two items may share a display text.

    Every ``label`` and ``ar`` is canonicalised with ``match_key`` (NFKC,
    casefold, whitespace collapsed and trimmed). A canonical text may belong to
    only one item, in either language field. Otherwise another player's
    assignment could be served as the exact string of the viewer's own secret.
    ``label`` equal to ``ar`` within one item is allowed. Issues name paths
    only, never the colliding text.
    """
    owner = {}
    for index, item in enumerate(items):
        keys = [("label", match_key(item["label"]))]
        if "ar" in item:
            keys.append(("ar", match_key(item["ar"])))
        for field, key in keys:
            first = owner.get(key)
            if first is not None and first != index:
                issues.append("items[%d].%s: collides with display text of items[%d] "
                              "(case/width-insensitive)" % (index, field, first))
            elif first is None:
                owner[key] = index


def _round_id(game_no, round_no):
    return "g%dr%d" % (game_no, round_no)


def _start_round(state, round_no):
    order = state["order"]
    drawn = state["deck"][: len(order)]
    state["deck"] = state["deck"][len(order):]
    state["assignments"] = dict(zip(order, drawn))
    state["roundNo"] = round_no
    state["roundId"] = _round_id(state["gameNo"], round_no)
    state["roundPhase"] = "asking"
    state["revealed"] = []
    state["ready"] = []
    state["guesses"] = {}
    state["solved"] = {}


def initialize(config, players, content):
    """Returns ``(state, None)`` or ``(None, error_code)``."""
    order = [p["id"] for p in players]
    if len(order) < MIN_PLAYERS:
        return None, "not_enough_players"
    items = copy.deepcopy(content["items"])
    if len(items) < len(order):
        return None, "content_insufficient"
    random.Random(config["seed"]).shuffle(items)
    state = {
        "mode": MODE_ID,
        "title": content["title"],
        "gameNo": config["gameNo"],
        "order": order,
        "deck": items,
        "awards": [],
        "scores": dict((p["id"], p["score"]) for p in players),
        "points": config.get("whoAmIPoints", DEFAULT_POINTS),
        "ended": False,
    }
    _start_round(state, 1)
    return state, None


def _in_order(state, pid):
    return isinstance(pid, str) and pid in state["order"]


def _ordered(state, pids):
    wanted = set(pids)
    return [p for p in state["order"] if p in wanted]


def reduce(state, actor, action, server_sequence):
    """Pure transition. Returns ``(new_state, None)`` or ``(state, error_code)``.

    The input state is never mutated; accepted transitions return a copy.
    """
    kind = action.get("type")
    payload = action.get("payload") or {}
    role = actor.get("role")
    pid = actor.get("playerId")
    round_id = action.get("roundId")
    if round_id is not None and round_id != state["roundId"]:
        return state, "stale_round"
    if not isinstance(payload, dict):
        return state, "invalid_payload"

    if kind == "game.end":
        if role != "operator":
            return state, "forbidden"
        if state["ended"]:
            return state, "invalid_phase"
        s = copy.deepcopy(state)
        s["ended"] = True
        s["revealed"] = list(s["order"])
        s["roundPhase"] = "revealed"
        return s, None

    if state["ended"]:
        return state, "invalid_phase"

    if kind == "whoami.ready":
        if role != "player" or not _in_order(state, pid):
            return state, "forbidden"
        ready = payload.get("ready", True)
        if not isinstance(ready, bool):
            return state, "invalid_payload"
        if state["roundPhase"] != "asking":
            return state, "invalid_phase"
        s = copy.deepcopy(state)
        current = set(s["ready"])
        if ready:
            current.add(pid)
        else:
            current.discard(pid)
        s["ready"] = _ordered(s, current)
        return s, None

    if kind == "whoami.guess":
        if role != "player" or not _in_order(state, pid):
            return state, "forbidden"
        text = clean_text(payload.get("text"), MAX_GUESS_LEN)
        if text is None:
            return state, "invalid_payload"
        if pid in state["revealed"]:
            return state, "already_revealed"
        s = copy.deepcopy(state)
        s["guesses"][pid] = {"text": text, "seq": server_sequence}
        return s, None

    if kind == "whoami.dismiss":
        # Clears one pending guess (judged wrong). Scores, assignments,
        # reveals and awards are untouched; the player may guess again.
        if role != "operator":
            return state, "forbidden"
        target = payload.get("playerId")
        if not _in_order(state, target):
            return state, "unknown_player"
        if target not in state["guesses"]:
            return state, "no_pending_guess"
        s = copy.deepcopy(state)
        del s["guesses"][target]
        return s, None

    if kind == "whoami.reveal":
        if role != "operator":
            return state, "forbidden"
        target = payload.get("playerId")
        s = copy.deepcopy(state)
        if target is None:
            if len(s["revealed"]) == len(s["order"]):
                return state, "already_revealed"
            s["revealed"] = list(s["order"])
        else:
            if not _in_order(s, target):
                return state, "unknown_player"
            if target in s["revealed"]:
                return state, "already_revealed"
            s["revealed"] = _ordered(s, s["revealed"] + [target])
        if len(s["revealed"]) == len(s["order"]):
            s["roundPhase"] = "revealed"
        return s, None

    if kind == "whoami.award":
        if role != "operator":
            return state, "forbidden"
        target = payload.get("playerId")
        if not _in_order(state, target):
            return state, "unknown_player"
        points = payload.get("points", state["points"])
        if not is_int(points, 0, MAX_AWARD_POINTS):
            return state, "invalid_payload"
        # Dedup key is round + player, independent of actionId.
        key = "%s:%s" % (state["roundId"], target)
        if key in state["awards"]:
            return state, "already_awarded"
        s = copy.deepcopy(state)
        s["awards"].append(key)
        s["scores"][target] = s["scores"].get(target, 0) + points
        s["solved"][target] = {"points": points, "seq": server_sequence}
        if target not in s["revealed"]:
            s["revealed"] = _ordered(s, s["revealed"] + [target])
        if len(s["revealed"]) == len(s["order"]):
            s["roundPhase"] = "revealed"
        return s, None

    if kind == "round.next":
        if role != "operator":
            return state, "forbidden"
        if len(state["deck"]) < len(state["order"]):
            return state, "content_exhausted"
        s = copy.deepcopy(state)
        _start_round(s, s["roundNo"] + 1)
        return s, None

    return state, "unknown_action"


def _public_item(item):
    out = {"label": item["label"]}
    if "ar" in item:
        out["ar"] = item["ar"]
    return out


def project(state, viewer):
    """Role projection. ``viewer`` is ``{role, playerId?}``. Pure."""
    role = viewer.get("role")
    me = viewer.get("playerId") if role == "player" else None
    if me is not None and me not in state["order"]:
        role, me = "display", None
    order = state["order"]
    revealed = set(order) if state["ended"] else set(state["revealed"])
    assignments = state["assignments"]
    view = {
        "title": state["title"],
        "round": state["roundNo"],
        "roundPhase": "ended" if state["ended"] else state["roundPhase"],
        "order": list(order),
        "ready": list(state["ready"]),
        "revealed": [p for p in order if p in revealed],
        "solved": [p for p in order if p in state["solved"]],
        "pendingGuesses": [p for p in order if p in state["guesses"]],
    }
    if role == "operator":
        view["guesses"] = dict((p, state["guesses"][p]["text"]) for p in order if p in state["guesses"])
        view["assignments"] = dict((p, copy.deepcopy(assignments[p])) for p in order)
        view["roundsRemaining"] = len(state["deck"]) // max(1, len(order))
        view["awardPoints"] = state["points"]
    elif role == "player":
        view["assignments"] = dict((p, _public_item(assignments[p])) for p in order if p != me)
        you = {
            "revealed": me in revealed,
            "ready": me in state["ready"],
            "solved": me in state["solved"],
            "guess": state["guesses"][me]["text"] if me in state["guesses"] else None,
        }
        if me in revealed:
            you["assignment"] = _public_item(assignments[me])
        view["you"] = you
    else:
        view["assignments"] = dict((p, _public_item(assignments[p])) for p in order if p in revealed)
    return view
