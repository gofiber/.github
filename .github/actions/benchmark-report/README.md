# benchmark-report

Turns a raw `go test -bench` output file into the PR comparison, the stored baseline
and the gh-pages publish. Called by [`benchmark.yml`](../../workflows/benchmark.yml)
from both the single-job and the merged sharded run, so both go through identical
logic. Inputs are documented in [`action.yml`](action.yml).

The comparison itself lives in [`compare.py`](compare.py) (stdlib only). It replaces
the alerting half of `benchmark-action/github-action-benchmark`, which decides the
"better" direction per tool instead of per unit and therefore reported every MB/s
gain as a regression. The publishing half is gone too: the gh-pages data is written
by [`benchmark-pages/publish.py`](../benchmark-pages/publish.py) in a columnar v2
format (every series name once, values as a matrix) after gab's per-run format had
grown fiber's data.js to 85 MB, a third of GitHub's hard file limit, for ~1.4 MB of
information. A legacy data.js is converted automatically on the first publish, the
page reads both formats, and the series names follow gab's extractor rules exactly
so no chart forks at the cutover. Data publish and page sync land as one commit.
The full per-run table gab wrote into the job summary on default-branch runs is
gone with it; the report and the charts carry that information.

## Comment rules

A benchmark comment that shows up on every push is a comment nobody reads. The
action is allowed to be loud exactly once per finding.

- **Nothing significant, nothing posted.** No comment at all until a benchmark
  crosses `alert-threshold` or `improve-threshold`.
- **Refresh in place, do not repost.** Editing a comment sends no notification, so
  the posted report is patched with the current numbers as long as it holds. A new
  comment is only created when both of these are true:
  1. the findings actually changed (see below), and
  2. the posted report has been buried under someone else's comment, so refreshing
     it would go unseen.
- **Superseded reports are hidden.** When a new comment is posted, the one it
  replaces is minimized as outdated (the GraphQL `minimizeComment` mutation with
  classifier `OUTDATED`): GitHub folds it away and labels it, its body and marker
  stay intact. At most one report per module is ever visible.
- **A fixed regression is cleared in place.** Once the numbers moved far enough to
  count as fixed (see below) the posted report is patched down to a one-line all
  clear. It is never deleted, so a flapping benchmark cannot notify the PR twice by
  removing and reposting the same comment.
- **Sibling modules do not bury each other.** A repo like `storage` posts one report
  per package. Those count as the bot's own noise, only foreign comments bury a
  report.
- **Pushes to the default branch** have no PR to comment on and report regressions
  as a commit comment. Improvements are not worth a commit comment.
- **Commenting never fails the job.** Both comment steps are `continue-on-error`, so
  a rate limit or a missing permission is a warning and not a red benchmark gate. A
  failed lookup skips the posting for that run rather than posting a duplicate.

### What counts as changed

Every report carries a fingerprint of its findings in an HTML comment: one line per
`(package, benchmark, unit)` that crossed a threshold, plus the factor it moved by.
The next run reports again when

- it flags a benchmark the posted report does not name, or
- a benchmark the report names has moved more than `--tolerance` (25%) since, which
  covers both a regression getting worse and one being fixed, or
- a benchmark the report names is gone from this run entirely.

The two halves are deliberately asymmetric. A new finding is judged by the
threshold, an already reported one by how far it moved. Judging both by the
threshold would let a result sitting on the line drop in and out of the list on
noise alone, and every one of those flips would be a comment.

The tolerance exists because the runner pools are not hardware-homogeneous and an
untouched benchmark wobbles by double digit percentages between runs. Keying the
baseline by CPU model and skipping the comparison for runs that spanned several of
them bounds that wobble, it does not remove it.

Reports with more than 200 findings carry no fingerprint, they are treated as
changed every time. At that point the report is not about individual benchmarks
anymore.

### Every benchmark carries its own noise floor

A fixed threshold cannot serve 600 benchmarks at once: some sit rock-stable at
±2%, others (proxy, SendFile, anything with heavy setup) swing 1.5-2x between
runs of identical code. The published v2 history knows which is which: the p95
of a benchmark's adjacent-run ratios is its personal noise band, and its
effective threshold is `max(alert-threshold, p95 * 1.15)` in both directions.
The same slack applies before a reported finding counts as moved. This is the
defense a same-machine retest cannot provide: PR 3702's three surviving false
improvements (1.54-1.72x on benchmarks that historically wobble that far) all
die against their own history. Fetched best effort from gh-pages; without it,
the flat thresholds stand.

### Every deviation has to reproduce

The pools are noisy enough that a single measurement is as likely wobble as change
(an untouched benchmark has been seen to move close to 2x between runs). So every
result whose single measurement would touch the comment is re-measured in the same
job with an exact `-bench` regex, seconds instead of the quarter hour of the full
suite, and only what reproduces is believed. That covers

- fresh regressions: they fail the gate, so a false one is a red PR for nothing,
- fresh improvements: they do not fail anything, but a false ⚡ posts a comment and
  praises a commit for a runner's good mood,
- results the posted report names whose numbers have supposedly moved since: a
  "fixed" regression is just as often the runner as a real fix, so it is re-checked
  before the comment is cleared or rewritten.

The rules of the re-measurement:

- Only the verified benchmarks take their re-measured value. The regex targets the
  top-level benchmark (subtests ride along, `-bench` matches per slash level), but
  anything the second run measured beyond that is ignored, otherwise a fresh wobble
  could flag something the first run cleared.
- The retest runs `-count=3` and the median decides, so a single wobble in the
  re-measurement itself cannot confirm a false finding or clear a real one.
- Multi-module (loop-mode) repos work too: the plan maps each package to the
  top-level module that owns it and the retest runs inside that directory. A
  package no module claims stays unverified and its finding stands.
- A lone direction flip dies: a finding that shows slower on one measurement and
  faster on the other is reported as neither. When the posted report already
  agreed with one of the two directions, that side wins - two of three readings
  beat the outlier.
- A default-branch run stores the verified values as the baseline (`verified.txt`
  over the raw output). Without that, a one-off spike in main's own run would
  become the number every following PR is judged against, and the PR-side retest
  cannot catch a corrupted base: both PR measurements would honestly reproduce
  the phantom difference.
- The footer says what happened: `retest: 1/2 regressions, 1/1 improvements
  reproduced, 1 reported re-checked`.
- The retest is skipped, and the first measurement stands, when more than 100
  benchmarks moved (that is not a noise problem) or when the runner's CPU differs
  from the one the numbers came from.
- The job summary carries a per-benchmark verification breakdown (first
  measurement, retest, noise bar, outcome); the PR comment stays lean.

## Report format

```
❗❗❗ 2 benchmarks slower (up to 6.20x)    <- expanded, the half that needs acting on
⚡ 9 benchmarks faster (up to 2.30x)       <- collapsed
a1b2c3d vs main@6955385 · 1608/1620 results compared · retest: 2/2 regressions
reproduced · Ampere-1a (GOMAXPROCS=4) · full results
```

Both commit shas link to their commits, `full results` links to the workflow run.

- `⚡` faster, `❗` slower. One symbol at the threshold, one more per doubling past
  it, three at most, so a 6x regression is visible without reading the numbers. The
  two summary lines carry the worst case, so a collapsed report still says what it
  found and which commit it measured.
- One row per benchmark, not per metric: `ns/op`, `B/op` and `allocs/op` of the same
  benchmark move together and share a row.
- The package is shortened to what is not shared with the other findings, and the
  full path moves to the footer when there is only one.
- Tables stop at 15 rows. The footer links to the workflow run, where the job
  summary carries the same report and the job log has every measured number.

## Tests

```bash
python3 .github/scripts/test/test_benchmark_compare.py   # comparison and fingerprint
bash .github/scripts/test/test-benchmark-comment.sh      # the comment rules above
bash .github/scripts/test/test-benchmark-cpu-key.sh      # baseline cache key
```

`test-benchmark-comment.sh` extracts the comment steps out of `action.yml` and runs
them against a fake `gh`, so it tests what ships rather than a copy. All three run in
the `scripts` job of [`test-actions.yml`](../../workflows/test-actions.yml).
