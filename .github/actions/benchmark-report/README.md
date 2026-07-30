# benchmark-report

Turns a raw `go test -bench` output file into the PR comparison, the stored baseline
and the gh-pages publish. Called by [`benchmark.yml`](../../workflows/benchmark.yml)
from both the single-job and the merged sharded run, so both go through identical
logic. Inputs are documented in [`action.yml`](action.yml).

The comparison itself lives in [`compare.py`](compare.py) (stdlib only). It replaces
the alerting half of `benchmark-action/github-action-benchmark`, which decides the
"better" direction per tool instead of per unit and therefore reported every MB/s
gain as a regression. `github-action-benchmark` is still used, but only to publish
the charts.

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
- **Superseded reports are collapsed.** When a new comment is posted, the one it
  replaces is rewritten into a collapsed `<details>` block marked outdated and
  linking to its replacement. At most one report per module is ever expanded.
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

## Report format

```
❗❗❗ 2 benchmarks slower (up to 6.20x)    <- expanded, the half that needs acting on
⚡ 9 benchmarks faster (up to 2.30x)       <- collapsed
a1b2c3d vs main@6955385 · 1608/1620 results compared · Neoverse-N1 (GOMAXPROCS=6)
```

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
