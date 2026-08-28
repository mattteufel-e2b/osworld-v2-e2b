#!/usr/bin/env bash
# Fetch the pinned xlang-ai/osworld-server payload into template/files/server/.
#
# The upstream repository publishes no license, so this repo does not
# redistribute its code. Instead, this script downloads the exact pinned commit
# from upstream at template-build-prep time and applies the local AT-SPI
# null-guard patch (patches/osworld-server-atspi-guards.patch). The fetched
# files (files/server/main.py, files/server/src/) are gitignored.
#
# Run this once before `npm run build`. Idempotent: re-running replaces the
# fetched payload with a fresh pinned copy and re-applies the patch.
set -euo pipefail

# Must match OSWORLD_SERVER_COMMIT in template/build.ts.
COMMIT="a3cc3f0c64e463f020d1a44780307e9b46cbcab1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DEST="$HERE/files/server"
PATCH="$REPO_ROOT/patches/osworld-server-atspi-guards.patch"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "[fetch_server] downloading xlang-ai/osworld-server@$COMMIT"
curl -fsSL -o "$WORK/server.tar.gz" \
    "https://github.com/xlang-ai/osworld-server/archive/$COMMIT.tar.gz"
mkdir -p "$WORK/upstream"
tar -xzf "$WORK/server.tar.gz" -C "$WORK/upstream" --strip-components=1

echo "[fetch_server] applying AT-SPI null-guard patch"
mkdir -p "$WORK/staged"
cp -R "$WORK/upstream/src" "$WORK/staged/src"
(cd "$WORK/staged" && patch -p1 -s < "$PATCH")

rm -rf "$DEST/src" "$DEST/main.py"
cp "$WORK/upstream/main.py" "$DEST/main.py"
cp -R "$WORK/staged/src" "$DEST/src"
echo "[fetch_server] done: $DEST/main.py + $DEST/src/ at $COMMIT (patched)"
