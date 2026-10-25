"""Text normalisation shared by names, guesses and answer matching."""

import unicodedata

MAX_NAME_LEN = 24

# Bidi embedding/override/isolate controls allow visual spoofing of names.
_BIDI_CONTROLS = set(
    [chr(c) for c in range(0x202A, 0x202F)] + [chr(c) for c in range(0x2066, 0x206A)]
)


def clean_text(value, max_len):
    """Return NFC text with collapsed whitespace, or None if unacceptable."""
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    for ch in value:
        if ch in _BIDI_CONTROLS:
            return None
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs", "Co", "Cn") and not ch.isspace():
            return None
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > max_len:
        return None
    return collapsed


def clean_name(value):
    return clean_text(value, MAX_NAME_LEN)


def match_key(value):
    """Case/width-insensitive comparison key for names and answers."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
