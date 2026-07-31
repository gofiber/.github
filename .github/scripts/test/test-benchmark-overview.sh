#!/usr/bin/env bash
# Tests the overview delta logic in benchmark-pages/index.html: the median
# baseline, the quantization-flip floors, and legacy 3-arg compatibility.
# Run from anywhere: bash .github/scripts/test/test-benchmark-overview.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PAGE="$SCRIPT_DIR/../../actions/benchmark-pages/index.html"

fails=0
check() {
  if [ "$2" = "$3" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1"
    echo "       want: $2"
    echo "       got:  $3"
    fails=$((fails + 1))
  fi
}

SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

# the first <script> block is the DOM-free model library, exported for node
awk '/<script>/ { flag = 1; next } /<\/script>/ { exit } flag' "$PAGE" > "$SANDBOX/app.js"
if [ ! -s "$SANDBOX/app.js" ]; then
  echo "could not extract the library script from $PAGE"
  exit 1
fi

delta() { # SERIES METRIC ('-' = none) -> rounded pct or "null"
  node -e '
    const lib = require(process.argv[1]);
    const arr = JSON.parse(process.argv[2]);
    const metric = process.argv[3] === "-" ? undefined : process.argv[3];
    const d = lib.seriesDelta(arr, 0, arr.length - 1, metric);
    console.log(d === null ? "null" : Math.round(d.pct));
  ' "$SANDBOX/app.js" "$1" "$2"
}

echo "--- the median damps, the latest run decides"
check "a step against a flat history shows" "100" "$(delta '[100,100,100,200]' 'ns/op')"
check "an old spike is damped by the median" "2" "$(delta '[100,300,100,100,102]' 'ns/op')"

echo "--- quantization flips stay out of the overview"
check "a 1 byte flip is not a mover" "null" "$(delta '[1,1,1,0]' 'B/op')"
check "a real allocation is" "200" "$(delta '[8,8,8,24]' 'B/op')"
check "the 0/1 allocs zone is quiet" "null" "$(delta '[1,1,1,0]' 'allocs/op')"
check "1 to 2 allocs still moves" "100" "$(delta '[1,1,1,2]' 'allocs/op')"
check "sub-nanosecond wobble is quiet" "null" "$(delta '[0.5,0.5,0.5,0.9]' 'ns/op')"

echo "--- legacy callers without a metric keep the old behavior"
check "no metric, no floor" "-100" "$(delta '[1,1,1,0]' '-')"

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails check(s) failed"
  exit 1
fi
echo "all checks passed"
