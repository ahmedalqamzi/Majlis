"""Feud with HOUSE ANSWERS: pure initialize / reduce / project.

Boards are house-authored (``label: "House answers"``); no survey or
respondent counts are claimed. The operator sees the full board including
aliases. Players and display see the question, slot count, strikes, scores,
buzzer state and only answers that were individually revealed (text and
points, never aliases).

Buzzer winner is the first ``feud.buzz`` the room accepts while the buzzer is
open. The room applies actions one at a time under its lock with a monotonic
receipt sequence, so exactly one buzz can win. Awards are deduplicated by
round + board, independent of actionId.
"""

import copy

from ..content import MAX_ID, is_int, is_text
from ..textutil import clean_text, match_key

MODE_ID = "feud"
TITLE = "Feud (House Answers)"
ACTION_PREFIX = "feud."
MIN_PLAYERS = 1
HOUSE_LABEL = "House answers"
MAX_STRIKES = 3
MAX_ANSWERS = 20
MAX_ALIASES = 50
MAX_POINTS = 1000
MAX_MATCH_TEXT = 120


def validate_item(item, path, issues):
    """Board ``{id,question,label:"House answers",answers:[{id,text,aliases,points}]}``."""
    if not isinstance(item, dict):
        issues.append(path + ": must be an object")
        return None
    before = len(issues)
    if not is_text(item.get("id"), MAX_ID):
        issues.append(path + ".id: required non-empty string (max 64)")
    if not is_text(item.get("question")):
        issues.append(path + ".question: required non-empty string")
    if item.get("label") != HOUSE_LABEL:
        issues.append(path + '.label: must be exactly "House answers"')
    answers = item.get("answers")
    out_answers = []
    if not isinstance(answers, list) or not answers or len(answers) > MAX_ANSWERS:
        issues.append(path + ".answers: required array of 1-20 answers")
    else:
        seen = set()
        for index, answer in enumerate(answers):
            apath = "%s.answers[%d]" % (path, index)
            if not isinstance(answer, dict):
                issues.append(apath + ": must be an object")
                continue
            abefore = len(issues)
            aid = answer.get("id")
            if not is_text(aid, MAX_ID):
                issues.append(apath + ".id: required non-empty string (max 64)")
            elif aid in seen:
                issues.append(apath + ".id: duplicate answer id")
            if not is_text(answer.get("text"), 200):
                issues.append(apath + ".text: required non-empty string")
            aliases = answer.get("aliases")
            if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
                issues.append(apath + ".aliases: required array (max 50)")
            else:
                for j, alias in enumerate(aliases):
                    if not is_text(alias, 200):
                        issues.append("%s.aliases[%d]: must be a non-empty string" % (apath, j))
            if not is_int(answer.get("points"), 0, MAX_POINTS):
                issues.append(apath + ".points: required integer 0-1000")
            if len(issues) == abefore:
                seen.add(aid)
                out_answers.append({
                    "id": aid,
                    "text": answer["text"],
                    "aliases": list(aliases),
                    "points": answer["points"],
                })
    if len(issues) != before:
        return None
    return {
        "id": item["id"],
        "question": item["question"],
        "label": HOUSE_LABEL,
        "answers": out_answers,
    }


def _round_id(game_no, board_no):
    return "g%db%d" % (game_no, board_no)


def _start_board(state, index):
    state["boardIndex"] = index
    state["roundId"] = _round_id(state["gameNo"], index + 1)
    state["roundPhase"] = "closed"
    state["revealed"] = []
    state["strikes"] = 0
    state["buzzer"] = {"open": False, "winner": None, "winnerSeq": None}
    state["control"] = None
    state["stealFrom"] = None
    state["awardedTo"] = None
    state["awardedPoints"] = None


def initialize(config, players, content):
    """Returns ``(state, None)`` or ``(None, error_code)``."""
    if len(players) < MIN_PLAYERS:
        return None, "not_enough_players"
    boards = copy.deepcopy(content["items"])
    if not boards:
        return None, "content_insufficient"
    state = {
        "mode": MODE_ID,
        "title": content["title"],
        "gameNo": config["gameNo"],
        "order": [p["id"] for p in players],
        "boards": boards,
        "awards": [],
        "scores": dict((p["id"], p["score"]) for p in players),
        "ended": False,
    }
    _start_board(state, 0)
    return state, None


def _board(state):
    return state["boards"][state["boardIndex"]]


def _revealed_points(state):
    revealed = set(state["revealed"])
    return sum(a["points"] for a in _board(state)["answers"] if a["id"] in revealed)


def _find_answer(state, payload):
    """Returns ``(answer, error_code)`` for ``{answerId}`` or ``{text}``."""
    answers = _board(state)["answers"]
    answer_id = payload.get("answerId")
    if answer_id is not None:
        for answer in answers:
            if answer["id"] == answer_id:
                return answer, None
        return None, "unknown_answer"
    text = clean_text(payload.get("text"), MAX_MATCH_TEXT)
    if text is None:
        return None, "invalid_payload"
    key = match_key(text)
    matched = None
    for answer in answers:
        keys = [match_key(answer["text"])] + [match_key(a) for a in answer["aliases"]]
        if key in keys:
            if answer["id"] not in state["revealed"]:
                return answer, None
            matched = matched or answer
    if matched is not None:
        return matched, None
    return None, "no_match"


def reduce(state, actor, action, server_sequence):
    """Pure transition. Returns ``(new_state, None)`` or ``(state, error_code)``."""
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
        s["buzzer"]["open"] = False
        return s, None

    if state["ended"]:
        return state, "invalid_phase"

    if kind == "feud.buzz":
        if role != "player" or pid not in state["order"]:
            return state, "forbidden"
        if state["buzzer"]["winner"] is not None:
            return state, "buzzer_taken"
        if state["roundPhase"] != "buzzer_open" or not state["buzzer"]["open"]:
            return state, "buzzer_closed"
        s = copy.deepcopy(state)
        s["buzzer"] = {"open": False, "winner": pid, "winnerSeq": server_sequence}
        s["control"] = pid
        s["roundPhase"] = "answering"
        return s, None

    if not kind or not isinstance(kind, str):
        return state, "unknown_action"
    if kind.startswith(ACTION_PREFIX) or kind == "round.next":
        if role != "operator":
            return state, "forbidden"

    if kind == "feud.open_buzzer":
        if state["buzzer"]["winner"] is not None:
            return state, "buzzer_taken"
        if state["roundPhase"] != "closed":
            return state, "invalid_phase"
        s = copy.deepcopy(state)
        s["buzzer"]["open"] = True
        s["roundPhase"] = "buzzer_open"
        return s, None

    if kind == "feud.reset_buzzer":
        if state["roundPhase"] == "awarded":
            return state, "round_awarded"
        s = copy.deepcopy(state)
        s["buzzer"] = {"open": False, "winner": None, "winnerSeq": None}
        s["control"] = None
        s["stealFrom"] = None
        s["roundPhase"] = "closed"
        return s, None

    if kind == "feud.reveal":
        answer, err = _find_answer(state, payload)
        if err:
            return state, err
        if answer["id"] in state["revealed"]:
            return state, "already_revealed"
        s = copy.deepcopy(state)
        s["revealed"] = [a["id"] for a in _board(s)["answers"]
                         if a["id"] in set(s["revealed"]) | {answer["id"]}]
        return s, None

    if kind == "feud.strike":
        if state["roundPhase"] not in ("answering", "steal"):
            return state, "invalid_phase"
        if state["strikes"] >= MAX_STRIKES:
            return state, "max_strikes"
        s = copy.deepcopy(state)
        s["strikes"] += 1
        return s, None

    if kind == "feud.steal":
        target = payload.get("playerId")
        if target not in state["order"]:
            return state, "unknown_player"
        if state["roundPhase"] != "answering":
            return state, "invalid_phase"
        if state["strikes"] < MAX_STRIKES:
            return state, "steal_not_available"
        if target == state["control"]:
            return state, "invalid_payload"
        s = copy.deepcopy(state)
        s["stealFrom"] = s["control"]
        s["control"] = target
        s["roundPhase"] = "steal"
        return s, None

    if kind == "feud.award":
        if state["roundPhase"] == "awarded":
            return state, "round_awarded"
        target = payload.get("playerId", state["control"])
        if target is None:
            return state, "no_control"
        if target not in state["order"]:
            return state, "unknown_player"
        # Dedup key is round + board, independent of actionId.
        key = "%s:%s" % (state["roundId"], _board(state)["id"])
        if key in state["awards"]:
            return state, "already_awarded"
        s = copy.deepcopy(state)
        points = _revealed_points(s)
        s["awards"].append(key)
        s["scores"][target] = s["scores"].get(target, 0) + points
        s["awardedTo"] = target
        s["awardedPoints"] = points
        s["buzzer"]["open"] = False
        s["roundPhase"] = "awarded"
        return s, None

    if kind == "round.next":
        if state["boardIndex"] + 1 >= len(state["boards"]):
            return state, "content_exhausted"
        s = copy.deepcopy(state)
        _start_board(s, s["boardIndex"] + 1)
        return s, None

    return state, "unknown_action"


def project(state, viewer):
    """Role projection. ``viewer`` is ``{role, playerId?}``. Pure."""
    board = _board(state)
    revealed = set(state["revealed"])
    slots = []
    for index, answer in enumerate(board["answers"]):
        if answer["id"] in revealed:
            slots.append({"index": index, "revealed": True, "id": answer["id"],
                          "text": answer["text"], "points": answer["points"]})
        else:
            slots.append({"index": index, "revealed": False})
    round_points = state["awardedPoints"] if state["awardedTo"] is not None else _revealed_points(state)
    view = {
        "title": state["title"],
        "label": HOUSE_LABEL,
        "answerSource": "house",
        "board": state["boardIndex"] + 1,
        "boardCount": len(state["boards"]),
        "question": board["question"],
        "roundPhase": "ended" if state["ended"] else state["roundPhase"],
        "slots": slots,
        "strikes": state["strikes"],
        "maxStrikes": MAX_STRIKES,
        "roundPoints": round_points,
        "buzzer": {"open": state["buzzer"]["open"], "winner": state["buzzer"]["winner"]},
        "control": state["control"],
        "stealFrom": state["stealFrom"],
        "awardedTo": state["awardedTo"],
        "awardedPoints": state["awardedPoints"],
    }
    if viewer.get("role") == "operator":
        view["boardId"] = board["id"]
        view["boardsRemaining"] = len(state["boards"]) - state["boardIndex"] - 1
        view["answers"] = [
            {"id": a["id"], "text": a["text"], "aliases": list(a["aliases"]),
             "points": a["points"], "revealed": a["id"] in revealed}
            for a in board["answers"]
        ]
    return view
