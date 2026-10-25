"""Stable machine error codes.

Messages are fixed strings. They never interpolate request data, content,
tokens or state, so an error body cannot leak secrets.
"""

ERRORS = {
    # transport / request shape
    "bad_request": (400, "Malformed request."),
    "invalid_json": (400, "Request body must be a JSON object."),
    "length_required": (411, "Content-Length is required; chunked bodies are not accepted."),
    "body_too_large": (413, "Request body exceeds the size limit."),
    "unsupported_media_type": (415, "Content-Type must be application/json."),
    "query_not_allowed": (400, "Query strings are not accepted on API routes; send credentials only in the Authorization header."),
    "origin_forbidden": (403, "Cross-origin requests are not allowed."),
    "host_forbidden": (403, "Host header is not an allowed server origin."),
    "not_found": (404, "Not found."),
    "method_not_allowed": (405, "Method not allowed."),
    "rate_limited": (429, "Too many requests; retry later."),
    "too_many_rooms": (503, "Room capacity reached; retry later."),
    "too_many_streams": (429, "Too many open event streams."),
    "server_busy": (503, "Server is busy."),
    "internal_error": (500, "Internal error."),
    # rooms / credentials
    "room_not_found": (404, "Room not found or expired."),
    "room_expired": (410, "Room expired."),
    "unauthorized": (401, "Missing or invalid credentials."),
    "token_expired": (401, "Credentials expired."),
    "forbidden": (403, "This role may not perform that action."),
    "invalid_config": (400, "Invalid room configuration."),
    "invalid_name": (400, "Name must be 1-24 visible characters."),
    "name_taken": (409, "That name is already used in this room."),
    "room_full": (409, "Room is full."),
    "room_closed": (409, "Room is closed."),
    "game_in_progress": (409, "A game is in progress; new players may join between games."),
    # actions
    "invalid_action_id": (400, "actionId must be 1-64 characters of [A-Za-z0-9._:-]."),
    "invalid_action": (400, "Action must contain a valid type and optional string roundId."),
    "unknown_action": (400, "Unknown action type."),
    "invalid_payload": (400, "Invalid action payload."),
    "round_required": (400, "This action requires the current roundId."),
    "stale_round": (409, "roundId does not match the current round."),
    "invalid_phase": (409, "Action is not valid in the current phase."),
    "paused": (409, "Game is paused."),
    "wrong_mode": (409, "Action belongs to a different mode."),
    "unknown_mode": (400, "Unknown mode."),
    "mode_unimplemented": (409, "Mode is not implemented in this server build."),
    "mode_not_selected": (409, "Select a mode first."),
    "content_missing": (409, "Content pack for this mode is missing."),
    "content_invalid": (409, "Content pack for this mode failed validation."),
    "content_insufficient": (409, "Content pack has too few items for this game."),
    "content_exhausted": (409, "No more content items remain for another round."),
    "not_enough_players": (409, "Not enough players for this room or mode."),
    "too_many_players": (409, "Too many players for this room or mode."),
    "unknown_player": (400, "Unknown player."),
    "already_revealed": (409, "Already revealed."),
    "no_pending_guess": (409, "That player has no pending guess."),
    "already_awarded": (409, "Points for this round were already awarded."),
    "buzzer_closed": (409, "Buzzer is not open."),
    "buzzer_taken": (409, "Another player already won the buzzer."),
    "unknown_answer": (400, "Unknown answer."),
    "no_match": (409, "No unrevealed answer matches that text."),
    "max_strikes": (409, "Maximum strikes reached."),
    "steal_not_available": (409, "A steal is only available after the maximum strikes."),
    "no_control": (409, "No player is in control; specify playerId."),
    "round_awarded": (409, "This round was already awarded."),
    "self_vote": (409, "Players may not vote for themselves."),
    "already_voted": (409, "This player has already voted this round."),
}


class PartyError(Exception):
    """Raised for request failures that map to a stable error code."""

    def __init__(self, code, retry_after=None):
        if code not in ERRORS:
            code = "internal_error"
        Exception.__init__(self, code)
        self.code = code
        self.retry_after = retry_after

    @property
    def status(self):
        return ERRORS[self.code][0]


def status_for(code):
    return ERRORS.get(code, ERRORS["internal_error"])[0]


def error_body(code):
    if code not in ERRORS:
        code = "internal_error"
    return {"code": code, "message": ERRORS[code][1]}
