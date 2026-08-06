#!/usr/bin/env bash
# Drives the two comment steps of benchmark-report/action.yml against a fake `gh`:
# which report gets refreshed, when a second one is posted instead, and what gets
# collapsed. The steps are extracted from the action itself, so this cannot drift
# from what actually ships.
# Run from anywhere: bash .github/scripts/test/test-benchmark-comment.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ACTION="$SCRIPT_DIR/../../actions/benchmark-report/action.yml"
MARKER='<!-- benchmark-report:. -->'

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
WORK="$SANDBOX/work"
mkdir -p "$WORK" "$SANDBOX/bin"

extract() { # the run: block of a composite step, dedented
  awk -v step="    - name: $1" '
    $0 == step { found = 1; next }
    found && /^      run: \|$/ { body = 1; next }
    body { if ($0 ~ /^        / || $0 == "") { sub(/^        /, ""); print; next } else exit }
  ' "$ACTION"
}
extract "Find Posted Report" > "$SANDBOX/find.sh"
extract "Post Benchmark Report" > "$SANDBOX/post.sh"
if [ ! -s "$SANDBOX/find.sh" ] || [ ! -s "$SANDBOX/post.sh" ]; then
  echo "could not extract the comment steps from $ACTION"
  exit 1
fi

cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
# records every call and answers the three reads the steps make
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *"issues/"*"/comments --paginate"*) cat "$GH_ROWS" ;;
  *"pulls/"*"/reviews --paginate"*) cat "$GH_REVIEWS" ;;
  *"issues/comments/"*"--jq .body"*) cat "$GH_BODY" ;;
  *"issues/comments/"*"--jq .node_id"*) echo "IC_node42" ;;
esac
STUB
chmod +x "$SANDBOX/bin/gh"
PATH="$SANDBOX/bin:$PATH"

export GH_LOG="$SANDBOX/gh.log" GH_ROWS="$SANDBOX/rows.tsv" GH_BODY="$SANDBOX/body.md" \
  GH_REVIEWS="$SANDBOX/reviews.txt"
printf '%s\n\n%s\n' "$MARKER" "the old numbers" > "$GH_BODY"
printf '%s\n' "the new numbers" > "$WORK/report.md"

# timestamps are fake but ordered, t1 < t2 < t3: everything here compares as strings
find_report() { # rows of "id<TAB>posted<TAB>mine<TAB>any benchmark report", plus review stamps
  printf '%s\n' "$1" > "$GH_ROWS"
  printf '%s\n' "${2-}" > "$GH_REVIEWS"
  : > "$GH_LOG"
  : > "$SANDBOX/outputs"
  (
    cd "$WORK" || exit 1
    GITHUB_OUTPUT="$SANDBOX/outputs" GH_TOKEN=x REPO=o/r PR=7 MARKER="$MARKER" \
      bash "$SANDBOX/find.sh"
  )
  tr '\n' ' ' < "$SANDBOX/outputs"
}

post() { # SIGNIFICANT CHANGED POSTED BURIED [PR]
  : > "$GH_LOG"
  (
    cd "$WORK" || exit 1
    GH_TOKEN=x REPO=o/r PR="${5-7}" SHA=deadbeef MARKER="$MARKER" \
      SIGNIFICANT="$1" CHANGED="$2" POSTED="$3" BURIED="$4" \
      bash "$SANDBOX/post.sh"
  )
  grep -F -- '-X' "$GH_LOG" | sed -E 's/.*-X (POST|PATCH|DELETE) ([^ ]+).*/\1 \2/' | tr '\n' ';'
}

echo "--- finding the report already on the PR"
check "no report yet" "id= buried=false " "$(find_report "1	t1	false	false")"
check "the newest own report wins" "id=9 buried=false " \
  "$(find_report "$(printf '1\tt1\ttrue\ttrue\n9\tt2\ttrue\ttrue')")"
check "a foreign comment buries it" "id=1 buried=true " \
  "$(find_report "$(printf '1\tt1\ttrue\ttrue\n2\tt2\tfalse\tfalse')")"
# storage posts one report per package, they must not bury each other every run
check "a sibling module does not bury it" "id=1 buried=false " \
  "$(find_report "$(printf '1\tt1\ttrue\ttrue\n2\tt2\tfalse\ttrue')")"
check "chatter before the report does not count" "id=9 buried=false " \
  "$(find_report "$(printf '1\tt1\tfalse\tfalse\n9\tt2\ttrue\ttrue')")"
check "reposting resets the burial" "id=9 buried=false " \
  "$(find_report "$(printf '1\tt1\ttrue\ttrue\n2\tt2\tfalse\tfalse\n9\tt3\ttrue\ttrue')")"
# a review is a reply too, it just does not live in the issues endpoint
check "a review buries it" "id=1 buried=true " "$(find_report "1	t1	true	true" "t2")"
check "a review before the report does not" "id=1 buried=false " \
  "$(find_report "1	t2	true	true" "t1")"
check "the newest review decides" "id=1 buried=true " \
  "$(find_report "1	t2	true	true" "$(printf 't1\nt3')")"

echo "--- what lands on the PR"
check "the first finding is posted" "POST repos/o/r/issues/7/comments;" "$(post true true '' false)"
check "nothing to say, nothing posted" "" "$(post false true '' false)"
# the point of the whole exercise: a commit that moved nothing must not comment again
check "unchanged findings only refresh" "PATCH repos/o/r/issues/comments/42;" \
  "$(post true false 42 false)"
check "a buried report with unchanged findings is left alone" "" \
  "$(post true false 42 true)"
check "changed findings refresh while the report is last" "PATCH repos/o/r/issues/comments/42;" \
  "$(post true true 42 false)"
check "changed findings behind chatter get a fresh comment" \
  "POST repos/o/r/issues/7/comments;" "$(post true true 42 true)"
check "a fixed regression is cleared in place while last" "PATCH repos/o/r/issues/comments/42;" \
  "$(post false true 42 false)"
check "a buried regression gets a fresh all-clear comment" "POST repos/o/r/issues/7/comments;" \
  "$(post false true 42 true)"
check "an already clean report stays untouched" "" "$(post false false 42 true)"
check "a push comments on the commit" "POST repos/o/r/commits/deadbeef/comments;" \
  "$(post true true '' false '')"

echo "--- the superseded report"
post true true 42 true > /dev/null
check "is hidden as outdated, not rewritten" "yes" \
  "$(grep -q 'graphql.*minimizeComment.*classifier: OUTDATED' "$GH_LOG" && echo yes || echo no)"
check "the hide targets the old comment's node" "yes" \
  "$(grep -q 'graphql.*-f id=IC_node42' "$GH_LOG" && echo yes || echo no)"
check "the node is looked up from the old comment" "yes" \
  "$(grep -q 'repos/o/r/issues/comments/42 --jq .node_id' "$GH_LOG" && echo yes || echo no)"
check "the replacement is posted before the old one is hidden" "yes" \
  "$([ "$(grep -nF -- '-X POST' "$GH_LOG" | head -1 | cut -d: -f1)" \
    -lt "$(grep -n 'graphql' "$GH_LOG" | head -1 | cut -d: -f1)" ] && echo yes || echo no)"
check "no PATCH touches the old body" "no" \
  "$(grep -qF -- '-X PATCH' "$GH_LOG" && echo yes || echo no)"
check "refreshing in place hides nothing" "no" \
  "$(post true false 42 true > /dev/null; grep -q graphql "$GH_LOG" && echo yes || echo no)"

echo "--- the jq filter that feeds all of this"
if command -v jq > /dev/null 2>&1; then
  cat > "$SANDBOX/comments.json" <<'EOF'
[{"id": 1, "created_at": "t1", "body": "<!-- benchmark-report:. -->\n\nmine"},
 {"id": 2, "created_at": "t2", "body": "looks good to me"},
 {"id": 3, "created_at": "t3", "body": "<!-- benchmark-report:./middleware/redis -->\n\na sibling module"}]
EOF
  check "flags mine, the siblings and the rest apart" "$(printf '1\tt1\ttrue\ttrue\n2\tt2\tfalse\tfalse\n3\tt3\tfalse\ttrue')" \
    "$(jq -r ".[] | [.id, .created_at, (.body | startswith(\"${MARKER}\") | tostring), (.body | startswith(\"<!-- benchmark-report:\") | tostring)] | @tsv" \
      "$SANDBOX/comments.json")"
else
  echo "skip jq filter check, jq is not installed"
fi
# the filter above is a copy, so make sure the action still asks for the same four fields
check "the action still emits the same rows" "yes" \
  "$(grep -qF '[.id, .created_at, (.body' "$ACTION" \
    && grep -qF 'startswith(\"<!-- benchmark-report:\") | tostring)] | @tsv' "$ACTION" && echo yes || echo no)"

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails check(s) failed"
  exit 1
fi
echo "all checks passed"
