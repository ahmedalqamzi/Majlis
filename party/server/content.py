"""Content pack loading and validation.

Packs live at ``{content_dir}/{mode}.json`` with ``schemaVersion:1``,
``mode``, ``title``, ``licenseNote`` and ``items`` (CONTRACT.md, "Content
format"). Item validation is supplied per mode. Loading never raises for bad
content: it reports ``content_missing`` or ``content_invalid`` with issue
strings that name JSON paths only, never content values.
"""

import json
import os

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 2000
MAX_TEXT = 500
MAX_ID = 64

STATUS_OK = "ok"
STATUS_MISSING = "content_missing"
STATUS_INVALID = "content_invalid"


class ContentStatus(object):
    __slots__ = ("mode", "status", "pack", "issues")

    def __init__(self, mode, status, pack=None, issues=None):
        self.mode = mode
        self.status = status
        self.pack = pack
        self.issues = list(issues or [])

    @property
    def ok(self):
        return self.status == STATUS_OK


def default_content_dir():
    """``party/content`` next to this package; it may legitimately be absent."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, os.pardir, "content"))


def is_text(value, max_len=MAX_TEXT):
    return isinstance(value, str) and value.strip() != "" and len(value) <= max_len


def is_int(value, lo, hi):
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _reject_constant(_name):
    raise ValueError("non-finite number")


def parse_pack(mode, data, validate_item, validate_items=None):
    """Validate decoded JSON. Returns ``(pack, issues)``; pack is None on error.

    ``validate_items(items, issues)`` is an optional cross-item check that runs
    once every item is individually valid.
    """
    if not isinstance(data, dict):
        return None, ["root: must be an object"]
    issues = []
    if not is_int(data.get("schemaVersion"), SCHEMA_VERSION, SCHEMA_VERSION):
        issues.append("schemaVersion: must be 1")
    if data.get("mode") != mode:
        issues.append("mode: must equal the file's mode id")
    if not is_text(data.get("title")):
        issues.append("title: required non-empty string")
    if not is_text(data.get("licenseNote"), 2000):
        issues.append("licenseNote: required non-empty string")
    items = data.get("items")
    normalized = []
    if not isinstance(items, list) or not items:
        issues.append("items: required non-empty array")
    elif len(items) > MAX_ITEMS:
        issues.append("items: too many items")
    else:
        seen = set()
        for index, item in enumerate(items):
            path = "items[%d]" % index
            before = len(issues)
            out = validate_item(item, path, issues)
            if len(issues) == before and out is not None:
                if out["id"] in seen:
                    issues.append(path + ".id: duplicate id")
                else:
                    seen.add(out["id"])
                    normalized.append(out)
        if not issues and validate_items is not None:
            validate_items(normalized, issues)
    if issues:
        return None, issues
    return {
        "mode": mode,
        "title": data["title"],
        "licenseNote": data["licenseNote"],
        "items": normalized,
    }, []


def load_mode(content_dir, mode, validate_item, validate_items=None):
    path = os.path.join(content_dir, mode + ".json")
    if not os.path.isdir(content_dir) or not os.path.isfile(path):
        return ContentStatus(mode, STATUS_MISSING)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return ContentStatus(mode, STATUS_INVALID, issues=["file: unreadable"])
    if len(raw) > MAX_FILE_BYTES:
        return ContentStatus(mode, STATUS_INVALID, issues=["file: exceeds size limit"])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ContentStatus(mode, STATUS_INVALID, issues=["file: not UTF-8"])
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return ContentStatus(mode, STATUS_INVALID, issues=["file: not valid JSON"])
    pack, issues = parse_pack(mode, data, validate_item, validate_items)
    if pack is None:
        return ContentStatus(mode, STATUS_INVALID, issues=issues)
    return ContentStatus(mode, STATUS_OK, pack=pack)


def load_all(content_dir, registry):
    """Load every implemented mode's pack. Returns ``{mode: ContentStatus}``."""
    statuses = {}
    for spec in registry.values():
        if spec.implemented:
            statuses[spec.id] = load_mode(content_dir, spec.id, spec.module.validate_item,
                                          getattr(spec.module, "validate_items", None))
    return statuses
