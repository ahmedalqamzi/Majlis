"""Run tests/test_impostor_http.py (real HTTP + SSE Impostor tests) against a
native binary instead of `python -m party.server`.

Companion to reference_wire.py, which only globs tests/test_http_*.py and so
never exercises test_impostor_http.py (named without that prefix). This file
duplicates reference_wire.py's exact NativePopen-swap technique rather than
editing it, so the already-accepted first-pair harness is left untouched.

    python3 -I -B party/native/harness/reference_wire_impostor.py NATIVE_BIN
    python3 -I -B party/native/harness/reference_wire_impostor.py --python   (baseline, no swap)

A suite that would launch the Python server anyway makes this runner fail.
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TESTS = os.path.join(ROOT, "tests")


def main(argv):
    baseline = len(argv) == 2 and argv[1] == "--python"
    if len(argv) != 2 or not (baseline or os.access(argv[1], os.X_OK)):
        sys.stderr.write("usage: reference_wire_impostor.py NATIVE_BIN | --python\n")
        return 2
    native = None if baseline else os.path.abspath(argv[1])
    swapped = []
    original = subprocess.Popen

    class NativePopen(original):
        def __init__(self, args, *rest, **kwargs):
            if native is None:
                pass
            elif isinstance(args, (list, tuple)) and len(args) >= 3 and list(args[1:3]) == ["-m", "party.server"]:
                args = [native] + list(args[3:])
                swapped.append(1)
            elif isinstance(args, (list, tuple)) and "party.server" in args:
                raise AssertionError("reference server launched outside the native swap")
            original.__init__(self, args, *rest, **kwargs)

    subprocess.Popen = NativePopen
    sys.path.insert(0, TESTS)
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    names = ["test_impostor_http"]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    sys.stdout.write("native launches: %d\n" % len(swapped))
    if native is not None and not swapped:
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
