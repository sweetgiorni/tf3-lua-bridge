#!/usr/bin/env bash
# Copy the publishable part of the mod into the game's staging area, as <staging_area>/<modId>/.
# Usage: tools/package.sh [staging-area]   (default: TF3_STAGING_AREA, else the Steam userdata staging_area)
# The mod is mod.json, strings.json, _metadata/ and content/, plus the repo's LICENSE as license.txt (upload
# validation rejects a file named LICENSE: lowercase names with an extension only); everything else in the repo (tools/, docs,
# .git) stays out, so adding files under content/ needs no change here. Re-running updates the copy and
# removes files deleted from the source. _metadata/mod.io_fileid.txt, written by the game on the first
# upload, is kept.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STEAM="${TF3_STEAM:-/mnt/c/Program Files (x86)/Steam}"

STAGING="${1:-${TF3_STAGING_AREA:-}}"
if [ -z "$STAGING" ]; then
    for d in "$STEAM"/userdata/*/3493540/local/staging_area; do [ -d "$d" ] && STAGING="$d"; done
fi
[ -d "$STAGING" ] || { echo "staging area not found; pass it as an argument or set TF3_STAGING_AREA" >&2; exit 2; }

MODID="$(sed -n 's/.*"modId"[[:space:]]*:[[:space:]]*"\([a-z0-9_]*\)".*/\1/p' "$ROOT/mod.json" | head -1)"
[ -n "$MODID" ] || { echo "no modId in mod.json" >&2; exit 2; }
DEST="$STAGING/$MODID"

# Never publish scripts or executables (the platform rejects them).
bad="$(cd "$ROOT" && find content _metadata -type f \( -name '*.py' -o -name '*.sh' -o -name '*.bat' -o -name '*.exe' \))"
[ -z "$bad" ] || { echo "refusing to package, found: $bad" >&2; exit 1; }

rsync -a --delete --delete-excluded --filter='P _metadata/mod.io_fileid.txt' \
    --include=/mod.json --include=/strings.json --include='/_metadata/***' --include='/content/***' --exclude='*' \
    "$ROOT/" "$DEST/"
cp "$ROOT/LICENSE" "$DEST/license.txt"

cmp "$ROOT/LICENSE" "$DEST/license.txt"
for e in mod.json strings.json _metadata content; do
    [ -e "$ROOT/$e" ] || continue
    diff -rq --exclude mod.io_fileid.txt "$ROOT/$e" "$DEST/$e"
done
echo "packaged $MODID -> $DEST (verified identical)"
(cd "$DEST" && find . -type f | sort)
