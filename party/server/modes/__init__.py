"""Mode registry.

All ten contract mode ids are declared. Only ``who_am_i``, ``feud`` and
``impostor`` are implemented; the other seven are ``implemented: False`` and
the room rejects selecting or starting them (``mode_unimplemented``). They are
never presented as playable.

An implemented mode module provides ``MODE_ID``, ``TITLE``, ``ACTION_PREFIX``,
``MIN_PLAYERS``, ``validate_item(item, path, issues)``,
``initialize(config, players, content)``,
``reduce(state, actor, action, serverSequence)`` and ``project(state, viewer)``.
"""

import collections

from . import feud, impostor, who_am_i


class ModeSpec(object):
    __slots__ = ("id", "title", "implemented", "module", "action_prefix")

    def __init__(self, mode_id, title, module=None, action_prefix=None):
        self.id = mode_id
        self.title = title
        self.module = module
        self.implemented = module is not None
        self.action_prefix = action_prefix


def _build():
    specs = [
        ModeSpec(who_am_i.MODE_ID, who_am_i.TITLE, who_am_i, who_am_i.ACTION_PREFIX),
        ModeSpec(feud.MODE_ID, feud.TITLE, feud, feud.ACTION_PREFIX),
        ModeSpec(impostor.MODE_ID, impostor.TITLE, impostor, impostor.ACTION_PREFIX),
        ModeSpec("draw_guess", "Draw & Guess", action_prefix="draw_guess."),
        ModeSpec("drawing_chain", "Drawing Chain", action_prefix="drawing_chain."),
        ModeSpec("bluff", "Bluff", action_prefix="bluff."),
        ModeSpec("werewolf", "Werewolf", action_prefix="werewolf."),
        ModeSpec("taboo", "Forbidden Words", action_prefix="taboo."),
        ModeSpec("wavelength", "Spectrum", action_prefix="wavelength."),
        ModeSpec("escape_room", "Escape Room", action_prefix="escape_room."),
    ]
    return collections.OrderedDict((s.id, s) for s in specs)


REGISTRY = _build()


def get_mode(mode_id):
    return REGISTRY.get(mode_id) if isinstance(mode_id, str) else None


def mode_for_action(action_type):
    """Mode spec owning a mode-scoped action type, or None."""
    for spec in REGISTRY.values():
        if action_type.startswith(spec.action_prefix):
            return spec
    return None


def catalog(contents):
    """Public, secret-free list of modes with implementation/content status."""
    out = []
    for spec in REGISTRY.values():
        status = contents.get(spec.id)
        out.append({
            "id": spec.id,
            "title": spec.title,
            "implemented": spec.implemented,
            "content": status.status if (spec.implemented and status is not None) else None,
        })
    return out
