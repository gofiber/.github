#!/usr/bin/env python3
"""Compare two `go test -bench` runs and report regressions and improvements.

Replaces the alerting half of github-action-benchmark, which decides the "better"
direction per tool rather than per unit and reads every Go metric as
smaller-is-better. A throughput gain in MB/s therefore showed up as a regression,
which was worked around by stripping MB/s before comparing - and that hid real
throughput regressions along with it. Direction is decided per unit here.

Writes a markdown report to the path in --out and exits 1 when a regression
crosses the threshold.
"""
import argparse
import re
import sys
from collections import namedtuple

# Non-greedy name so the trailing -GOMAXPROCS is split off even when the
# benchmark name itself ends in -<digits> (subtests like Benchmark_X/n-8).
BENCH_RE = re.compile(r"^(?P<name>Benchmark\S*?)(?:-\d+)?[ \t]+\d+[ \t]+(?P<rest>\S.*?)[ \t]*$")
PKG_RE = re.compile(r"^pkg:[ \t]+(?P<pkg>\S+)")

Key = namedtuple("Key", "pkg name unit")
Change = namedtuple("Change", "key base current ratio")


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


def table(changes, caption, limit, invert):
    lines = [
        f"| Benchmark | Package | Base | Current | {caption} |",
        "|-|-|-|-|-|",
    ]
    for change in changes[:limit]:
        factor = (1 / change.ratio if change.ratio else float("inf")) if invert else change.ratio
        lines.append(
            f"| `{change.key.name}` | `{change.key.pkg}` |"
            f" {number(change.base)} {change.key.unit} |"
            f" {number(change.current)} {change.key.unit} | {factor:.2f}x |"
        )
    if len(changes) > limit:
        lines.append(f"| _and {len(changes) - limit} more_ | | | | |")
    return lines


def number(value):
    return f"{value:.2f}".rstrip("0").rstrip(".") if value < 1e6 else f"{value:.0f}"


def render(worse, better, threshold, improve_threshold, limit, hardware, notes):
    if worse:
        lines = ["## :warning: Performance Alert", ""]
        lines.append(
            f"{len(worse)} result(s) got at least {threshold:.2f}x worse than the base branch."
        )
    elif better:
        lines = ["## :rocket: Performance Improvement", ""]
        lines.append("No regressions, and some results got measurably faster.")
    else:
        lines = ["## Benchmark Report", "", "No significant change."]

    if worse:
        lines += ["", "### Regressions", ""] + table(worse, "Worse by", limit, False)
    if better:
        lines += [
            "",
            f"### Improvements (at least {improve_threshold:.2f}x)",
            "",
        ] + table(better, "Better by", limit, True)
    if hardware:
        lines += ["", f"Measured on {hardware}."]
    lines += ["", *notes]
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
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--hardware", default="")
    args = parser.parse_args(argv)
    improve_threshold = args.improve_threshold or args.threshold

    with open(args.base, encoding="utf-8") as handle:
        base, _ = parse(handle)
    with open(args.current, encoding="utf-8") as handle:
        current, duplicates = parse(handle)

    worse, better = compare(base, current, args.threshold, improve_threshold, args.min_ns)

    shared = len(current.keys() & base.keys())
    notes = [f"Compared {shared} of {len(current)} results against the base branch."]
    added = len(current.keys() - base.keys())
    removed = len(base.keys() - current.keys())
    if added or removed:
        notes.append(f"{added} new, {removed} gone, both skipped.")
    if duplicates:
        notes.append(f":warning: {duplicates} result(s) were measured more than once.")

    report = render(
        worse, better, args.threshold, improve_threshold, args.limit, args.hardware, notes
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(report)
    print(f"regressions={len(worse)} improvements={len(better)}")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
