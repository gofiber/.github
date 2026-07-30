#!/usr/bin/env bash
# The benchmark page's data pipeline end to end: the legacy github-action-benchmark
# format and the columnar v2 format must build the identical page model, and
# sync.sh must publish data and sync the page into a gh-pages checkout as one
# commit. Runs the real index.html logic (extracted, node) and the real sync.sh
# against local git fixtures, so it tests what ships rather than a copy.
# Run from anywhere: bash .github/scripts/test/test-benchmark-pages.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PAGES_DIR=$(cd "$SCRIPT_DIR/../../actions/benchmark-pages" && pwd)

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

# ---------- fixtures ----------
# legacy data.js exactly as github-action-benchmark wrote it: bare duplicates
# with the full tail as unit, per-metric entries, a single-metric benchmark,
# a multi-line commit message
cat > "$SANDBOX/legacy-data.js" <<'EOF'
window.BENCHMARK_DATA = {
  "lastUpdate": 1700000000000,
  "repoUrl": "https://github.com/gofiber/fiber",
  "entries": {
    "Benchmark": [
      {
        "commit": {
          "author": {"email": "a@b", "name": "A", "username": "a"},
          "committer": {"email": "a@b", "name": "A", "username": "a"},
          "distinct": true,
          "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "message": "feat: first\n\nbody",
          "timestamp": "2026-07-01T10:00:00+02:00",
          "tree_id": "t1",
          "url": "https://github.com/gofiber/fiber/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        },
        "date": 1700000000001,
        "tool": "go",
        "benches": [
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3)", "value": 58, "unit": "ns/op\t      24 B/op\t       1 allocs/op", "extra": "19444994 times\n4 procs"},
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - ns/op", "value": 58, "unit": "ns/op", "extra": "19444994 times\n4 procs"},
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - B/op", "value": 24, "unit": "B/op", "extra": "19444994 times\n4 procs"},
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - allocs/op", "value": 1, "unit": "allocs/op", "extra": "19444994 times\n4 procs"},
          {"name": "BenchmarkAppendMsg (github.com/gofiber/fiber/v3)", "value": 16.19, "unit": "ns/op\t1977.09 MB/s\t       0 B/op\t       0 allocs/op", "extra": "74023837 times\n4 procs"},
          {"name": "BenchmarkAppendMsg (github.com/gofiber/fiber/v3) - ns/op", "value": 16.19, "unit": "ns/op", "extra": "74023837 times\n4 procs"},
          {"name": "BenchmarkAppendMsg (github.com/gofiber/fiber/v3) - MB/s", "value": 1977.09, "unit": "MB/s", "extra": "74023837 times\n4 procs"},
          {"name": "BenchmarkAppendMsg (github.com/gofiber/fiber/v3) - B/op", "value": 0, "unit": "B/op", "extra": "74023837 times\n4 procs"},
          {"name": "BenchmarkAppendMsg (github.com/gofiber/fiber/v3) - allocs/op", "value": 0, "unit": "allocs/op", "extra": "74023837 times\n4 procs"},
          {"name": "Benchmark_Ctx_Get/header-8 (github.com/gofiber/fiber/v3)", "value": 0.5, "unit": "ns/op", "extra": "1000000 times\n4 procs"}
        ]
      },
      {
        "commit": {
          "author": {"email": "a@b", "name": "A", "username": "a"},
          "committer": {"email": "a@b", "name": "A", "username": "a"},
          "distinct": true,
          "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "message": "fix: second",
          "timestamp": "2026-07-02T10:00:00+02:00",
          "tree_id": "t2",
          "url": "https://github.com/gofiber/fiber/commit/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        },
        "date": 1700000100001,
        "tool": "go",
        "benches": [
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3)", "value": 61, "unit": "ns/op\t      24 B/op\t       1 allocs/op", "extra": "19444994 times\n4 procs"},
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - ns/op", "value": 61, "unit": "ns/op", "extra": "19444994 times\n4 procs"},
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - B/op", "value": 24, "unit": "B/op", "extra": "19444994 times\n4 procs"},
          {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - allocs/op", "value": 1, "unit": "allocs/op", "extra": "19444994 times\n4 procs"},
          {"name": "Benchmark_Ctx_Get/header-8 (github.com/gofiber/fiber/v3)", "value": 0.48, "unit": "ns/op", "extra": "1000000 times\n4 procs"}
        ]
      }
    ]
  }
}
EOF

cat > "$SANDBOX/output.txt" <<'EOF'
goos: linux
goarch: arm64
pkg: github.com/gofiber/fiber/v3
Benchmark_NewError-4                	19444994	        59.00 ns/op	      24 B/op	       1 allocs/op
BenchmarkAppendMsg-4                	74023837	        16.10 ns/op	1988.00 MB/s	       0 B/op	       0 allocs/op
Benchmark_Ctx_Get/header-8-4        	 1000000	         0.49 ns/op
PASS
pkg: github.com/gofiber/fiber/v3/binder
Benchmark_NewError-4                	 1000000	      1118 ns/op	     160 B/op	       8 allocs/op
PASS
EOF

# ---------- legacy and v2 build the identical page model ----------
if command -v node > /dev/null 2>&1; then
  # the first script block of index.html is the DOM-free logic with module.exports
  awk '/<script>/ { if (!done) { collect = 1; next } } /<\/script>/ { if (collect) { collect = 0; done = 1 } } collect' \
    "$PAGES_DIR/index.html" > "$SANDBOX/logic.js"
  check "index.html exports its logic for node" "yes" \
    "$(grep -q "module.exports" "$SANDBOX/logic.js" && echo yes || echo no)"

  cp "$SANDBOX/legacy-data.js" "$SANDBOX/v2-data.js"
  python3 "$PAGES_DIR/publish.py" --data "$SANDBOX/v2-data.js" --convert > /dev/null

  cat > "$SANDBOX/compare.js" <<'EOF'
const fs = require('fs');
const [logicPath, legacyPath, v2Path] = process.argv.slice(2);
const logic = require(logicPath);
function plain(v) {
  if (v instanceof Map) {
    const o = {};
    for (const k of Array.from(v.keys()).sort()) o[k] = plain(v.get(k));
    return o;
  }
  if (Array.isArray(v)) return v.map(plain);
  if (v && typeof v === 'object') {
    const o = {};
    for (const k of Object.keys(v).sort()) o[k] = plain(v[k]);
    return o;
  }
  return v;
}
function modelOf(path) {
  const data = logic.parseDataJs(fs.readFileSync(path, 'utf8'));
  return plain(logic.buildModel([{ label: null, data }]));
}
const legacy = modelOf(legacyPath);
const v2 = modelOf(v2Path);
console.log('models-equal=' + (JSON.stringify(legacy) === JSON.stringify(v2)));
console.log('series=' + legacy.totalBenches + '/' + v2.totalBenches);
console.log('runs=' + legacy.totalRuns + '/' + v2.totalRuns);
console.log('metrics=' + legacy.metrics.join(',') );
console.log('v2-smaller=' + (fs.statSync(v2Path).size < fs.statSync(legacyPath).size));
EOF
  result="$(node "$SANDBOX/compare.js" "$SANDBOX/logic.js" "$SANDBOX/legacy-data.js" "$SANDBOX/v2-data.js")"
  check "legacy and v2 render the same model" "models-equal=true" "$(grep models-equal <<< "$result")"
  check "no series lost in conversion" "series=3/3" "$(grep series= <<< "$result")"
  check "no runs lost in conversion" "runs=2/2" "$(grep runs= <<< "$result")"
  check "all metrics survive" "metrics=ns/op,B/op,allocs/op,MB/s" "$(grep metrics= <<< "$result")"
  check "the v2 file is smaller" "v2-smaller=true" "$(grep v2-smaller <<< "$result")"
else
  echo "skip node model comparison, node is not installed"
fi

# ---------- sync.sh publishes and syncs as one commit ----------
git_q() { git -c user.email=t@t -c user.name=t "$@" > /dev/null 2>&1; }

ORIGIN="$SANDBOX/origin.git"
git init -q --bare "$ORIGIN"
SEED="$SANDBOX/seed"
git init -q "$SEED"
(
  cd "$SEED"
  git_q checkout --orphan gh-pages
  mkdir benchmarks
  cp "$SANDBOX/legacy-data.js" benchmarks/data.js
  git_q add -A
  git_q commit -m "seed"
  git_q push "$ORIGIN" gh-pages
)

REPO="$SANDBOX/repo"
git init -q "$REPO"
(
  cd "$REPO"
  echo code > main.go
  git_q add -A
  git_q commit -m "feat: benchmark run"
)
SHA="$(git -C "$REPO" log -1 --format=%H)"

PAGES="$SANDBOX/pages"
git clone -q "$ORIGIN" "$PAGES" 2> /dev/null
git -C "$PAGES" checkout -q gh-pages

(
  cd "$REPO"
  DATA_DIR=benchmarks OUTPUT_FILE="$SANDBOX/output.txt" MAX_ITEMS=5 \
    FORCE_PACKAGE_SUFFIX=true SYNC_PAGE=true CPU_MODEL="Ampere-1a (GOMAXPROCS=4)" \
    GITHUB_SERVER_URL=https://example.test GITHUB_REPOSITORY=gofiber/fiber \
    bash "$PAGES_DIR/sync.sh" "$PAGES" > "$SANDBOX/sync.log" 2>&1
) || { echo "FAIL sync.sh exited non-zero"; cat "$SANDBOX/sync.log"; fails=$((fails + 1)); }

check "one commit carries data and page" "Update benchmark data for ${SHA:0:7}" \
  "$(git -C "$ORIGIN" log gh-pages -1 --format=%s)"
PUBLISHED="$(git -C "$ORIGIN" show gh-pages:benchmarks/data.js)"
check "the published data is v2" "yes" \
  "$(grep -q '"version":2' <<< "$PUBLISHED" && echo yes || echo no)"
check "the new run is appended" "yes" \
  "$(grep -q "$SHA" <<< "$PUBLISHED" && echo yes || echo no)"
check "history survives the conversion" "yes" \
  "$(grep -q "aaaaaaa" <<< "$PUBLISHED" && echo yes || echo no)"
check "the run records its CPU" "yes" \
  "$(grep -q '"cpu":"Ampere-1a (GOMAXPROCS=4)"' <<< "$PUBLISHED" && echo yes || echo no)"
check "the page is deployed with its layout baked" "yes" \
  "$(git -C "$ORIGIN" show gh-pages:benchmarks/index.html | grep -q 'data-layout="single"' && echo yes || echo no)"
check "the root redirect exists" "yes" \
  "$(git -C "$ORIGIN" show gh-pages:index.html | grep -q 'gofiber-benchmark-redirect' && echo yes || echo no)"

# a data-only leg must not touch the page
git -C "$PAGES" pull -q --rebase > /dev/null 2>&1
(
  cd "$REPO"
  echo more >> main.go
  git_q add -A
  git_q commit -m "feat: second run"
  DATA_DIR=benchmarks OUTPUT_FILE="$SANDBOX/output.txt" MAX_ITEMS=5 \
    FORCE_PACKAGE_SUFFIX=true SYNC_PAGE=false \
    GITHUB_SERVER_URL=https://example.test GITHUB_REPOSITORY=gofiber/fiber \
    bash "$PAGES_DIR/sync.sh" "$PAGES" > "$SANDBOX/sync2.log" 2>&1
) || { echo "FAIL second sync.sh exited non-zero"; cat "$SANDBOX/sync2.log"; fails=$((fails + 1)); }
check "a data-only run leaves the page alone" "1" \
  "$(git -C "$ORIGIN" log gh-pages --format=%s -- benchmarks/index.html | wc -l | tr -d ' ')"
check "but appends its run" "4" \
  "$(git -C "$ORIGIN" show gh-pages:benchmarks/data.js | python3 -c 'import json,sys; t=sys.stdin.read(); print(len(json.loads(t[t.find("{"):t.rfind("}")+1])["runs"]))')"

# a sync-only run restores a drifted page, and an unchanged one stays quiet
git -C "$PAGES" pull -q --rebase > /dev/null 2>&1
(
  cd "$PAGES"
  echo "<!-- drift -->" >> benchmarks/index.html
  git_q add -A
  git_q commit -m "drift"
  git_q push
)
(
  cd "$REPO"
  DATA_DIR=benchmarks SYNC_PAGE=true \
    bash "$PAGES_DIR/sync.sh" "$PAGES" > "$SANDBOX/sync3.log" 2>&1 \
  && DATA_DIR=benchmarks SYNC_PAGE=true \
    bash "$PAGES_DIR/sync.sh" "$PAGES" > "$SANDBOX/sync4.log" 2>&1
) || { echo "FAIL sync-only sync.sh exited non-zero"; cat "$SANDBOX/sync3.log" "$SANDBOX/sync4.log"; fails=$((fails + 1)); }
check "a sync-only run says so" "Sync benchmark page" \
  "$(git -C "$ORIGIN" log gh-pages -1 --format=%s)"
check "an unchanged page is not committed again" "yes" \
  "$(grep -q "already up to date" "$SANDBOX/sync4.log" && echo yes || echo no)"

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails check(s) failed"
  exit 1
fi
echo "all checks passed"
