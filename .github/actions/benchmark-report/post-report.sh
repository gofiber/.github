#!/usr/bin/env bash
# Posts, refreshes or supersedes the report per the comment rules in README.md.
# env: GH_TOKEN REPO PR SHA SIGNIFICANT CHANGED POSTED BURIED MARKER; reads report.md
set -euo pipefail
{ printf '%s\n\n' "$MARKER"; cat report.md; } > comment.md
if [[ -z "$PR" ]]; then
  gh api -X POST "repos/${REPO}/commits/${SHA}/comments" -F body=@comment.md > /dev/null
  exit 0
fi
if [[ -z "$POSTED" ]]; then
  if [[ "$SIGNIFICANT" == "true" ]]; then
    gh api -X POST "repos/${REPO}/issues/${PR}/comments" -F body=@comment.md > /dev/null
  fi
  exit 0
fi
# the posted report already says "nothing to see here" and still holds
if [[ "$CHANGED" != "true" && "$SIGNIFICANT" != "true" ]]; then
  exit 0
fi
# editing sends no notification, so the numbers stay current for free, but
# only while the report is still the last word in the thread
if [[ "$BURIED" != "true" ]]; then
  gh api -X PATCH "repos/${REPO}/issues/comments/${POSTED}" -F body=@comment.md > /dev/null
  exit 0
fi
# a buried report is what the thread below it replied to: never rewritten,
# only superseded by a new comment once the findings moved
if [[ "$CHANGED" != "true" ]]; then
  exit 0
fi
gh api -X POST "repos/${REPO}/issues/${PR}/comments" -F body=@comment.md > /dev/null
# the superseded report is hidden, not rewritten: its body (and marker) stay
# intact, GitHub folds it away and labels it outdated
node="$(gh api "repos/${REPO}/issues/comments/${POSTED}" --jq .node_id)"
# shellcheck disable=SC2016 # $id is a GraphQL variable, not a shell one
gh api graphql \
  -f query='mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { minimizedComment { isMinimized } } }' \
  -f id="$node" > /dev/null
