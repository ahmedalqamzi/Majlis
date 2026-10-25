# party/core — L01A shared-core parity (not production)

A pure-JS copy of the party server's room authority (RoomManager, Who Am I and
Feud reducers, content loading, text rules, JSON body rules), plus the tools
that prove it behaves like the accepted Python server at 77ecabd.

Nothing here is wired to a listener, UI, device or build. `party/server`,
`tests`, `party/content` and `party/web` are unchanged. L01B (any production
use) waits for Skull's review.

## Files

| File | Role |
| --- | --- |
| `majlis-core.js` | Pure core. Classic script, one global `MajlisCore`. No DOM, network, timers, storage, eval, `Function()`, `Math.random`, `Date` or host Unicode APIs. Entropy is passed in as a byte pool, and time as a `now` argument. |
| `unicode13.generated.js` | NFC/NFKC/NFD/NFKD, casefold, `isspace`, clean-text rejection and code-uppercase tables, generated from the oracle's CPython 3.9.6 `unicodedata` 13.0.0. |
| `tools/gen_unicode_tables.py` | Regenerates the tables. The output must be byte-identical; the header records the tables' sha256. |
| `tools/oracle_record.py` | Imports the 77ecabd Python server unchanged. It replaces only the seams (seeded secrets, injected clock, no-wait condition, fake request) and records golden traces, randomized traces and unit suites as JSONL. |
| `runner/trace-runner.js` | Replays one recorded line against `MajlisCore` and emits the same observation shape. |
| `runner/jsc_runner.swift` | macOS CLI. Evaluates only the tables, the core and the runner in a bare `JSContext` (no injected natives), then streams the JSONL through it. |
| `tools/compare.py` | Semantic comparison with a declared nondeterminism mapping (N1–N6 in its docstring). Anything else is red. |
| `tools/negative_controls.py` | Mutates a copy of the core or tables (rules, buzz order, private-field leaks, dedup, entropy order, table entries). Every mutation must go red. |
| `runner/icu-drift-runner.js`, `tools/check_icu_drift.py` | Rebuild the host-ICU vs Unicode-13 drift set ("Unicode158") and check the core against Python on it. |
| `tools/token_scan.py` | Checks that exported evidence never holds a plaintext capability. Token fields must be `{"sha256": …}`. |

## Rerun (macOS, Command Line Tools only)

```
PY=/Library/Developer/CommandLineTools/usr/bin/python3
SWIFTC=/Library/Developer/CommandLineTools/usr/bin/swiftc
SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX27.sdk
ORACLE=<git archive 77ecabd party/server party/content tests>   # untouched extract
$SWIFTC -sdk $SDK -O -o jsc_runner party/core/runner/jsc_runner.swift
$PY -I -B party/core/tools/oracle_record.py $ORACLE traces
for s in errors shuffle depth json text content golden sweep fuzz; do
  ./jsc_runner party/core/unicode13.generated.js party/core/majlis-core.js \
    party/core/runner/trace-runner.js traces/$s.py.jsonl traces/$s.js.jsonl
done
$PY -I -B party/core/tools/compare.py traces compare            # exit 1 while N5 stays red
$PY -I -B party/core/tools/compare.py traces compare-st --self-test
$PY -I -B party/core/tools/negative_controls.py . traces negative ./jsc_runner
```

The recorder is deterministic from `--seed` (default `l01a-2026-10-24`).
Tokens in traces come from the seeded fake pool and are test-only.

## Known red difference and coverage holes

* **N5, JSON nesting depth.** CPython accepts nesting up to the depth where its
  stack-dependent RecursionError fires (about 985 in the recorder thread). The
  core rejects nesting from 900 up with `invalid_json`. Bodies nested 900–985
  deep therefore get different answers. This is kept red and needs a decision.
* **Not exercised by the core at all:** HTTP framing, Host/Origin checks,
  methods, paths, query strings, rate limits, server_busy, bearer-header
  parsing, SSE timing and peer-closed detection. The harness supplies valid
  headers, so `bad_request`, `unsupported_media_type`, `host_forbidden`,
  `origin_forbidden`, `method_not_allowed`, `not_found`, `query_not_allowed`,
  `rate_limited` and `server_busy` are unreached. `internal_error` is unreached,
  and `too_many_players` appears unreachable behind `room_full`.
* Entropy parity is proven only under the seeded fake derivation. Pool
  exhaustion maps to a harness exception, not to production `internal_error`.
* A Python string with a lone high surrogate followed by a lone low surrogate
  has no JS form (N6). None occurred in the recorded text suite.
