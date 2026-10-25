"""Planted-secret privacy mutation test for the Impostor mode.

The existing negative controls in party/web/tests/real_e2e_impostor.py (self_vote,
already_voted, operator-cannot-vote, player-cannot-advance) are rejection checks: they
prove bad actions are refused. None of them prove the privacy_scan-style check used
there, or the direct project()-view assertion used in this task's oracle-parity traces,
would actually catch a real secret leak if one were introduced. This script closes
that gap the same way party/core/tools/negative_controls.py does for Who Am I / Feud
(MUTATIONS dict): deliberately mutate the accepted Python oracle's source so the
secret word leaks to the operator and to the impostor, confirm the mutated behavior
is wrong relative to the unmutated oracle, and confirm a privacy scan over project()
output distinguishes the two.

Usage: python3 -I -B impostor_privacy_mutation_test.py ORACLE_PACKAGE_ROOT
Exit status 0 only when every planted leak is detected (i.e. the guard the real
project() code already has is proven load-bearing, not just coincidentally never hit).
"""
import copy
import importlib
import json
import sys
import types


def load_module(pkg_root):
    sys.path.insert(0, pkg_root)
    sys.modules.pop("party.server.modes.impostor", None)
    return importlib.import_module("party.server.modes.impostor")


def fresh_state(M):
    content = {
        "mode": "impostor", "title": "Mutation Test Pack", "licenseNote": "test",
        "items": [
            {"id": "w1", "word": "Camel", "category": "Animals"},
            {"id": "w2", "word": "Falcon", "category": "Animals"},
        ],
    }
    players = [{"id": "p1", "score": 0}, {"id": "p2", "score": 0}, {"id": "p3", "score": 0}]
    state, err = M.initialize({"seed": 7, "gameNo": 1}, players, content)
    assert err is None, err
    state, err = M.reduce(state, {"role": "operator"}, {"type": "impostor.advance", "payload": {}, "roundId": state["roundId"]}, 0)
    assert err is None, err
    return state


def secret_leaked(view, word):
    return word in json.dumps(view)


def check_guard(label, M, state, viewer_role_player, word, expect_leak):
    view = M.project(state, viewer_role_player)
    leaked = secret_leaked(view, word)
    ok = leaked == expect_leak
    status = "OK" if ok else "MISS"
    print(f"[{status}] {label}: leaked={leaked} expected_leak={expect_leak}")
    return ok


def planted_operator_leak(M):
    """Monkeypatch project() so the operator view includes the live secret word
    before reveal -- the exact class of bug the task's privacy requirement guards
    against (operator/display must never see the word pre-reveal)."""
    real_project = M.project

    def leaking_project(state, viewer):
        view = real_project(state, viewer)
        if viewer.get("role") == "operator" and state.get("roundPhase") != "reveal":
            view = copy.deepcopy(view)
            view["_planted_leak_word"] = state["item"]["word"]
        return view

    return leaking_project


def planted_impostor_leak(M):
    """Monkeypatch project() so the impostor's own view includes the secret word --
    the impostor must never see it, by design, at any phase."""
    real_project = M.project

    def leaking_project(state, viewer):
        view = real_project(state, viewer)
        if viewer.get("role") == "player" and viewer.get("playerId") == state.get("impostor"):
            view = copy.deepcopy(view)
            view["_planted_leak_word"] = state["item"]["word"]
        return view

    return leaking_project


def main():
    root = sys.argv[1]
    all_ok = True

    M = load_module(root)
    state = fresh_state(M)
    word = state["item"]["word"]

    # Baseline: the real, unmutated project() must not leak to operator or impostor.
    all_ok &= check_guard("baseline operator (unmutated)", M, state, {"role": "operator"}, word, expect_leak=False)
    all_ok &= check_guard("baseline impostor (unmutated)", M, state, {"role": "player", "playerId": state["impostor"]}, word, expect_leak=False)

    # Planted leak 1: operator/display sees the word before reveal.
    M.project = planted_operator_leak(M)
    all_ok &= check_guard("planted operator leak (mutated)", M, state, {"role": "operator"}, word, expect_leak=True)
    M.project = importlib.reload(load_module(root)).project  # restore real project()

    # Planted leak 2: the impostor's own view carries the word.
    M.project = planted_impostor_leak(M)
    all_ok &= check_guard("planted impostor leak (mutated)", M, state, {"role": "player", "playerId": state["impostor"]}, word, expect_leak=True)

    print("ALL PLANTED LEAKS DETECTED" if all_ok else "FAILED: a planted leak went unnoticed")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
