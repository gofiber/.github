#!/usr/bin/env python3
"""Compare two `go test -bench` runs and report regressions and improvements.

Replaces the alerting half of github-action-benchmark, which decides the "better"
direction per tool rather than per unit and reads every Go metric as
smaller-is-better. A throughput gain in MB/s therefore showed up as a regression,
which was worked around by stripping MB/s before comparing - and that hid real
throughput regressions along with it. Direction is decided per unit here.

Writes a compact markdown report to --out and exits 1 when a regression crosses
the threshold. The report carries a fingerprint of its findings, so the next run
can tell whether anything actually changed and keep quiet when it did not.
"""
import argparse
import glob
import json
import math
import os
import re
import statistics
import sys
from collections import namedtuple

# Non-greedy name so the trailing -GOMAXPROCS is split off even when the
# benchmark name itself ends in -<digits> (subtests like Benchmark_X/n-8).
BENCH_RE = re.compile(r"^(?P<name>Benchmark\S*?)(?:-\d+)?[ \t]+\d+[ \t]+(?P<rest>\S.*?)[ \t]*$")
PKG_RE = re.compile(r"^pkg:[ \t]+(?P<pkg>\S+)")
# \r?: GitHub rewrites a comment body to CRLF once anyone edits it in the web UI,
# and a digest that stops matching would silently turn every run into "changed"
FINGERPRINT_RE = re.compile(r"<!-- benchmark-fingerprint\r?\n(?P<body>.*?)-->", re.S)
# Beyond this the digest would outweigh the report it rides on. Leaving it out
# just means the next run counts as changed, which is the safe direction.
FINGERPRINT_MAX = 200
UNITS = ("ns/op", "MB/s", "B/op", "allocs/op")
# A run where this many benchmarks moved is not having a noise problem, and
# re-measuring them would cost a good part of the full suite again.
RETEST_MAX = 100
# B/op and allocs/op are integers rounded from an amortized total: a benchmark
# allocating ~1 byte per op flips between 0 and 1 on iteration-count luck alone,
# and as a ratio such a flip is infinite - no multiplicative threshold, noise
# band or same-machine retest can absorb it (observed: Benchmark_Ctx_Links
# 1 -> 0 B/op reported as an "∞x" improvement between identical commits).
# Only the flip zone is silenced: byte moves under one real allocation (Go's
# smallest is 8), and the 0<->1 allocs/op zone. A genuinely new allocation
# always moves B/op by >= 8 too, so it stays visible; 1 -> 2 allocs still alerts.
MIN_UNIT_DELTA = {"B/op": 8.0}


def quantization_noise(unit, base, current):
    """Whether a change sits inside the rounding wobble of an integer unit."""
    if unit == "allocs/op":
        return max(base, current) <= 1
    return abs(current - base) < MIN_UNIT_DELTA.get(unit, 0.0)
# How the published history's series names carry package and metric, and how far
# above a benchmark's own historic wobble its personal threshold sits. The margin
# is deliberately thin: the wobble is already the tail of observed noise.
# Two complementary statistics, the floor is their maximum:
# - p95 of adjacent-run ratios catches frequent step-to-step swings and is robust
#   against single real perf changes in the history;
# - the trimmed range of the recent window catches bimodal benchmarks whose rare
#   machine-mode switches slip under any adjacent quantile (MarshalMsgcachedHeader
#   sits at ~56 or ~94 ns/op and every switch looked like a 1.68x finding).
# Calibrated on the four false positives observed across PR 3702 and the
# main-vs-main dispatch runs: each statistic alone lets one through, the
# combination kills all of them.
HISTORY_METRIC_RE = re.compile(r" - [^ ]*/[^ ]*$")
HISTORY_PKG_RE = re.compile(r" \(([^()]+)\)$")
NOISE_MARGIN = 1.15
NOISE_QUANTILE = 0.95
NOISE_MIN_PAIRS = 5
NOISE_WINDOW = 16
NOISE_WINDOW_MIN = 8

Key = namedtuple("Key", "pkg name unit")
Change = namedtuple("Change", "key base current ratio")
Row = namedtuple("Row", "pkg name strength changes")


def bigger_is_better(unit):
    """Go's convention: per-operation costs end in /op, throughput rates in /s."""
    return unit.endswith("/s")


def parse_runs(stream):
    """Read benchmark output keeping every measurement, {(pkg, name, unit): [values]}."""
    results = {}
    pkg = ""
    for line in stream:
        header = PKG_RE.match(line)
        if header:
            pkg = header.group("pkg")
            continue
        match = BENCH_RE.match(line)
        if not match:
            continue
        fields = match.group("rest").split()
        # "<name> <iterations> <value> <unit> [<value> <unit>...]", so an odd
        # tail means the line is not a benchmark result after all
        if len(fields) % 2:
            continue
        for value, unit in zip(fields[::2], fields[1::2]):
            try:
                number = float(value)
            except ValueError:
                continue
            results.setdefault(Key(pkg, match.group("name"), unit), []).append(number)
    return results


def parse(stream):
    """Read benchmark output into {(pkg, name, unit): value}, last measurement wins."""
    runs = parse_runs(stream)
    duplicates = sum(len(values) - 1 for values in runs.values())
    return {key: values[-1] for key, values in runs.items()}, duplicates


def ratio(unit, base, current):
    """How much worse current is than base. Above 1 is a regression either way."""
    if base == current:
        return 1.0
    if bigger_is_better(unit):
        base, current = current, base
    # zero is the best a per-operation metric can be, so allocs 1 -> 0 must not
    # come out the same as 0 -> 1 just because both divide by zero
    if base == 0:
        return float("inf")
    if current == 0:
        return 0.0
    return current / base


def bars(key, threshold, improve_threshold, noise):
    """The thresholds for one benchmark: at least its own historic noise floor."""
    wobble = noise.get(key) if noise else None
    if not wobble:
        return threshold, improve_threshold
    floor = wobble * NOISE_MARGIN
    return max(threshold, floor), max(improve_threshold, floor)


def compare(base, current, threshold, improve_threshold, min_ns, noise=None):
    """Split the benchmarks present in both runs into regressions and improvements."""
    too_fast = {
        (key.pkg, key.name)
        for key, value in current.items()
        if key.unit == "ns/op" and value < min_ns
    }
    worse, better = [], []
    for key, value in current.items():
        if key not in base or (key.pkg, key.name) in too_fast:
            continue
        if quantization_noise(key.unit, base[key], value):
            continue
        change = Change(key, base[key], value, ratio(key.unit, base[key], value))
        bar, improve_bar = bars(key, threshold, improve_threshold, noise)
        if change.ratio >= bar:
            worse.append(change)
        elif change.ratio <= 1 / improve_bar:
            better.append(change)
    worse.sort(key=lambda c: -c.ratio)
    better.sort(key=lambda c: c.ratio)
    return worse, better


def load_history(path):
    """The published v2 data.js as a dict, or None when absent or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if data.get("version") == 2 else None


def series_key(name, unit):
    """A data.js series name mapped back to the (pkg, name, unit) compare key."""
    series = HISTORY_METRIC_RE.sub("", name)
    pkg_match = HISTORY_PKG_RE.search(series)
    pkg = pkg_match.group(1) if pkg_match else ""
    bare = series[: pkg_match.start()] if pkg_match else series
    return Key(pkg, bare, unit)


def history_baseline(path, cpu):
    """Newest published run of the same CPU as {Key: value}, plus its commit sha.

    The published data IS the baseline: the report publishes the verified
    numbers, so the newest column already holds exactly what the old cached
    baseline file used to duplicate - and unlike a cache entry it cannot be
    evicted. Runs of other CPU models are skipped for the same reason the old
    cache key carried the model: a comparison only lines up when both sides
    saw the same hardware.
    """
    data = load_history(path)
    if not data or not cpu:
        return {}, ""
    runs = data.get("runs", [])
    pick = next((i for i in range(len(runs) - 1, -1, -1) if runs[i].get("cpu") == cpu), None)
    if pick is None:
        return {}, ""
    base = {}
    for row, name in enumerate(data.get("names", [])):
        values = data.get("values", [])[row]
        value = values[pick] if pick < len(values) else None
        if value is not None:
            base[series_key(name, data["units"][row])] = value
    return base, runs[pick].get("id", "")


def align_packages(mapping, current):
    """Attach the run's package to history keys that carry none.

    Repos publishing without the package suffix (single-module storage) name
    their series bare, while the run's own results always know the package.
    Only an unambiguous bare name is claimed; a name two packages share stays.
    """
    owners = {}
    for key in current:
        owners.setdefault((key.name, key.unit), []).append(key)
    aligned = {}
    for key, value in mapping.items():
        if not key.pkg:
            candidates = owners.get((key.name, key.unit), [])
            if len(candidates) == 1:
                aligned[candidates[0]] = value
                continue
        aligned[key] = value
    return aligned


def read_history(path):
    """Per-benchmark run-to-run wobble from the published v2 data, {Key: factor}.

    Adjacent default-branch runs are overwhelmingly same-code, so the tail of
    their pairwise ratios is that benchmark's own noise band, measured across
    many machines - which is exactly what a same-machine retest cannot see.
    """
    data = load_history(path)
    if not data:
        return {}
    wobble = {}
    for row, name in enumerate(data.get("names", [])):
        values = [v for v in data["values"][row] if v is not None]
        parts = []
        ratios = []
        for before, after in zip(values, values[1:]):
            if before > 0 and after > 0:
                step = after / before
                ratios.append(step if step >= 1 else 1 / step)
        if len(ratios) >= NOISE_MIN_PAIRS:
            ratios.sort()
            parts.append(ratios[int(NOISE_QUANTILE * (len(ratios) - 1))])
        recent = sorted(v for v in values[-NOISE_WINDOW:] if v > 0)
        if len(recent) >= NOISE_WINDOW_MIN:
            # trimmed by one on each side, so a single outlier run does not
            # inflate the floor for the next two weeks
            parts.append(recent[-2] / recent[1])
        if parts:
            wobble[series_key(name, data["units"][row])] = rounded(max(parts))
    return wobble


def strength(value, improved):
    """How much better or worse, as a factor of at least 1 whichever way it went."""
    if improved:
        return 1 / value if value else math.inf
    return value


def group(changes, improved):
    """One row per benchmark: ns/op, B/op and allocs/op of the same one move together."""
    grouped = {}
    for change in changes:
        grouped.setdefault((change.key.pkg, change.key.name), []).append(change)
    rows = []
    for (pkg, name), members in grouped.items():
        # the order Go prints them in, so a row does not reshuffle between runs
        members.sort(key=lambda c: unit_rank(c.key.unit))
        rows.append(Row(pkg, name, max(strength(c.ratio, improved) for c in members), members))
    rows.sort(key=lambda row: -row.strength)
    return rows


def unit_rank(unit):
    return (UNITS.index(unit), "") if unit in UNITS else (len(UNITS), unit)


def package_labels(packages):
    """Drop the module path they all share, `.../fiber/v3/binder` becomes `v3/binder`."""
    if len(packages) < 2:
        return {}
    split = {pkg: pkg.split("/") for pkg in packages}
    shared = 0
    while all(len(parts) > shared + 1 for parts in split.values()) and (
        len({parts[shared] for parts in split.values()}) == 1
    ):
        shared += 1
    return {pkg: "/".join(parts[shared:]) for pkg, parts in split.items()}


def marks(value, threshold, symbol):
    """One symbol at the threshold, one more per doubling past it, three at most."""
    if math.isinf(value):
        return symbol * 3
    return symbol * min(3, 1 + int(math.log2(max(value / threshold, 1))))


def counted(rows):
    return f"{len(rows)} benchmark" + ("" if len(rows) == 1 else "s")


def number(value):
    return f"{value:.2f}".rstrip("0").rstrip(".") if value < 1e6 else f"{value:.0f}"


def factor_text(value):
    return "∞x" if math.isinf(value) else f"{value:.2f}x"


def table(rows, limit, improved, threshold, labels):
    symbol = "⚡" if improved else "❗"
    lines = ["| Benchmark | | Base → Current |", "|-|-|-|"]
    for row in rows[:limit]:
        label = labels.get(row.pkg, "")
        name = f"`{row.name}`" + (f" <sub>{label}</sub>" if label else "")
        values = " · ".join(
            f"{number(c.base)} → {number(c.current)} {c.key.unit}" for c in row.changes
        )
        marked = f"{marks(row.strength, threshold, symbol)} {factor_text(row.strength)}"
        lines.append(f"| {name} | {marked} | {values} |")
    if len(rows) > limit:
        lines.append(f"| _and {len(rows) - limit} more_ | | |")
    return lines


def details(summary, body, expanded=False):
    # the blank lines are what makes GitHub render markdown inside the block
    tag = f"<details{' open' if expanded else ''}>"
    return [tag, f"<summary>{summary}</summary>", "", *body, "", "</details>"]


def key_text(key):
    return f"{key.pkg}|{key.name}|{key.unit}"


def text_key(text):
    """Inverse of key_text; None for digest lines that are not a key at all."""
    if text.count("|") < 2:
        return None
    pkg, rest = text.split("|", 1)
    # the unit never contains a pipe, a subtest name theoretically could
    name, unit = rest.rsplit("|", 1)
    return Key(pkg, name, unit)


def digest(changes):
    """Digest of the findings a report named, `pkg|benchmark|unit` to the factor."""
    return {key_text(c.key): rounded(c.ratio) for c in changes}


def factors(base, current):
    """Where every comparable benchmark stands now, keyed like the digest."""
    return {
        f"{key.pkg}|{key.name}|{key.unit}": rounded(ratio(key.unit, base[key], value))
        for key, value in current.items()
        if key in base
    }


def rounded(value):
    return float(f"{value:.3g}")


def read_digest(text):
    """What an earlier report found, or None if it carries no readable digest."""
    match = FINGERPRINT_RE.search(text)
    if not match:
        return None
    entries = {}
    for line in match.group("body").splitlines():
        fields = line.split()
        if len(fields) != 2:
            return None
        try:
            entries[fields[0]] = float(fields[1])
        except ValueError:
            return None
    return entries


def near(before, after, tolerance):
    """Within run-to-run noise of each other."""
    if before == after:
        return True
    if not before or not after or math.isinf(before) or math.isinf(after):
        return False
    return max(before, after) / min(before, after) <= 1 + tolerance


def slack(key, tolerance, noise):
    """The movement a benchmark gets for free: its own noise band, at least tolerance."""
    wobble = noise.get(key) if noise else None
    return max(tolerance, wobble - 1) if wobble else tolerance


def nothing_new(previous, found, now, tolerance, noise=None):
    """Whether the posted report still describes this run.

    Deliberately asymmetric: a benchmark it does not name yet is news, but one it
    does name is compared against where that benchmark stands now, not against
    whether it is still over the threshold. A result sitting right on the line
    would otherwise drop in and out of the list on noise alone.
    """
    if previous is None or any(key not in previous for key in found):
        return False
    for text, before in previous.items():
        key = text_key(text)
        if not near(before, now.get(text), slack(key, tolerance, noise) if key else tolerance):
            return False
    return True


def headline(rows, threshold, symbol, word):
    """The one line that has to work collapsed: how many, how bad, worst first."""
    top = factor_text(rows[0].strength)
    return (
        f"{marks(rows[0].strength, threshold, symbol)} <b>{counted(rows)} {word}</b>"
        f" ({top if len(rows) == 1 else f'up to {top}'})"
    )


def render(worse, better, threshold, improve_threshold, limit, notes):
    slower = group(worse, False)
    faster = group(better, True)
    labels = package_labels({row.pkg for row in slower + faster})
    lines = []
    if slower:
        lines += details(
            headline(slower, threshold, "❗", "slower"),
            table(slower, limit, False, threshold, labels),
            expanded=True,
        )
    if faster:
        lines += details(
            headline(faster, improve_threshold, "⚡", "faster"),
            table(faster, limit, True, improve_threshold, labels),
        )
    if not lines:
        lines = ["✅ No significant benchmark change."]
    packages = {row.pkg for row in slower + faster}
    if len(packages) == 1:
        notes = notes + [f"`{packages.pop()}`"]
    lines += ["", f"<sub>{' · '.join(notes)}</sub>"]
    found = digest(worse + better)
    if len(found) <= FINGERPRINT_MAX:
        lines += ["", "<!-- benchmark-fingerprint"]
        lines += [f"{key} {value:.3g}" for key, value in sorted(found.items())]
        lines += ["-->"]
    return "\n".join(lines).rstrip() + "\n"


def factor(text):
    """Accept `150%`, `1.5` or empty; awk would render either in the shell's locale."""
    text = text.strip()
    if not text:
        return None
    return float(text[:-1]) / 100 if text.endswith("%") else float(text)


def linked(sha, repo_url):
    return f"[{sha[:7]}]({repo_url}/commit/{sha})" if repo_url else sha[:7]


def pairs(keys):
    return {(key.pkg, key.name) for key in keys}


def modules(workdir):
    """Modules at the root and one level down, {module path: relative directory}."""
    found = {}
    for mod in sorted(glob.glob(os.path.join(workdir, "go.mod")) + glob.glob(os.path.join(workdir, "*", "go.mod"))):
        with open(mod, encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"module[ \t]+(\S+)", line)
                if match:
                    found[match.group(1)] = os.path.relpath(os.path.dirname(mod), workdir)
                    break
    return found


def write_plan(path, keys, workdir):
    """Regex and per-module package list for re-measuring only what moved.

    Lines after the regex are `dir<TAB>pkg pkg ...`. Each package goes to the
    module with the longest owning path; the root module and the loop-mode
    subdirectories are all candidates (template carries both at once), and a
    package no module claims is left unverified.
    """
    named = sorted(pairs(keys))
    with open(path, "w", encoding="utf-8") as handle:
        if not named or len(named) > RETEST_MAX:
            return
        known = modules(workdir)
        groups = {}
        for pkg in sorted({pkg for pkg, _ in named}):
            owner = max(
                (mod for mod in known if pkg == mod or pkg.startswith(mod + "/")),
                key=len,
                default=None,
            )
            if owner:
                groups.setdefault(known[owner], []).append(pkg)
        if not groups:
            return
        # -bench matches per slash-separated level, so target the top-level
        # benchmark and take its subtests along
        roots = sorted({re.escape(name.split("/", 1)[0]) for _, name in named})
        handle.write("^(" + "|".join(roots) + ")$\n")
        for directory in sorted(groups):
            handle.write(directory + "\t" + " ".join(groups[directory]) + "\n")


def write_details(path, base, first_run, retested, to_verify, flagged_worse, flagged_better, worse, better, threshold, improve_threshold, noise):
    """The verification breakdown, one row per checked benchmark, for the summary."""
    final_worse = {c.key for c in worse}
    final_better = {c.key for c in better}
    lines = ["### Verification", ""]
    if not to_verify:
        lines.append("Nothing crossed a threshold or moved against the posted report.")
    elif not retested:
        lines.append(f"{len(pairs(to_verify))} result(s) flagged, but not re-measured; the first measurement stands.")
    else:
        lines += [
            "| Benchmark | Unit | Noise bar | First run | Retest | Outcome |",
            "|-|-|-|-|-|-|",
        ]
        for key in sorted(to_verify)[:150]:
            first = rounded(ratio(key.unit, base[key], first_run[key]))
            second = retested.get(key)
            second_text = "-"
            if second is not None:
                second_text = factor_text(rounded(ratio(key.unit, base[key], second)))
            bar, improve_bar = bars(key, threshold, improve_threshold, noise)
            if key in final_worse:
                outcome = "regression confirmed"
            elif key in final_better:
                outcome = "improvement confirmed"
            elif key in flagged_worse or key in flagged_better:
                outcome = "not reproduced, dropped"
            else:
                outcome = "reported result re-checked"
            lines.append(
                f"| `{key.name}` <sub>{key.pkg}</sub> | {key.unit} |"
                f" {factor_text(max(bar, improve_bar))} | {factor_text(first)} | {second_text} | {outcome} |"
            )
        if len(to_verify) > 150:
            lines.append(f"| _and {len(to_verify) - 150} more_ | | | | | |")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def save_verified(path, results, keep=()):
    """The believed values in `go test -bench` shape, so parse() reads them back.

    Every name gets a sacrificial -1 suffix: parse() strips one trailing -N (the
    GOMAXPROCS count on real output), and a bare name ending in -8 would lose it.
    `keep` carries raw lines to append verbatim - the `ok <pkg> <secs>` lines,
    which the shard weighing reads from the stored baseline and which this
    serialization would otherwise lose.
    """
    grouped = {}
    for key, value in results.items():
        grouped.setdefault(key.pkg, {}).setdefault(key.name, []).append((key.unit, value))
    with open(path, "w", encoding="utf-8") as handle:
        for pkg in sorted(grouped):
            handle.write(f"pkg: {pkg}\n")
            for name, units in sorted(grouped[pkg].items()):
                units.sort(key=lambda pair: unit_rank(pair[0]))
                columns = "\t".join(f"{value:.10g} {unit}" for unit, value in units)
                handle.write(f"{name}-1\t1\t{columns}\n")
        for line in keep:
            handle.write(line.rstrip("\n") + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    # explicit baseline file; empty means the newest same-CPU run in --history
    parser.add_argument("--base", default="")
    # CPU model this run measured on, to pick its baseline from the history
    parser.add_argument("--baseline-cpu", default="")
    parser.add_argument("--current", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--threshold", type=factor, default=1.5)
    # empty mirrors --threshold: an improvement is worth reporting exactly when a
    # regression of the same size would be, so both directions carry the same noise
    parser.add_argument("--improve-threshold", type=factor, default=None)
    parser.add_argument("--min-ns", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--hardware", default="")
    parser.add_argument("--commit", default="")
    parser.add_argument("--baseline-ref", default="")
    parser.add_argument("--baseline-sha", default="")
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--run-url", default="")
    # Where to write the retest instructions when anything would touch the comment,
    # and where the re-measured numbers come back in. What does not reproduce on the
    # second measurement is dropped; see the retest block in action.yml.
    parser.add_argument("--retest-plan", default="")
    parser.add_argument("--retested", default="")
    # module root (or loop-mode parent) the plan's directories are relative to
    parser.add_argument("--workdir", default=".")
    # published v2 data.js; per-benchmark noise floors are derived from it
    parser.add_argument("--history", default="")
    # verification breakdown for the job summary, kept out of the PR comment
    parser.add_argument("--details", default="")
    # The believed values, written in `go test -bench` shape. The default branch
    # stores them as the baseline, so a one-off spike in its own run does not
    # become the number every following PR is judged against.
    parser.add_argument("--save-verified", default="")
    # Report currently posted on the PR. Its fingerprint decides `changed`, which is
    # what keeps a commit that moved nothing from producing a second comment.
    parser.add_argument("--previous", default="")
    parser.add_argument("--tolerance", type=factor, default=0.25)
    args = parser.parse_args(argv)
    improve_threshold = args.improve_threshold or args.threshold

    baseline_sha = args.baseline_sha
    if args.base:
        with open(args.base, encoding="utf-8") as handle:
            base, _ = parse(handle)
    else:
        base, baseline_sha = history_baseline(args.history, args.baseline_cpu)
        if not base:
            # the action reads this marker and reports "comparison skipped"
            print("baseline=none")
            return 0
    with open(args.current, encoding="utf-8") as handle:
        current, duplicates = parse(handle)
    noise = read_history(args.history) if args.history else {}
    base = align_packages(base, current)
    noise = align_packages(noise, current)

    worse, better = compare(base, current, args.threshold, improve_threshold, args.min_ns, noise)

    previous = ""
    if args.previous and os.path.exists(args.previous):
        with open(args.previous, encoding="utf-8") as handle:
            previous = handle.read()
    previous_digest = read_digest(previous)
    reported = previous_digest or {}

    # Everything a single measurement claims before it may touch the comment: fresh
    # findings in either direction, and results the posted report names that have
    # supposedly moved since (a "fixed" regression is just as often the runner).
    flagged_worse = {c.key for c in worse}
    flagged_better = {c.key for c in better}
    stale = set()
    disowned = set()
    for text, before in reported.items():
        key = text_key(text)
        if key is None:
            continue
        # a posted finding whose reported strength would not be flagged under
        # today's bars (noise floors, raised thresholds) is a claim to retract,
        # not a number to keep refreshing
        bar, improve_bar = bars(key, args.threshold, improve_threshold, noise)
        if (before > 1 and before < bar) or (before and before < 1 and 1 / before < improve_bar):
            disowned.add(key)
        if (
            key in current
            and key in base
            and not near(
                before,
                rounded(ratio(key.unit, base[key], current[key])),
                slack(key, args.tolerance, noise),
            )
        ):
            stale.add(key)
    stale |= {key for key in disowned if key in current and key in base}
    to_verify = flagged_worse | flagged_better | stale

    retested = {}
    if args.retested and os.path.exists(args.retested):
        with open(args.retested, encoding="utf-8") as handle:
            # the retest runs -count=3; the median keeps a single wobble in the
            # re-measurement from deciding the verification either way
            retested = {key: statistics.median(values) for key, values in parse_runs(handle).items()}
    retest_note = ""
    first_run = current
    if retested and to_verify:
        reported_side = {}
        for text, before in reported.items():
            key = text_key(text)
            if key is not None:
                reported_side[key] = "w" if before > 1 else "b"

        def side(key, value):
            bar, improve_bar = bars(key, args.threshold, improve_threshold, noise)
            spot = ratio(key.unit, base[key], value)
            return "w" if spot >= bar else "b" if spot <= 1 / improve_bar else "-"

        def believe(key, value):
            second = retested.get(key)
            if second is None:
                return value
            # a flip with the posted report on the first measurement's side keeps
            # the first: two of three readings agree, the retest was the outlier
            if reported_side.get(key) == side(key, value) != side(key, second):
                return value
            return second

        # only the verified results take their re-measured value; overriding the
        # rest would let a fresh wobble flag something the first run cleared
        current = {
            key: believe(key, value) if key in to_verify else value
            for key, value in current.items()
        }
        worse, better = compare(base, current, args.threshold, improve_threshold, args.min_ns, noise)
        # A finding survives when both measurements of this run agree on its
        # direction, or when the posted report already said the same; a lone flip
        # (slower once, faster once) is exactly the noise the retest exists to kill.
        reported_worse = {text_key(text) for text, factor in reported.items() if factor > 1}
        reported_better = {text_key(text) for text, factor in reported.items() if factor < 1}
        worse = [c for c in worse if c.key in flagged_worse | reported_worse]
        better = [c for c in better if c.key in flagged_better | reported_better]
        counts = []
        if flagged_worse:
            survived = pairs({c.key for c in worse} & flagged_worse)
            counts.append(f"{len(survived)}/{len(pairs(flagged_worse))} regressions")
        if flagged_better:
            survived = pairs({c.key for c in better} & flagged_better)
            counts.append(f"{len(survived)}/{len(pairs(flagged_better))} improvements")
        parts = [", ".join(counts) + " reproduced"] if counts else []
        extra = stale - flagged_worse - flagged_better
        if extra:
            parts.append(f"{len(pairs(extra))} reported re-checked")
        retest_note = "retest: " + ", ".join(parts)
    if args.details:
        write_details(
            args.details, base, first_run, retested, to_verify,
            flagged_worse, flagged_better, worse, better,
            args.threshold, improve_threshold, noise,
        )
    if args.retest_plan:
        write_plan(args.retest_plan, [] if retested else to_verify, args.workdir)
    if args.save_verified and retested:
        with open(args.current, encoding="utf-8") as handle:
            package_times = [line for line in handle if re.match(r"ok\s+\S+\s+[\d.]+s", line)]
        save_verified(args.save_verified, current, package_times)

    shared = len(current.keys() & base.keys())
    ends = [linked(args.commit, args.repo_url)] if args.commit else []
    if args.baseline_ref:
        sha = "@" + linked(baseline_sha, args.repo_url) if baseline_sha else ""
        ends.append(args.baseline_ref + sha)
    notes = [" vs ".join(ends)] if ends else []
    notes.append(f"{shared}/{len(current)} results compared")
    added = len(current.keys() - base.keys())
    removed = len(base.keys() - current.keys())
    if added or removed:
        notes.append(f"{added} new, {removed} gone")
    if duplicates:
        notes.append(f"⚠️ {duplicates} measured twice")
    if retest_note:
        notes.append(retest_note)
    if noise:
        notes.append("noise-aware thresholds")
    if args.hardware:
        notes.append(args.hardware)
    if args.run_url:
        # where the capped tables and the raw `go test -bench` numbers live
        notes.append(f"[full results]({args.run_url})")

    report = render(worse, better, args.threshold, improve_threshold, args.limit, notes)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(report)

    changed = bool(disowned) or not nothing_new(
        previous_digest, digest(worse + better), factors(base, current), args.tolerance, noise
    )

    for key, value in (
        ("regressions", len(worse)),
        ("improvements", len(better)),
        ("regressed", "true" if worse else "false"),
        ("significant", "true" if worse or better else "false"),
        ("changed", "true" if changed else "false"),
        # visible in the step log: an empty noise map means flat thresholds
        ("noise", len(noise)),
    ):
        print(f"{key}={value}")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
