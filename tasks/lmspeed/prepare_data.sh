#!/usr/bin/env bash
# Reproduce the lmspeed corpus on the node (the gate's machine), not locally:
# the held-out split must exist only where the gate can read it and the jail
# cannot. Usage: prepare_data.sh [ROOT]   (default $STIGMERGEIA_DATA_ROOT/lmspeed-data,
# the corpus directory corpus.json names under the data root the gate reads)
#
#   ROOT/public/enwik8.train   bytes [0, 90M)     agents read it (mounted read-only in the jail)
#   ROOT/public/enwik8.valid   bytes [90M, 95M)   agents read it; the gate's --split train windows
#   ROOT/private/enwik8.test   bytes [95M, 100M)  the gate only; never mounted in any jail
#   ROOT/src/                  the zip and the full file (contains the test split: 0700)
set -euo pipefail
if [ $# -ge 1 ]; then
  root=$1
elif [ -n "${STIGMERGEIA_DATA_ROOT:-}" ]; then
  root=$STIGMERGEIA_DATA_ROOT/lmspeed-data
else
  echo "prepare_data.sh: give ROOT or set STIGMERGEIA_DATA_ROOT (the corpus goes in \$STIGMERGEIA_DATA_ROOT/lmspeed-data)" >&2
  exit 2
fi
mkdir -p "$root/src" "$root/public" "$root/private"
chmod 700 "$root/src" "$root/private"
cd "$root/src"
[ -f enwik8.zip ] || curl -sSL --max-time 600 -o enwik8.zip https://mattmahoney.net/dc/enwik8.zip
[ -f enwik8 ] || unzip -o -q enwik8.zip
echo "a1fa5ffddb56f4953e226637dabbb36a  enwik8" | md5sum -c -
head -c 90000000 enwik8 > "$root/public/enwik8.train"
tail -c +90000001 enwik8 | head -c 5000000 > "$root/public/enwik8.valid"
tail -c 5000000 enwik8 > "$root/private/enwik8.test"
chmod 444 "$root/public/"*
chmod 600 "$root/private/enwik8.test"
sha256sum "$root/public/"* "$root/private/"*
