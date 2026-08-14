# org-health

Detects systemic anomalies across the gofiber org and posts them to a Discord
channel. Runs from `.github/workflows/org-health.yml` in two modes:

- **scan** (every 30 minutes nominally; GitHub throttles the cron to roughly
  hourly): default-branch workflows that flipped from green to red or to a
  timeout (classified as plain failure,
  same-SHA flip, or scheduled run without new commits), workflows that cannot start
  (`startup_failure`), and workflows failing across several distinct PRs at
  once (cross-PR correlation).
- **digest** (daily, 08:15 UTC): PR/issue backlog over thresholds, PRs without
  review, dependency PRs that auto-merge never got through, issues without any
  answer, issue spikes (24h vs 14-day average), and scheduled workflows GitHub
  disabled for inactivity.

A single red build is never reported. Contributors breaking lint or tests in
their own PR is the CI doing its job; only patterns that point at broken
shared infrastructure produce findings.

## Setup

1. Create a webhook in the target Discord channel (channel settings ->
   Integrations -> Webhooks).
2. Store the URL as the org secret `DISCORD_WEBHOOK_URL_HEALTH` (the workflow
   maps it to the `DISCORD_WEBHOOK_URL` env var the tool reads).

All gofiber repos are public, so the default `GITHUB_TOKEN` is enough for the
API reads.

The scan reads up to 3 pages of default-branch runs per repo, and stops early
once a page reaches past the 7-day window. On contrib, storage and recipes a
single page covers barely two hours, so one page would hide the green run a
failure has to be compared against. Known limit: during a batch merge even 300
runs span only a few hours, so a workflow that runs rarely can miss its edge.

## Configuration

The repo list is discovered automatically: all public, non-archived repos of
the org, so new repos are covered without a config change. `excludeRepos`
removes individual repos from the discovered list; setting `repos` explicitly
skips discovery entirely. `repoOverrides` overrides individual non-zero
threshold fields per repo (fiber needs higher limits than schema).

`known-issues.json` mutes findings for known, tracked problems:

```json
[
  {
    "repo": "storage",
    "check": "cross-pr",
    "workflow": "Tests",
    "until": "2026-07-01",
    "reason": "aerospike image broken, tracked in gofiber/storage#123"
  }
]
```

`repo`, `check`, and `workflow` accept `*` or may be omitted to match
anything. `until` (YYYY-MM-DD, inclusive) is mandatory; entries without it
never match, so exceptions cannot accumulate silently. Check names:
`master-failure`, `scheduled-failure`, `same-sha-flip`, `startup-failure`,
`cross-pr`, `dead-workflow`, `pr-backlog`, `issue-backlog`, `stale-prs`,
`stuck-bot-prs`, `unanswered-issues`, `issue-spike`.

## Anti-noise behaviour

- Failure alerts fire on the green-to-red transition only. The transition is
  found by walking back over the failures, not by comparing the newest two
  runs: a batch merge lands several runs between two scans and hid 59% of the
  transitions that way. Repeats are held off by the cooldown below.
- Only transitions younger than 7 days are reported, dated by the run that
  broke the streak rather than the newest attempt. A deleted or dormant
  workflow keeps its last red run as the latest one forever and would
  otherwise re-fire whenever its state entry expires.
- GitHub's own managed jobs (dependabot updates, dependency graph,
  default-setup CodeQL) run as the `dynamic` event and are skipped. They are
  dependency problems reported in their own tab, not the repo's CI.
- Failure findings are keyed by workflow, not by run, so a workflow that flaps
  green/red alerts once per cooldown instead of once per flip.
- The backlog and review-debt checks skip the PRs that merge themselves
  (dependabot, github-actions); their count swings by ten within minutes of a
  batch merge and is not review debt. `stuck-bot-prs` counts exactly those
  instead, so both signals stay readable. PRs from any other bot (copilot,
  renovate) wait for a human like any PR and stay in the review-debt bucket.
- Every finding key has a 72h cooldown (`cooldownHours`, overridable per repo),
  persisted in `state.json` via actions/cache.
- The digest is a single message; scan alerts are batched into one message
  with at most 10 embeds.
- Dry runs do not touch the state, so they never consume a cooldown.

## Local testing

```sh
cd org-health
go test ./...
GITHUB_TOKEN=$(gh auth token) go run . --mode digest --dry-run
GITHUB_TOKEN=$(gh auth token) go run . --mode scan --dry-run
```
