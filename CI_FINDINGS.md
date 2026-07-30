# CI findings

Measurements and gotchas from the move to Blacksmith runners and the CI speed-ups
that followed. Numbers come from real runs, not estimates; where something is
unverified it says so. Companion to `WORKFLOW_CENTRALIZATION.md`, which describes
what the central workflows do - this one records what we learned running them.

## Rollout status of the central workflows

Caller counts read straight from the workflow files of all 34 organisation repos
(GitHub code search is not reliable here, see "How these were measured").

| Workflow | Repos calling it |
| --- | --- |
| `dependabot-automerge.yml` | 12 |
| `sync-sponsors.yml` | 11 |
| `auto-labeler.yml` | 9 |
| `dependabot-on-demand.yml` | 9 |
| `weekly-release.yml` | 7 |
| `after-release.yml` | 6 |
| `sync-docs.yml` | 5 |
| `go-lint-single.yml` | 5 |
| `benchmark.yml` | 5 |
| `go-lint-multi.yml` | 3 |
| `go-test.yml` | **0** |
| `markdown-check.yml` | **0** |
| `security-golang.yml` | **0** |

The last three exist but were never rolled out. What actually runs today are the
repo-local `test.yml`, `markdown.yml` and `vulncheck.yml`. Changes to the three uncalled
workflows are staged for a rollout, not live.

### What the `dependabot-automerge.yml` rollout took

Every active repo that has the workflow now calls the central one; `website` is the only
active repo without it, and `fiber-v2` has no such file either. Two things had to be
handled per repo, both easy to get wrong:

1. **The token.** All twelve local copies used `secrets.PR_TOKEN`, a PAT, and the callers
   still pass it. The central
   workflow falls back to `github.token`, and `gh pr review --approve` fails with it -
   the Actions bot may not approve a pull request. `secrets: inherit` does **not**
   fix this: it passes secrets under their original names, while the central workflow
   expects `github-token`. It has to be mapped explicitly.
2. **`match_pattern`.** The central default `test|discover|go` matches none of the
   repos, because their job names differ. A wrong pattern is the dangerous failure:
   the wait job finds no matching checks, skips them, and merges before CI is done -
   while everything looks green.

| Repo | `match_pattern` |
| --- | --- |
| fiber | `^unit\|^lint /` |
| docs | `build\|deploy` |
| contrib, template, utils, cli, boilerplate | `Tests\|lint` |
| storage | `Tests\|Linter` |
| schema | `unit\|lint` |
| recipes | `builds` |
| awesome-fiber | `Linter` |
| multi-labeler | `Test\|Build\|[Ll]int` |

A caller therefore looks like this, and the `permissions` block is mandatory because
a called workflow can never hold more rights than its caller:

```yaml
jobs:
  automerge:
    uses: gofiber/.github/.github/workflows/dependabot-automerge.yml@main
    with:
      match_pattern: '^unit|^lint /'
    secrets:
      github-token: ${{ secrets.PR_TOKEN }}
```

Check names change with the move: a called workflow prefixes its jobs, so
`wait_for_checks` becomes `automerge / wait_for_checks`. Safe here, because no repo
requires status checks, neither through branch protection nor through a ruleset. Worth
re-checking before a similar move, and worth checking that no `match_pattern` matches the
new names - the wait job would otherwise wait on itself.

### Major updates: everywhere except this repository

The central workflow auto-merges **major** `github_actions` updates, which the local
copies never did, except for four glue actions and except in `gofiber/.github` itself.
The asymmetry is deliberate. In the twelve leaf repos an action sits in a workflow that
runs on pull requests, so a broken major is caught by the same CI that gates the merge.
This repository's own pull request checks are `test-actions.yml` (`scripts`, `discover`,
`go`) and `test-release-gate.yml`, and they use only `actions/checkout` and
`actions/setup-go`. The twelve other actions in the reusables sit behind `workflow_call`
and never run on a pull request here, while every repo pins them `@main`, so a bad merge
would break the organisation at once.

### Why auto-merge runs go red

Two distinct errors, and only one is the base branch race the retry loop was written for.

| Error | Cause |
| --- | --- |
| `Base branch was modified` | another Dependabot PR merged while this one was merging |
| `Pull request is in unstable status` | a check outside `match_pattern` was still running; `gh pr merge --auto` merges directly once the PR looks mergeable, and GitHub rejects that while any check is pending |

The second is the more common one, and waiting for those checks is not the fix it looks
like. In storage the straggler was the 509s benchmark, which does not fit the 600s
`wait_for_checks` timeout with any margin; in recipes it was CodeQL `Analyze (go)`, not a
benchmark at all. Both finished after the merge attempt, by 106s and 179s. Enumerating
every check per repo would need maintaining forever, so the merge is retried ten times
30s apart instead: 270s spans both cases, and the loop lives in one file.

A red auto-merge run is not automatically a workflow bug. Contrib's eight failures were
real test failures, where the wait job correctly refused to merge.

## Blacksmith runners

The organisation is on the OSS free tier, which covers every runner type. Sizes are
picked per workload, not uniformly:

| Where | Label |
| --- | --- |
| Go tests, lint, security scans, benchmark default | `blacksmith-4vcpu-ubuntu-2404` |
| fiber `repeated`, recipes `gobuild`, docs `deploy`, CodeQL Go | `blacksmith-8vcpu-ubuntu-2404` |
| markdown, spell-check, coordination jobs | `blacksmith-2vcpu-ubuntu-2404` |
| Windows legs | `blacksmith-4vcpu-windows-2025` (Public Beta) |
| macOS legs | `blacksmith-6vcpu-macos-latest` (6 vCPU is the smallest offered) |

Release automation, dependabot auto-merge and the sync jobs stay on `ubuntu-latest`
on purpose, so releases and merges survive a Blacksmith outage and their
write-scoped tokens stay on GitHub infrastructure.

Billing ratios matter even on the free tier, since the free minutes are consumed at
those rates: Windows counts double, macOS twenty times, ARM 0.625x.

### Go patch versions differ from GitHub's images

`storage`'s MSSQL tests went red the first run after the switch:

```
go: go.mod requires go >= 1.25.7 (running go 1.25.6; GOTOOLCHAIN=local)
```

`actions/setup-go` resolves a `1.25.x` spec against the runner image's tool cache and
does not look for anything newer unless `check-latest: true` is set. GitHub's image
had 1.25.7 cached, Blacksmith's had 1.25.6, and `mssql/go.mod` requires 1.25.7.
setup-go also exports `GOTOOLCHAIN=local`, so Go may not fetch the toolchain itself.

**Always set `check-latest: true`** where a `.x` spec is used. `storage/firestore`
requires 1.25.8 and would have been next.

Setting `GOTOOLCHAIN: auto` at job level is not a workaround: the recipes build log
shows `GOTOOLCHAIN: local` in the step environment even though the job sets `auto`,
because setup-go's `exportVariable` wins.

### Caches do not carry over

Blacksmith redirects `actions/cache` and the `setup-*` caches to its own backend, so
GitHub's existing entries are invisible from there. Every cache is cold once after
the switch: Go modules, build cache, npm, and the benchmark baseline, which is why
the first PR comparison after the move finds no baseline. No action swaps are needed,
the `useblacksmith/*` actions are archived per their own docs.

### Matrix legs

There are **no required status checks** in any repo, neither classic branch
protection nor active rulesets. That is what made it safe to name the runner directly
in the matrix instead of routing around it with an expression - job names change, and
nothing depends on them.

The codecov gates were switched from `matrix.platform == 'ubuntu-latest'` to
`runner.os == 'Linux'` in the same move. They mean "upload from the Linux leg", not
"from that specific label", and they now survive the next runner change.

## Benchmarks (fiber)

A run took 16 minutes. Compiling every test binary is 21s cold and 11s warm, so
roughly 15 of those 16 minutes are benchmark execution - which is why sharding scales
almost linearly here.

Two findings shaped the design:

- **221 of 394 benchmarks (56%) live in the root package.** Splitting by package
  would cap out around 1.8x, so shards have to split by benchmark name.
- **Benchmark names are not unique across packages.** Five names exist in two
  packages each. A single `-bench` regex over `./...` would run those in every
  package that defines them and measure them twice, so each shard invokes `go test`
  **per package** with that package's own subset.

`shards` in `benchmark.yml` splits the run and merges the output before anything is
compared or published, so github-action-benchmark sees exactly what an unsharded run
would produce. Verified end to end against a reference run for 2, 3 and 4 shards.
The reporting half lives in the `benchmark-report` composite action so the unsharded
path stays a single job - otherwise storage's 35 matrix legs would have doubled to 70.

The merged run is reported from a job that never benchmarked anything, so each shard
records its `lscpu` model into its artifact and the merge hands it on. Shards landing
on different CPUs produce a warning: that data point then mixes hardware.

`filter-parallel` used to run the 54 `_Parallel` benchmarks and then drop them from
the results. `-skip _Parallel` skips them up front; the filter stays as the guard
that keeps them out of the chart history if the flag is ever lost.

`build-mode: none` is not available for Go - only C/C++, C#, Java and Rust.

## Repeated tests

`-shuffle=on` seeds **once per process**, not per `-count` iteration:

```
-count=3 -shuffle=on:   C B E A D | C B E A D | C B E A D
second process:         C E D A B | C E D A B | C E D A B
```

A single `-count=15` therefore replays one ordering fifteen times, and ordering
dependence - the only thing shuffling can find - is sampled exactly once. Splitting
into three jobs of `-count=5` keeps the fifteen runs, samples three orderings and
finishes in a third of the time.

Cost model from a real run (unit job with `-count=1` is 60s, repeated with
`-count=15` is 600s): about 21s fixed plus 38.6s per pass, so `21 + 579/N` seconds
per leg. Five legs would be ~2.3 min, fifteen legs ~1 min, at proportionally more
concurrent vCPU.

## recipes build

91 independent Go modules, previously vetted and built in two sequential loops.

| | Duration |
| --- | --- |
| GitHub runner, sequential | 526s |
| Blacksmith 8 vCPU, sequential | 348s |
| Blacksmith 8 vCPU, parallel | 113s |

The build step itself is 27s of that; the rest is setup-go with cache restore. It is
this fast because the shared build cache makes almost everything a cache hit, which
is also why parallelising helped: per module there is barely enough work left to keep
one core busy.

Two things worth remembering:

- **`go vet` and `go build` print nothing on success.** The first parallel version
  dropped the old per-directory `echo`, and the step logged zero lines - "all green"
  and "never ran" looked identical. Each module now reports `ok`, `FAILED` or
  `skipped, no go.mod`, and the step prints how many directories it is processing.
- The old loop ran under GitHub's default `bash -e` and **aborted at the first broken
  module**, so modules after it were never checked. The parallel version runs all of
  them and still fails the step.

## CodeQL

Default setup is enabled in 13 of 14 repos and runs on GitHub-hosted standard
runners; `runner_label` is empty and cannot be pointed at Blacksmith. Using a
Blacksmith runner requires advanced setup, and the two are mutually exclusive - while
default setup is enabled GitHub rejects an advanced workflow's SARIF upload.

Measured on recipes, Go analysis only:

| Step | Duration |
| --- | --- |
| Initialize CodeQL | 135s |
| Setup Go | 12s |
| **Autobuild** | **406s** |
| Perform CodeQL Analysis | 29s |

The queries take 29 seconds. Everything else is tooling and building. `actions` and
`javascript-typescript` finish in seconds.

Two things that look like problems and are not:

- The API reports five languages (`actions`, `go`, `javascript`,
  `javascript-typescript`, `typescript`) but only **three** analyses run. The other
  two are aliases.
- The recipes root `go.mod` is a stub whose `go list ./...` yields a single package,
  which suggested CodeQL was scanning almost nothing. The 406s autobuild says
  otherwise: CodeQL's Go autobuilder finds the 91 modules by itself. That is why
  `recipes/.github/workflows/codeql.yml` keeps `autobuild` instead of a hand-written
  build - a manual build that misses a module shrinks the database silently rather
  than failing.

The lever there is `actions/setup-go` with cache **before** `init`, so autobuild does
not rebuild every dependency tree from scratch. Analysis categories must stay
`/language:<lang>` to keep the existing alert history.

Unverified: whether Blacksmith's image ships the preinstalled CodeQL bundle that
makes GitHub's `Initialize CodeQL` take 135s. If not, that step gets slower and eats
part of the gain. Check it on the first run.

## Local runs

`~/.actrc` and `.github/.actrc` map the Blacksmith labels onto
`catthehacker/ubuntu:act-24.04` for `act`. Two flags matter:

- `--artifact-server-path`, because the sharded benchmark passes results between jobs
  as artifacts and those steps fail without a local artifact server.
- `--container-architecture linux/amd64`, because the runners are x64 and act
  otherwise uses the host architecture. Left commented out in the global file: it
  puts every act run on Apple Silicon through emulation, in every project.

act cannot emulate Windows or macOS; those labels map to a Linux image so a matrix
job still runs end to end, and nothing OS-specific is faithful. act reads `.actrc`
from the working directory and `$HOME` only, never from a sibling checkout.

## How these were measured

Worth recording, because two methods gave wrong answers first:

- **GitHub code search is incomplete.** Searching the organisation for
  `benchmark.yml@main` returned 4 of the 5 known callers. A negative result from it
  proves nothing. The caller table above comes from reading every workflow file of
  every repo through the contents API, with `benchmark.yml` as a control that has to
  return exactly 5.
- **Local checkouts are not the organisation.** 13 of 34 repos were checked out;
  `website` is the only non-archived one missing. Counts taken locally are lower
  bounds.
- **Occurrences are not repos.** A file can reference the same workflow twice.
