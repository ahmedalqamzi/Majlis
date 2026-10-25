#!/bin/sh
# Builds the L01B native transport CLI with the Command Line Tools only.
#
#   party/native/build.sh OUT_DIR
#
# Produces OUT_DIR/majlis-party-native (production) and
# OUT_DIR/majlis-party-native-test (-D MAJLIS_TEST_LOOPBACK, loopback-only,
# reference wire API and fault injection; never shipped). The three reviewed
# scripts are embedded as base64 text and evaluated once at startup.
set -eu
OUT=${1:?usage: build.sh OUT_DIR}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
SWIFTC=${SWIFTC:-/Library/Developer/CommandLineTools/usr/bin/swiftc}
SDK=${SDK:-/Library/Developer/CommandLineTools/SDKs/MacOSX27.sdk}
TARGET=${TARGET:-arm64-apple-macosx14.0}
mkdir -p "$OUT/gen"
GEN="$OUT/gen/EmbeddedSources.swift"
"$HERE/gen_embedded_sources.sh" "$GEN"
SRC="$HERE/Sources/Engine.swift $HERE/Sources/Support.swift $HERE/Sources/HTTP.swift $HERE/Sources/Server.swift $HERE/Sources/main.swift $GEN"
# shellcheck disable=SC2086
"$SWIFTC" -sdk "$SDK" -target "$TARGET" -O -module-name MajlisNative $SRC -o "$OUT/majlis-party-native"
# shellcheck disable=SC2086
"$SWIFTC" -sdk "$SDK" -target "$TARGET" -O -module-name MajlisNative -D MAJLIS_TEST_LOOPBACK $SRC -o "$OUT/majlis-party-native-test"
# The production binary must not contain the test surface switches.
for marker in "#csprng-fail" "--surface" "test build binds loopback only"; do
  if strings -a "$OUT/majlis-party-native" | grep -F -q -e "$marker"; then
    echo "production binary contains test marker: $marker" >&2
    exit 1
  fi
done
echo "built $OUT/majlis-party-native $OUT/majlis-party-native-test"
