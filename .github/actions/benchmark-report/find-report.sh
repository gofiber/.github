#!/usr/bin/env bash
# Finds this module's report on the PR and whether a reply sits below it.
# env: GH_TOKEN REPO PR MARKER; writes id/buried to $GITHUB_OUTPUT, the body to previous.md
set -euo pipefail
# id, when it was posted, is it this module's report, is it any module's report
rows="$(gh api "repos/${REPO}/issues/${PR}/comments" --paginate \
  --jq ".[] | [.id, .created_at, (.body | startswith(\"${MARKER}\") | tostring), (.body | startswith(\"<!-- benchmark-report:\") | tostring)] | @tsv")"
id=""
posted_at=""
buried=false
while IFS=$'\t' read -r comment created mine report; do
  if [[ "$mine" == "true" ]]; then
    id="$comment"
    posted_at="$created"
    buried=false
  elif [[ -n "$id" && "$report" != "true" ]]; then
    # only foreign comments bury a report, the sibling modules' ones do not
    buried=true
  fi
done <<< "$rows"
# reviews are not in the issues endpoint, yet in the thread they sit below
# the report just the same; a single inline comment is one too (bodyless,
# state COMMENTED), so this covers both
if [[ -n "$id" && "$buried" != "true" ]]; then
  reviewed_at="$(gh api "repos/${REPO}/pulls/${PR}/reviews" --paginate \
    --jq '.[] | .submitted_at // empty' | sort | tail -n 1)"
  if [[ "$reviewed_at" > "$posted_at" ]]; then
    buried=true
  fi
fi
{
  echo "id=${id}"
  echo "buried=${buried}"
} >> "$GITHUB_OUTPUT"
if [[ -n "$id" ]]; then
  gh api "repos/${REPO}/issues/comments/${id}" --jq .body > previous.md
fi
