#!/bin/sh
# Stands in for a Python interpreter in harnesses that start the server as
# `$MAJLIS_SERVER_PY -m party.server ARGS` (party/web/tests/real_e2e.py):
# drops `-m party.server` and runs $MAJLIS_NATIVE_BIN ARGS instead.
set -eu
[ "${1:-}" = "-m" ] && [ "${2:-}" = "party.server" ] || { echo "native_shim: expected -m party.server" >&2; exit 2; }
shift 2
exec "${MAJLIS_NATIVE_BIN:?MAJLIS_NATIVE_BIN not set}" "$@"
