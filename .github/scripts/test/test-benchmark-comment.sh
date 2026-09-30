#!/usr/bin/env bash
# Drives the comment scripts of benchmark-report against a fake `gh`: which report
# gets refreshed, when a second one is posted instead, what gets collapsed, and
# what the fork path refuses to take from a staged artifact.
# Run from anywhere: bash .github/scripts/test/test-benchmark-comment.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPTS="$SCRIPT_DIR/../../actions/benchmark-report"
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

cat > "$SANDBOX/bin/gh" <<'STUB'
#!/usr/bin/env bash
# records every call and answers the reads the scripts make
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *"issues/"*"/comments --paginate"*) cat "$GH_ROWS" ;;
  *"pulls/"*"/reviews --paginate"*) cat "$GH_REVIEWS" ;;
  *"issues/comments/"*"--jq .body"*) cat "$GH_BODY" ;;
  *"issues/comments/"*"--jq .node_id"*) echo "IC_node42" ;;
  *"/pulls -f state=open"*) cat "$GH_PULLS" ;;
  *"/artifacts --paginate"*) ls "$GH_STAGED" ;;
  "run download "*) cp -R "$GH_STAGED/$7/." "$9" ;; # run download ID -R REPO -n NAME -D DIR
esac
STUB
chmod +x "$SANDBOX/bin/gh"
PATH="$SANDBOX/bin:$PATH"

export GH_LOG="$SANDBOX/gh.log" GH_ROWS="$SANDBOX/rows.tsv" GH_BODY="$SANDBOX/body.md" \
  GH_REVIEWS="$SANDBOX/reviews.txt" GH_PULLS="$SANDBOX/pulls.json" GH_STAGED="$SANDBOX/staged"
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
      bash "$SCRIPTS/find-report.sh"
  )
  tr '\n' ' ' < "$SANDBOX/outputs"
}

post() { # SIGNIFICANT CHANGED POSTED BURIED [PR]
  : > "$GH_LOG"
  (
    cd "$WORK" || exit 1
    GH_TOKEN=x REPO=o/r PR="${5-7}" SHA=deadbeef MARKER="$MARKER" \
      SIGNIFICANT="$1" CHANGED="$2" POSTED="$3" BURIED="$4" \
      bash "$SCRIPTS/post-report.sh"
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

echo "--- a fork PR's staged report"
stage() { # NAME WORKING-DIRECTORY SIGNIFICANT
  mkdir -p "$GH_STAGED/$1"
  printf '%s\n' "the fork numbers" > "$GH_STAGED/$1/report.md"
  printf '%s\n' "$2" > "$GH_STAGED/$1/working-directory"
  printf '%s\n' "$3" > "$GH_STAGED/$1/significant"
  printf '%s\n' "true" > "$GH_STAGED/$1/changed"
}
fork_post() { # PR head sha and repo as the pulls API reports them
  printf '[{"number": 7, "head": {"sha": "%s", "repo": {"full_name": "%s"}}}]\n' "$1" "$2" > "$GH_PULLS"
  printf '1\tt1\tfalse\tfalse\n' > "$GH_ROWS"
  : > "$GH_REVIEWS"
  : > "$GH_LOG"
  GH_TOKEN=x REPO=o/r RUN_ID=99 HEAD_REPO=fork/r HEAD_BRANCH=feat HEAD_SHA=abc \
    bash "$SCRIPTS/fork-comment.sh" > /dev/null 2>&1
  grep -E -- '-X (POST|PATCH)' "$GH_LOG" | sed -E 's/.*-X (POST|PATCH) ([^ ]+).*/\1 \2/' | tr '\n' ';'
}
if command -v jq > /dev/null 2>&1; then
  rm -rf "$GH_STAGED"; stage benchmark-comment-. . true
  check "lands on the PR the event points at" "POST repos/o/r/issues/7/comments;" "$(fork_post abc fork/r)"
  check "a head that moved on posts nothing" "" "$(fork_post def fork/r)"
  check "the same branch of another fork is not it" "" "$(fork_post abc other/r)"
  rm -rf "$GH_STAGED"; stage benchmark-comment-. . TRUE
  check "anything but a literal true is false" "" "$(fork_post abc fork/r)"
  rm -rf "$GH_STAGED"; stage benchmark-comment-. . true
  ln -sf "$GH_BODY" "$GH_STAGED/benchmark-comment-./report.md"
  check "a symlinked report is refused" "" "$(fork_post abc fork/r)"
  rm -rf "$GH_STAGED"; stage benchmark-comment-a 'a"b' true; stage benchmark-comment-redis redis true
  check "a bad module is refused, the next one still posts" "POST repos/o/r/issues/7/comments;" \
    "$(fork_post abc fork/r)"
  check "the marker follows the staged module" "yes" \
    "$(grep -qF 'startswith("<!-- benchmark-report:redis -->")' "$GH_LOG" && echo yes || echo no)"
else
  echo "skip fork checks, jq is not installed"
fi

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
# the filter above is a copy, so make sure the script still asks for the same four fields
check "the script still emits the same rows" "yes" \
  "$(grep -qF '[.id, .created_at, (.body' "$SCRIPTS/find-report.sh" \
    && grep -qF 'startswith(\"<!-- benchmark-report:\") | tostring)] | @tsv' "$SCRIPTS/find-report.sh" && echo yes || echo no)"

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails check(s) failed"
  exit 1
fi
echo "all checks passed"
