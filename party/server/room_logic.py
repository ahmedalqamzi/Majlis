"""Pure room-level reducer and role projection.

Room state is a plain dict containing no credentials. Tokens, presence,
replay logs and locking live in ``manager.Room``. Every function here is pure:
inputs are never mutated, and accepted transitions return a new dict.
"""

import copy

from . import PROTOCOL
from .modes import REGISTRY, catalog, get_mode, mode_for_action
from .textutil import match_key

HARD_MIN_PLAYERS = 3
HARD_MAX_PLAYERS = 12

LOBBY = "lobby"
PLAYING = "playing"
PAUSED = "paused"
ENDED = "ended"
CLOSED = "closed"

ROOM_ACTIONS = frozenset([
    "room.set_mode", "room.start", "room.pause", "room.resume", "room.end",
    "room.reset", "room.close", "round.next",
])


def new_room_state(code, operator_name, min_players, max_players):
    return {
        "code": code,
        "operatorName": operator_name,
        "limits": {"min": min_players, "max": max_players},
        "phase": LOBBY,
        "mode": None,
        "gameNo": 0,
        "players": [],
        "game": None,
    }


def apply_join(state, player_id, name):
    """Add a new player. Returns ``(state, None)`` or ``(state, error_code)``."""
    phase = state["phase"]
    if phase == CLOSED:
        return state, "room_closed"
    if phase in (PLAYING, PAUSED):
        return state, "game_in_progress"
    if len(state["players"]) >= state["limits"]["max"]:
        return state, "room_full"
    key = match_key(name)
    if any(match_key(p["name"]) == key for p in state["players"]):
        return state, "name_taken"
    s = copy.deepcopy(state)
    s["players"].append({"id": player_id, "name": name, "score": 0})
    return s, None


def _sync_scores(s):
    scores = s["game"].get("scores", {}) if s["game"] else {}
    for player in s["players"]:
        if player["id"] in scores:
            player["score"] = scores[player["id"]]


def _content_error(contents, mode):
    status = contents.get(mode)
    if status is None or status.status == "content_missing":
        return "content_missing"
    if not status.ok:
        return "content_invalid"
    return None


def reduce_room(state, actor, action, server_sequence, contents, seed):
    """Apply one action. Returns ``(new_state, None)`` or ``(state, error_code)``.

    ``actor`` is token-derived ``{role, playerId}``; ``action`` is a validated
    ``{type, roundId, payload}``; ``seed`` is only consumed by ``room.start``.
    """
    role = actor.get("role")
    kind = action["type"]
    payload = action.get("payload") or {}
    round_id = action.get("roundId")
    phase = state["phase"]

    if role == "display":
        return state, "forbidden"
    is_room_action = kind in ROOM_ACTIONS
    spec = None if is_room_action else mode_for_action(kind)
    if not is_room_action and spec is None:
        return state, "unknown_action"
    if is_room_action and role != "operator":
        return state, "forbidden"
    if phase == CLOSED:
        return state, "room_closed"
    game = state["game"]
    if phase in (PLAYING, PAUSED) and game is not None and round_id is not None \
            and round_id != game["roundId"]:
        return state, "stale_round"

    if kind == "room.set_mode":
        target = payload.get("mode")
        target_spec = get_mode(target)
        if target_spec is None:
            return state, "unknown_mode"
        if not target_spec.implemented:
            return state, "mode_unimplemented"
        if phase not in (LOBBY, ENDED):
            return state, "invalid_phase"
        s = copy.deepcopy(state)
        s["mode"] = target
        s["phase"] = LOBBY
        s["game"] = None
        return s, None

    if kind == "room.start":
        if phase not in (LOBBY, ENDED):
            return state, "invalid_phase"
        mode_spec = get_mode(state["mode"])
        if mode_spec is None:
            return state, "mode_not_selected"
        if not mode_spec.implemented:
            return state, "mode_unimplemented"
        err = _content_error(contents, mode_spec.id)
        if err:
            return state, err
        count = len(state["players"])
        if count < max(state["limits"]["min"], mode_spec.module.MIN_PLAYERS):
            return state, "not_enough_players"
        if count > state["limits"]["max"]:
            return state, "too_many_players"
        s = copy.deepcopy(state)
        s["gameNo"] += 1
        config = {"seed": seed, "gameNo": s["gameNo"]}
        game_state, err = mode_spec.module.initialize(
            config, copy.deepcopy(s["players"]), contents[mode_spec.id].pack)
        if err:
            return state, err
        s["game"] = game_state
        s["phase"] = PLAYING
        return s, None

    if kind == "room.pause":
        if phase != PLAYING:
            return state, "invalid_phase"
        s = copy.deepcopy(state)
        s["phase"] = PAUSED
        return s, None

    if kind == "room.resume":
        if phase != PAUSED:
            return state, "invalid_phase"
        s = copy.deepcopy(state)
        s["phase"] = PLAYING
        return s, None

    if kind == "room.end":
        if phase not in (PLAYING, PAUSED) or game is None:
            return state, "invalid_phase"
        module = REGISTRY[state["mode"]].module
        new_game, err = module.reduce(game, actor, {"type": "game.end", "payload": {}}, server_sequence)
        if err:
            return state, err
        s = copy.deepcopy(state)
        s["game"] = new_game
        s["phase"] = ENDED
        _sync_scores(s)
        return s, None

    if kind == "room.reset":
        clear = payload.get("clearScores", False)
        if not isinstance(clear, bool):
            return state, "invalid_payload"
        s = copy.deepcopy(state)
        s["phase"] = LOBBY
        s["game"] = None
        if clear:
            for player in s["players"]:
                player["score"] = 0
        return s, None

    if kind == "room.close":
        s = copy.deepcopy(state)
        s["phase"] = CLOSED
        s["game"] = None
        return s, None

    # round.next and mode-scoped actions are delegated to the active mode.
    if phase == PAUSED:
        return state, "paused"
    if phase != PLAYING or game is None:
        return state, "invalid_phase"
    if spec is not None and spec.id != state["mode"]:
        return state, "wrong_mode"
    if round_id is None:
        return state, "round_required"
    module = REGISTRY[state["mode"]].module
    new_game, err = module.reduce(game, actor, action, server_sequence)
    if err:
        return state, err
    s = copy.deepcopy(state)
    s["game"] = new_game
    _sync_scores(s)
    return s, None


def project_room(state, viewer, revision, presence, contents):
    """Role-projected snapshot. ``viewer`` is ``{role, playerId?}``.

    Common keys follow CONTRACT.md; ``operatorName``, ``playerLimits`` and
    the operator-only ``modes`` list are documented additive fields.
    """
    role = viewer["role"]
    pid = viewer.get("playerId") if role == "player" else None
    self_view = None
    if pid is not None:
        for player in state["players"]:
            if player["id"] == pid:
                self_view = {"id": player["id"], "name": player["name"]}
    players = [
        {"id": p["id"], "name": p["name"], "connected": presence.get(p["id"], 0) > 0, "score": p["score"]}
        for p in state["players"]
    ]
    game_view = None
    round_id = None
    game = state["game"]
    spec = get_mode(state["mode"])
    if game is not None and spec is not None and spec.implemented:
        game_view = spec.module.project(game, {"role": role, "playerId": pid})
        round_id = game["roundId"]
    snapshot = {
        "protocol": PROTOCOL,
        "code": state["code"],
        "revision": revision,
        "role": role,
        "self": self_view,
        "mode": state["mode"],
        "phase": state["phase"],
        "roundId": round_id,
        "players": players,
        "game": game_view,
        "operatorName": state["operatorName"],
        "playerLimits": dict(state["limits"]),
    }
    if role == "operator":
        snapshot["modes"] = catalog(contents)
    return snapshot
