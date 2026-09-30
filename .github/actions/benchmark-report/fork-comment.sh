#!/usr/bin/env bash
# Posts the reports a fork PR's benchmark run staged as artifacts. That run was
# fork-controlled, so the artifacts are data only: the PR comes from the trusted
# workflow_run event, every field is validated, the report is only ever a body.
# env: GH_TOKEN REPO RUN_ID HEAD_REPO HEAD_BRANCH HEAD_SHA
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# a head that moved on is not an error: the newer run reports for itself
pr="$(gh api -X GET "repos/${REPO}/pulls" -f state=open -f head="${HEAD_REPO%%/*}:${HEAD_BRANCH}" --paginate \
  | jq -rs --arg sha "$HEAD_SHA" --arg repo "$HEAD_REPO" \
    '[.[][] | select(.head.sha == $sha and .head.repo.full_name == $repo)] | .[0].number // empty')"
if [[ -z "$pr" ]]; then
  echo "no open PR has ${HEAD_REPO}@${HEAD_SHA} as its head, nothing to post"
  exit 0
fi

artifacts="$(gh api "repos/${REPO}/actions/runs/${RUN_ID}/artifacts" --paginate --jq '.artifacts[].name')"
names="$(printf '%s\n' "$artifacts" | grep -E '^benchmark-comment-[A-Za-z0-9._-]{1,100}$' | head -n 100 || true)"
if [[ -z "$names" ]]; then
  echo "run ${RUN_ID} staged no benchmark report"
  exit 0
fi

field() { # first line of a regular file, symlinks refused
  [[ -f "$1" && ! -L "$1" ]] && head -c 256 "$1" | head -n 1
}

# runs in an || context where set -e is off, hence the explicit exits
post() {
  local art="$1" work wd significant=false changed=false
  wd="$(field "$art/working-directory")" || wd=""
  # the marker lands in a jq filter and in the comment, so no quotes or '>'
  if [[ ! "$wd" =~ ^[A-Za-z0-9._/-]{1,200}$ ]]; then
    echo "::warning::refused a staged report: bad working-directory"
    return 0
  fi
  if [[ ! -f "$art/report.md" || -L "$art/report.md" ]] \
    || [[ "$(wc -c < "$art/report.md")" -gt 60000 ]]; then
    echo "::warning::refused the staged report for ${wd}: missing, a symlink or too large"
    return 0
  fi
  # anything but a literal true is false
  [[ "$(field "$art/significant")" == "true" ]] && significant=true
  [[ "$(field "$art/changed")" == "true" ]] && changed=true
  work="$(mktemp -d)" && cp "$art/report.md" "$work/report.md" || return 1
  (
    cd "$work" || exit 1
    export PR="$pr" SHA="$HEAD_SHA" MARKER="<!-- benchmark-report:${wd} -->"
    GITHUB_OUTPUT="$work/found" bash "$HERE/find-report.sh" || exit 1
    SIGNIFICANT="$significant" CHANGED="$changed" \
      POSTED="$(sed -n 's/^id=//p' found)" BURIED="$(sed -n 's/^buried=//p' found)" \
      bash "$HERE/post-report.sh"
  )
}

# one module failing must not cost the others their report, but the run goes red
failed=0
while IFS= read -r name; do
  art="$(mktemp -d)"
  if ! gh run download "$RUN_ID" -R "$REPO" -n "$name" -D "$art" || ! post "$art"; then
    echo "::error::posting ${name} to #${pr} failed"
    failed=1
  fi
done <<< "$names"
exit "$failed"
