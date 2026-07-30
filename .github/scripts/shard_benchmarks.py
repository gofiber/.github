#!/usr/bin/env python3
"""Partition a module's benchmarks across shards.

Reads `go test -list '^Benchmark' ./...` on stdin and writes the slice belonging
to SHARD_INDEX as one `<package> <regex>` line per package, ready to be fed into
`go test -bench <regex> <package>`.

Benchmark names are NOT unique across packages (fiber has five that repeat), so a
single regex over ./... would run those in every package that defines them and
measure them twice. Hence one invocation per package.

`go test -list` only sees top-level functions, but their cost varies wildly: the
sub-benchmarks are registered at runtime and one function can hide fifty of them.
With WEIGHTS set (JSON from `shard_benchmarks.py weights`, fed the published
gh-pages data by the plan job), the split balances estimated seconds instead of
counting functions; without it, the old round-robin stands.
"""
import json
import math
import os
import re
import sys

# a variant is roughly benchtime plus ramp-up; slow single iterations overshoot
BASE_SECONDS = 1.2
METRIC_SUFFIX_RE = re.compile(r" - [^ ]*/[^ ]*$")
PKG_SUFFIX_RE = re.compile(r" \(([^()]+)\)$")


def parse(stream):
    """`go test -list` prints the matching names per package, then its `ok <path>`
    line; `?` marks a package without test files."""
    pairs, pending = [], []
    for line in stream:
        line = line.rstrip("\n")
        if line.startswith("ok "):
            parts = line.split()
            if len(parts) >= 2:
                pairs += [(parts[1], name) for name in pending]
            pending = []
        elif line.startswith("?"):
            pending = []
        elif line.startswith("Benchmark"):
            pending.append(line.strip())
    return pairs


def weigh(text):
    """Estimated seconds per top-level benchmark, from the published v2 data.

    Returns {"<pkg> <name>": seconds}. Anything that fails to parse simply
    yields {}, which downgrades the split to the count-balanced one.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return {}
    if data.get("version") != 2:
        return {}
    variants = {}
    for row, name in enumerate(data.get("names", [])):
        series = METRIC_SUFFIX_RE.sub("", name)
        pkg_match = PKG_SUFFIX_RE.search(series)
        if not pkg_match:
            continue
        pkg = pkg_match.group(1)
        bare = series[: pkg_match.start()]
        # one weight contribution per variant, no matter how many units it carries;
        # the ns/op row may sit anywhere among them
        variants.setdefault((pkg, bare), BASE_SECONDS)
        if data["units"][row] == "ns/op":
            values = [v for v in data["values"][row] if v is not None]
            if values:
                # a slow single iteration blows past benchtime on every ramp-up run
                variants[(pkg, bare)] = BASE_SECONDS + 3 * values[-1] / 1e9
    weights = {}
    for (pkg, bare), seconds in variants.items():
        base, _, _ = bare.partition("/")
        key = f"{pkg} {base}"
        weights[key] = weights.get(key, 0.0) + seconds
    return {key: round(seconds, 3) for key, seconds in weights.items()}


def shard(pairs, index, total, weights=None):
    """Deterministic split, identical on every shard, each benchmark in exactly one.

    With weights: greedy longest-first onto the lightest shard. Without: round-robin
    over the sorted list, which balances by count.
    """
    ordered = sorted(set(pairs))
    if not weights:
        return [p for i, p in enumerate(ordered) if i % total == index]
    loads = [0.0] * total
    mine = []
    heaviest = sorted(ordered, key=lambda p: (-weights.get(f"{p[0]} {p[1]}", BASE_SECONDS), p))
    for pkg, name in heaviest:
        target = min(range(total), key=lambda s: (loads[s], s))
        loads[target] += weights.get(f"{pkg} {name}", BASE_SECONDS)
        if target == index:
            mine.append((pkg, name))
    spread = max(loads) - min(loads) if loads else 0.0
    print(f"estimated shard seconds: {[math.ceil(l) for l in loads]} (spread {spread:.0f}s)", file=sys.stderr)
    return mine


def render(selected):
    by_pkg = {}
    for pkg, name in selected:
        by_pkg.setdefault(pkg, []).append(name)
    for pkg in sorted(by_pkg):
        names = "|".join(re.escape(n) for n in sorted(by_pkg[pkg]))
        yield f"{pkg} ^({names})$"


def main():
    if sys.argv[1:] == ["weights"]:
        # plan job: published data.js on stdin, weights JSON on stdout
        print(json.dumps(weigh(sys.stdin.read()), separators=(",", ":"), sort_keys=True))
        return

    index = int(os.environ["SHARD_INDEX"])
    total = int(os.environ["SHARD_TOTAL"])
    if not 0 <= index < total:
        sys.exit(f"SHARD_INDEX {index} out of range for {total} shards")
    try:
        weights = json.loads(os.environ.get("WEIGHTS") or "{}")
    except ValueError:
        weights = {}
    if not isinstance(weights, dict):
        weights = {}

    pairs = parse(sys.stdin)
    if not pairs:
        sys.exit("no benchmarks found in `go test -list` output")
    selected = shard(pairs, index, total, weights)
    mode = " (count-balanced)"
    if weights:
        # new or renamed benchmarks carry no history yet: they default to one
        # average variant and can leave a shard long until the next publish
        unknown = sum(1 for pkg, name in set(pairs) if f"{pkg} {name}" not in weights)
        mode = f" (time-balanced, {unknown} without history)"
    print(
        f"shard {index + 1}/{total}: {len(selected)} of {len(set(pairs))} benchmarks{mode}",
        file=sys.stderr,
    )
    for line in render(selected):
        print(line)


if __name__ == "__main__":
    main()
