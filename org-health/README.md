# org-health

Detects systemic anomalies across the gofiber org and posts them to a Discord
channel. Runs from `.github/workflows/org-health.yml` in two modes:

- **scan** (every 30 minutes): default-branch workflows that flipped from
  green to red (edge-triggered, classified as plain failure, same-SHA flip,
  or scheduled run without new commits), workflows that cannot start
  (`startup_failure`), and workflows failing across several distinct PRs at
  once (cross-PR correlation).
- **digest** (daily, 08:00 UTC): PR/issue backlog over thresholds, PRs without
  review, issues without any answer, issue spikes (24h vs 14-day average),
  and scheduled workflows GitHub disabled for inactivity.

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
`unanswered-issues`, `issue-spike`.

## Anti-noise behaviour

- Failure alerts fire on the green-to-red transition only; an already-red
  workflow stays silent.
- Every finding key has a 72h cooldown (`cooldownHours`), persisted in
  `state.json` via actions/cache.
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
