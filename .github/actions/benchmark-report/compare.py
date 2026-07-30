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
import math
import os
import re
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

Key = namedtuple("Key", "pkg name unit")
Change = namedtuple("Change", "key base current ratio")
Row = namedtuple("Row", "pkg name strength changes")


def bigger_is_better(unit):
    """Go's convention: per-operation costs end in /op, throughput rates in /s."""
    return unit.endswith("/s")


def parse(stream):
    """Read benchmark output into {(pkg, name, unit): value}."""
    results = {}
    duplicates = 0
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
            key = Key(pkg, match.group("name"), unit)
            if key in results:
                duplicates += 1
            results[key] = number
    return results, duplicates


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


def compare(base, current, threshold, improve_threshold, min_ns):
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
        change = Change(key, base[key], value, ratio(key.unit, base[key], value))
        if change.ratio >= threshold:
            worse.append(change)
        elif change.ratio <= 1 / improve_threshold:
            better.append(change)
    worse.sort(key=lambda c: -c.ratio)
    better.sort(key=lambda c: c.ratio)
    return worse, better


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


def digest(changes):
    """Digest of the findings a report named, `pkg|benchmark|unit` to the factor."""
    return {f"{c.key.pkg}|{c.key.name}|{c.key.unit}": rounded(c.ratio) for c in changes}


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


def nothing_new(previous, found, now, tolerance):
    """Whether the posted report still describes this run.

    Deliberately asymmetric: a benchmark it does not name yet is news, but one it
    does name is compared against where that benchmark stands now, not against
    whether it is still over the threshold. A result sitting right on the line
    would otherwise drop in and out of the list on noise alone.
    """
    if previous is None or any(key not in previous for key in found):
        return False
    return all(near(before, now.get(key), tolerance) for key, before in previous.items())


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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--threshold", type=factor, default=1.5)
    # empty mirrors --threshold: an improvement is worth reporting exactly when a
    # regression of the same size would be, so both directions carry the same noise
    parser.add_argument("--improve-threshold", type=factor, default=None)
    parser.add_argument("--min-ns", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--hardware", default="")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--commit", default="")
    parser.add_argument("--run-url", default="")
    # Report currently posted on the PR. Its fingerprint decides `changed`, which is
    # what keeps a commit that moved nothing from producing a second comment.
    parser.add_argument("--previous", default="")
    parser.add_argument("--tolerance", type=factor, default=0.25)
    args = parser.parse_args(argv)
    improve_threshold = args.improve_threshold or args.threshold

    with open(args.base, encoding="utf-8") as handle:
        base, _ = parse(handle)
    with open(args.current, encoding="utf-8") as handle:
        current, duplicates = parse(handle)

    worse, better = compare(base, current, args.threshold, improve_threshold, args.min_ns)

    shared = len(current.keys() & base.keys())
    measured = " vs ".join(filter(None, (args.commit[:7], args.baseline)))
    notes = [measured] if measured else []
    notes.append(f"{shared}/{len(current)} results compared")
    added = len(current.keys() - base.keys())
    removed = len(base.keys() - current.keys())
    if added or removed:
        notes.append(f"{added} new, {removed} gone")
    if duplicates:
        notes.append(f"⚠️ {duplicates} measured twice")
    if args.hardware:
        notes.append(args.hardware)
    if args.run_url:
        # where the capped tables and the raw `go test -bench` numbers live
        notes.append(f"[full results]({args.run_url})")

    report = render(worse, better, args.threshold, improve_threshold, args.limit, notes)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(report)

    previous = ""
    if args.previous and os.path.exists(args.previous):
        with open(args.previous, encoding="utf-8") as handle:
            previous = handle.read()
    changed = not nothing_new(
        read_digest(previous), digest(worse + better), factors(base, current), args.tolerance
    )

    for key, value in (
        ("regressions", len(worse)),
        ("improvements", len(better)),
        ("regressed", "true" if worse else "false"),
        ("significant", "true" if worse or better else "false"),
        ("changed", "true" if changed else "false"),
    ):
        print(f"{key}={value}")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
