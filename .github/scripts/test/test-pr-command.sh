#!/usr/bin/env bash
# Tests the two halves of pr-command.yml: which comments start a run, who is
# allowed to start one and what survives of the words typed after the command;
# then that everything the command changed reaches the branch, and nothing it
# was not allowed to change does. Every step is lifted out of the workflow and
# executed here, so the checks cannot drift away from what CI runs.
# Run from anywhere: bash .github/scripts/test/test-pr-command.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKFLOW="$SCRIPT_DIR/../../workflows/pr-command.yml"

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

export SANDBOX
SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

python3 -c 'import yaml' 2>/dev/null || python3 -m pip install --quiet pyyaml
python3 - "$WORKFLOW" "$SANDBOX" <<'PYEOF'
import os, sys, yaml

workflow = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))


def dump(job, step_id, name):
    run = next(s for s in workflow['jobs'][job]['steps'] if s.get('id') == step_id)['run']
    open(os.path.join(sys.argv[2], name), 'w', encoding='utf-8').write(run)


dump('check', 'parse', 'parse.sh')
dump('run', 'diff', 'diff.sh')
dump('push', 'push', 'push.sh')
dump('report', 'comment', 'report.sh')
PYEOF

# gh never leaves the sandbox: the pull request metadata and the caller's
# permission are fixtures, and the reaction calls are only recorded.
export PR_JSON="$SANDBOX/pr.json"
export REACTIONS="$SANDBOX/reactions.txt"
export PERMISSION=write
cat > "$PR_JSON" <<'EOF'
{"head": {"repo": {"full_name": "contributor/fiber"}, "ref": "feature/x", "sha": "cafe1234"}}
EOF

mkdir -p "$SANDBOX/bin"
cat > "$SANDBOX/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *reactions*)   printf '%s\n' "$*" >> "$REACTIONS" ;;
  */permission*) [ "$PERMISSION" != "apierror" ] && printf '%s\n' "$PERMISSION" || exit 1 ;;
  "pr comment"*) printf '%s' "${*#*--body }" > "$SANDBOX/comment.md" ;;
  *)             cat "$PR_JSON" ;;
esac
EOF
chmod +x "$SANDBOX/bin/gh"
PATH="$SANDBOX/bin:$PATH"

status=0
parse() { # $1 command, $2 permission of the commenter, $3 comment body, $4 paths
  : > "$SANDBOX/out.txt"
  : > "$REACTIONS"
  : > "$SANDBOX/comment.md"
  PERMISSION="$2" GITHUB_OUTPUT="$SANDBOX/out.txt" GH_TOKEN=stub REPO=gofiber/fiber PR=42 \
    COMMENT_ID=7 COMMAND="$1" ACTOR=someone COMMENT_BODY="$3" PATHS="${4-.}" \
    bash "$SANDBOX/parse.sh" > "$SANDBOX/log.txt" 2>&1
  status=$?
}

out() { grep -E "^$1=" "$SANDBOX/out.txt" | tail -1 | cut -d= -f2-; }
reactions() { tr '\n' ' ' < "$REACTIONS" | grep -oE 'content=[a-z]+' | cut -d= -f2 | tr '\n' ' ' | sed 's/ $//'; }

echo "--- what counts as the command"
parse generate write '/generate'
check "bare command" "true" "$(out match)"
check "head repo comes from the pull request" "contributor/fiber" "$(out head-repo)"
check "head ref comes from the pull request" "feature/x" "$(out head-ref)"
check "head sha comes from the pull request" "cafe1234" "$(out head-sha)"
check "a head on another repo is a fork" "true" "$(out fork)"
check "an accepted command is acknowledged" "eyes" "$(reactions)"

printf '{"head": {"repo": {"full_name": "gofiber/fiber"}, "ref": "main-branch", "sha": "cafe1234"}}\n' > "$PR_JSON"
parse generate write '/generate'
check "a head on the repo itself is not a fork" "false" "$(out fork)"
printf '{"head": {"repo": {"full_name": "contributor/fiber"}, "ref": "feature/x", "sha": "cafe1234"}}\n' > "$PR_JSON"

parse generate write 'Looks good, I will run /generate later'
check "mid-sentence mention" "false" "$(out match)"

parse generate write '/generate-all'
check "longer command sharing the prefix" "false" "$(out match)"

parse bench-readme write '/bench-readme-amd64 -block 2'
check "sibling command with a suffix" "false" "$(out match)"

parse bench-readme-amd64 write '/bench-readme-amd64 -block 2'
check "the sibling matches itself" "true" "$(out match)"

parse generate write 'please:

/generate

thanks'
check "command on its own line further down" "true" "$(out match)"

parse generate write "$(printf '/generate\r')"
check "carriage returns from the web editor" "true" "$(out match)"

parse generate write '   /generate'
check "indented command" "true" "$(out match)"

parse generate write '/generate;rm -rf /'
check "the command needs a space or a line end after it" "false" "$(out match)"

echo "--- who may start a run"
for perm in admin maintain write; do
  parse generate "$perm" '/generate'
  check "$perm may run it" "true" "$(out match)"
done
# author_association would call all three of these COLLABORATOR or MEMBER.
for perm in triage read none; do
  parse generate "$perm" '/generate'
  check "$perm may not run it" "false" "$(out match)"
  check "$perm gets a reaction instead of silence" "confused" "$(reactions)"
  check "$perm is not told why in public" "0" "$(wc -c < "$SANDBOX/comment.md" | tr -d ' ')"
done
check "the refusal names the permission in the log" "1" "$(grep -c "is 'none'" "$SANDBOX/log.txt")"

# A lookup that cannot answer would otherwise take every command down in silence.
parse generate apierror '/generate'
check "a failed lookup fails the job" "1" "$status"

echo "--- the words after the command"
parse generate write '/generate -block 2'
check "flags are kept" "-block 2" "$(out args)"

parse generate write '/generate   -block   2   '
check "surrounding whitespace is trimmed" "-block   2" "$(out args)"

parse generate write '/generate'
check "no arguments is empty" "" "$(out args)"

parse generate write '/generate --tag v1.2.3-rc.1,v2 ./pkg'
check "versions, lists and paths survive" "--tag v1.2.3-rc.1,v2 ./pkg" "$(out args)"

# shellcheck disable=SC2016 # the shell syntax has to reach the parser unexpanded
parse generate write '/generate ; rm -rf / $(id) `id` && echo | tee'
check "shell syntax is refused, not stripped" "false" "$(out match)"
check "and the refusal is visible" "confused" "$(reactions)"
check "and a maintainer is told why" "1" "$(grep -c 'was not run' "$SANDBOX/comment.md")"

echo "--- caller mistakes"
parse 'Generate' write '/Generate'
check "an uppercase command input is refused" "1" "$status"

parse 'gen.*' write '/generate'
check "a regex as command input is refused" "1" "$status"

# An empty pathspec list matches everything and would void the push job's re-check.
parse generate write '/generate' ''
check "an empty paths input is refused" "1" "$status"

printf '{"head": {"repo": null, "ref": "feature/x", "sha": "cafe1234"}}\n' > "$PR_JSON"
parse generate write '/generate'
check "a deleted fork stops the run" "1" "$status"

# --- the other half: what the command changed has to survive the handoff from
# the job that ran it (read-only, no credentials) to the job that pushes it.
export RUNNER_TEMP="$SANDBOX/tmp"
WORK="$SANDBOX/work"
PUSHED="$SANDBOX/pushed"

setup_repo() {
  rm -rf "$SANDBOX/origin.git" "$WORK" "$PUSHED" "$RUNNER_TEMP"
  mkdir -p "$RUNNER_TEMP"
  git init -q --bare "$SANDBOX/origin.git"
  git clone -q "$SANDBOX/origin.git" "$WORK" 2>/dev/null
  git -C "$WORK" config user.email ci@example.com
  git -C "$WORK" config user.name ci
  mkdir -p "$WORK/sub"
  printf 'one\n' > "$WORK/tracked.txt"
  printf 'del\n' > "$WORK/gone.txt"
  printf 'x\n' > "$WORK/sub/nested.txt"
  printf '*.log\n' > "$WORK/.gitignore"
  git -C "$WORK" add -A
  git -C "$WORK" commit -qm init
  git -C "$WORK" push -q origin HEAD:refs/heads/feature
}

touch_everything() {
  printf 'one\ntwo\n' > "$WORK/tracked.txt"       # modified
  printf 'new\n' > "$WORK/created.go"             # added
  rm "$WORK/gone.txt"                             # deleted
  chmod +x "$WORK/sub/nested.txt"                 # mode change
  printf '\x00\x01binary\n' > "$WORK/blob.bin"    # added, binary
  printf 'noise\n' > "$WORK/debug.log"            # gitignored
}

collect() { # $1 pathspec
  : > "$SANDBOX/out.txt"
  ( cd "$WORK" && PATHS="$1" GITHUB_OUTPUT="$SANDBOX/out.txt" \
      GITHUB_STEP_SUMMARY="$SANDBOX/summary.md" bash "$SANDBOX/diff.sh" ) > "$SANDBOX/log.txt" 2>&1
  status=$?
}

apply_and_push() { # $1 pathspec the push job enforces
  git clone -q "$SANDBOX/origin.git" "$PUSHED" 2>/dev/null
  git -C "$PUSHED" checkout -q "$(git -C "$WORK" rev-parse HEAD)"
  : > "$SANDBOX/out.txt"
  ( cd "$PUSHED" && COMMAND=test MESSAGE='chore(ci): apply /test' PATHS="$1" \
      HEAD_REPO=owner/repo HEAD_REF=feature FORK=false HAS_TOKEN=false \
      GITHUB_OUTPUT="$SANDBOX/out.txt" bash "$SANDBOX/push.sh" ) > "$SANDBOX/log.txt" 2>&1
  status=$?
}

echo "--- collecting what the command changed"
setup_repo
touch_everything
collect .
check "changes are detected" "true" "$(out changed)"
check "the patch is written" "1" "$([ -s "$RUNNER_TEMP/pr-command.patch" ] && echo 1 || echo 0)"

apply_and_push .
check "the push reports success" "true" "$(out pushed)"
check "the commit lands on the branch" "chore(ci): apply /test" \
  "$(git -C "$SANDBOX/origin.git" log -1 --format=%s refs/heads/feature)"
tree=$(git -C "$PUSHED" show --name-status --format= HEAD | sort | tr '\t' ' ' | tr '\n' ',')
check "modified, added and deleted all arrive" \
  "A blob.bin,A created.go,D gone.txt,M sub/nested.txt,M tracked.txt," "$tree"
check "a mode change arrives" "100755" "$(git -C "$PUSHED" ls-tree HEAD sub/nested.txt | awk '{print $1}')"
check "the binary file arrives whole" "9" \
  "$(git -C "$PUSHED" cat-file -s "$(git -C "$PUSHED" rev-parse HEAD:blob.bin)")"
check "a gitignored file stays out" "0" "$(git -C "$PUSHED" ls-tree HEAD debug.log | wc -l | tr -d ' ')"

echo "--- paths confine what gets committed"
setup_repo
touch_everything
collect tracked.txt
check "only the listed path is collected" "diff --git a/tracked.txt b/tracked.txt" \
  "$(grep '^diff --git' "$RUNNER_TEMP/pr-command.patch")"

# Several globs, matched recursively by git alone and left untouched by the shell.
setup_repo
mkdir -p "$WORK/middleware/cache"
printf 'gen\n' > "$WORK/root_msgp.go"
printf 'gen\n' > "$WORK/middleware/cache/manager_msgp.go"
printf 'gen\n' > "$WORK/other.go"
collect '*_msgp.go *_interface_gen.go'
check "a glob pathspec reaches into subdirectories" \
  "diff --git a/middleware/cache/manager_msgp.go b/middleware/cache/manager_msgp.go,diff --git a/root_msgp.go b/root_msgp.go," \
  "$(grep '^diff --git' "$RUNNER_TEMP/pr-command.patch" | sort | tr '\n' ',')"

collect .
apply_and_push '*_msgp.go *_interface_gen.go'
check "and the push refuses a patch that reaches past those globs" "1" "$status"
check "with nothing pushed" "false" "$(out pushed)"

# Rename detection prints only the destination, so without --no-renames a file
# dragged in from outside the globs would match on both sides of the check.
setup_repo
git -C "$WORK" mv gone.txt renamed_msgp.go
collect .
apply_and_push '*_msgp.go'
check "a rename out of the allowed paths is refused" "1" "$status"
check "and the file it came from survives" "gone.txt" \
  "$(git -C "$SANDBOX/origin.git" ls-tree --name-only refs/heads/feature gone.txt)"

# The job that builds the patch is the one that ran pull request code, so the
# push job has to reject a patch reaching past the paths on its own.
setup_repo
touch_everything
collect .
apply_and_push tracked.txt
check "a patch past the allowed paths is refused" "1" "$status"
check "and nothing is pushed" "false" "$(out pushed)"
check "the branch is untouched" "init" \
  "$(git -C "$SANDBOX/origin.git" log -1 --format=%s refs/heads/feature)"

echo "--- a command that changes nothing"
setup_repo
collect .
check "nothing to commit is reported" "false" "$(out changed)"
check "no patch is left behind" "0" "$([ -s "$RUNNER_TEMP/pr-command.patch" ] && echo 1 || echo 0)"

# --- the result comment is the only feedback the commenter gets, so every
# outcome has to reach the right one of them.
comment() { # $1..$n as KEY=VALUE overrides of the report environment
  : > "$SANDBOX/comment.md"
  env -i PATH="$PATH" HOME="$HOME" REACTIONS="$REACTIONS" PR_JSON="$PR_JSON" \
    SANDBOX="$SANDBOX" GH_TOKEN=stub REPO=gofiber/fiber PR=42 COMMAND=generate \
    RUN_URL=https://example/run CHECK_RESULT=success RESULT=success CHANGED=true \
    PUSHED=true SHA=abc1234 REASON= HEAD_REF=feature/x FORK=false HAS_TOKEN=true \
    "$@" bash "$SANDBOX/report.sh" > "$SANDBOX/log.txt" 2>&1
  cat "$SANDBOX/comment.md"
}

has() { case "$2" in *"$1"*) echo yes ;; *) echo no ;; esac; }

echo "--- the result comment"
check "a failed check job is reported" "yes" "$(has 'could not start' "$(comment CHECK_RESULT=failure)")"
check "a failed command is reported" "yes" "$(has 'generate` failed' "$(comment RESULT=failure)")"
check "no changes is reported" "yes" "$(has 'nothing changed' "$(comment CHANGED=false)")"

body=$(comment)
# shellcheck disable=SC2016 # the backticks are markdown in the comment body
check "a push names the commit and branch" "yes" "$(has 'pushed `abc1234` to `feature/x`' "$body")"
check "with a push-token no re-run note is added" "no" "$(has 'starts no workflow run' "$body")"
check "without one the re-run note is added" "yes" "$(has 'starts no workflow run' "$(comment HAS_TOKEN=false)")"

body=$(comment PUSHED=false REASON=push FORK=true HAS_TOKEN=false)
check "a fork that cannot be pushed to points at the artifact" "yes" "$(has 'pr-command-generate-patch' "$body")"
check "and names the missing secret" "yes" "$(has 'push-token' "$body")"
check "a same-repo push failure does not blame the secret" "no" \
  "$(has 'push-token' "$(comment PUSHED=false REASON=push FORK=false HAS_TOKEN=false)")"
# Telling a maintainer to apply that patch by hand would walk straight past the guard.
body=$(comment PUSHED=false REASON=paths)
check "a path refusal is named as such" "yes" "$(has 'outside the paths' "$body")"
check "and does not offer the patch" "no" "$(has 'git apply' "$body")"
# The patch only passed the paths check in the REASON=push state.
check "a patch that would not apply is not offered either" "no" \
  "$(has 'git apply' "$(comment PUSHED=false REASON=apply)")"
check "only a failed push offers it" "yes" \
  "$(has 'git apply' "$(comment PUSHED=false REASON=push)")"

check "a branch name cannot break out of the code span" "no" \
  "$(has '](https://evil' "$(comment 'HEAD_REF=x`](https://evil.example)')")"

# --- pr-command-hint: the author's description is data, and only ever appended to.
python3 - "$SCRIPT_DIR/../../workflows/pr-command-hint.yml" "$SANDBOX/hint.sh" <<'PYEOF'
import sys, yaml

workflow = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
step = next(s for s in workflow['jobs']['hint']['steps'] if s.get('id') == 'append')
open(sys.argv[2], 'w', encoding='utf-8').write(step['run'])
PYEOF

cat > "$SANDBOX/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  "pr view"*) cat "$SANDBOX/body.in" ;;
  "pr edit"*) cp "${@: -1}" "$SANDBOX/body.out" ;; # the --body-file path
esac
EOF

hint() { # $1 the description the pull request was opened with
  printf '%s' "$1" > "$SANDBOX/body.in"
  rm -f "$SANDBOX/body.out"
  ( cd "$SANDBOX" && GH_TOKEN=stub REPO=gofiber/fiber PR=42 HINT='`/generate` re-runs the generators' \
      bash "$SANDBOX/hint.sh" ) > "$SANDBOX/log.txt" 2>&1
}
edited() { grep -qF -- "$1" "$SANDBOX/body.out" 2>/dev/null && echo yes || echo no; }

echo "--- the hint in the description"
hint 'Fixes #1'
check "the description is kept" "yes" "$(edited 'Fixes #1')"
# shellcheck disable=SC2016 # the backticks are markdown in the description
check "the hint is appended" "yes" "$(edited '<sub>🤖 Maintainers: `/generate` re-runs the generators</sub>')"

# shellcheck disable=SC2016 # the description has to reach the step unexpanded
hint 'run $(touch pwned) and `touch pwned` "quoted"'
check "a description is data, never code" "no" "$([ -e "$SANDBOX/pwned" ] && echo yes || echo no)"
# shellcheck disable=SC2016 # same text as above, compared literally
check "and is written back byte for byte" "yes" "$(edited 'run $(touch pwned) and `touch pwned` "quoted"')"

hint "$(printf 'text\n\n<!-- pr-command-hint -->\n<sub>earlier</sub>')"
check "a second run leaves the description alone" "no" "$([ -e "$SANDBOX/body.out" ] && echo yes || echo no)"

hint ''
check "an empty description still gets the hint" "yes" "$(edited 'Maintainers:')"

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails check(s) failed"
  exit 1
fi
echo "all checks passed"
