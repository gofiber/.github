#!/usr/bin/env python3
"""Partition a module's benchmarks across shards.

Reads `go test -list '^Benchmark' ./...` on stdin and writes the slice belonging
to SHARD_INDEX as one `<package> <regex>` line per package, ready to be fed into
`go test -bench <regex> <package>`.

Benchmark names are NOT unique across packages (fiber has five that repeat), so a
single regex over ./... would run those in every package that defines them and
measure them twice. Hence one invocation per package.

`go test -list` only sees top-level functions, but their cost varies wildly: the
sub-benchmarks are registered at runtime, one function can hide fifty of them,
and setup outside the timer (proxy starts real servers) never shows in ns/op.
With WEIGHTS set (JSON from `shard_benchmarks.py weights`, fed the last merged
baseline output by the plan job), the split balances measured seconds instead of
counting functions; without it, the old round-robin stands.

A baseline alone would still guess: it smears a package's wall time evenly
over its variants, and one function hiding a minute of untimed setup (proxy's
StripHopByHop) looks as cheap as its neighbours. So each shard pipes its
`go test` output through `record`, which stamps every finished result line and
writes real seconds per top-level function; merged and published to gh-pages
as shard-timings.txt (an evictable cache lost them within hours), they are
what the plan job hands back in on the next run.
"""
import json
import math
import os
import re
import sys
import time

# a variant without history is assumed to cost about one benchtime plus ramp-up
BASE_SECONDS = 1.2
PKG_HEADER_RE = re.compile(r"^pkg:\s+(\S+)")
OK_RE = re.compile(r"^ok\s+(\S+)\s+([\d.]+)s")
# the trailing -GOMAXPROCS is split off, `go test -list` names carry none
BENCH_NAME_RE = re.compile(r"^(Benchmark\S*?)(?:-\d+)?\s+\d")


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
    """Measured seconds per top-level benchmark, from the last merged baseline.

    Returns {"<pkg> <name>": seconds}. The baseline's `ok <pkg> <secs>` lines
    carry each package's real wall time - including the setup that ns/op never
    shows - summed across the shards that ran it, and split over the package's
    top-level benchmarks by their share of variants. Anything that fails to
    parse yields {}, which downgrades the split to the count-balanced one.
    """
    pkg = ""
    seconds = {}
    tops = {}
    for line in text.splitlines():
        header = PKG_HEADER_RE.match(line)
        if header:
            pkg = header.group(1)
            continue
        finished = OK_RE.match(line)
        if finished:
            seconds[finished.group(1)] = seconds.get(finished.group(1), 0.0) + float(finished.group(2))
            continue
        bench = BENCH_NAME_RE.match(line)
        if bench and pkg:
            name = bench.group(1)
            tops.setdefault(pkg, {}).setdefault(name.partition("/")[0], set()).add(name)
    weights = {}
    for pkg, benchmarks in tops.items():
        total = seconds.get(pkg)
        count = sum(len(variants) for variants in benchmarks.values())
        if not total or not count:
            continue
        for top, variants in benchmarks.items():
            weights[f"{pkg} {top}"] = round(total * len(variants) / count, 3)
    return weights


def record(pkg, out_path, stream=sys.stdin, sink=sys.stdout, clock=time.monotonic):
    """Tee `go test` output through unchanged, appending `<secs> <pkg> <top>` lines.

    A result line completes when its benchmark finishes, so the delta since the
    last event covers the run plus any setup before it. Compile time falls before
    the first line and is never attributed. Go writes stdout unbuffered, so the
    stamps are taken as the benchmarks actually finish, not when a buffer flushes.
    """
    tops, order, prev = {}, [], None
    for line in stream:
        now = clock()
        if prev is None:
            prev = now
        m = BENCH_NAME_RE.match(line)
        if m:
            top = m.group(1).partition("/")[0]
            if top not in tops:
                tops[top] = 0.0
                order.append(top)
            tops[top] += now - prev
            prev = now
        elif OK_RE.match(line) or PKG_HEADER_RE.match(line):
            prev = now
        sink.write(line)
        sink.flush()
    with open(out_path, "a", encoding="utf-8") as handle:
        for top in order:
            handle.write(f"{tops[top]:.3f} {pkg} {top}\n")


def read_timings(path):
    """{"<pkg> <top>": secs} from recorded lines; malformed lines are dropped.

    The floor keeps a mismeasured near-zero from letting LPT stack every "free"
    benchmark onto whichever shard happens to be lightest at the time.
    """
    timings = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) != 3:
                    continue
                try:
                    secs = float(parts[0])
                except ValueError:
                    continue
                key = f"{parts[1]} {parts[2]}"
                timings[key] = timings.get(key, 0.0) + max(secs, 0.1)
    except OSError:
        return {}
    return timings


def combined_weights(baseline_text, timings_path=None):
    """Baseline guess for every benchmark, overridden by measured wall seconds."""
    weights = weigh(baseline_text)
    if timings_path:
        weights.update(read_timings(timings_path))
    return weights


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
    if sys.argv[1:2] == ["weights"]:
        # plan job: the last merged baseline output on stdin, optionally a recorded
        # timings file as argv[2] (missing file just means no overrides), JSON out
        timings = sys.argv[2] if len(sys.argv) > 2 and os.path.isfile(sys.argv[2]) else None
        print(json.dumps(combined_weights(sys.stdin.read(), timings), separators=(",", ":"), sort_keys=True))
        return
    if sys.argv[1:2] == ["record"]:
        # shard job: `go test ... | record <pkg> <timings-file> | tee ...`
        # test output is not guaranteed clean UTF-8, and the old plain-tee pipe
        # was byte-transparent; a stray byte must not take the whole shard down
        sys.stdin.reconfigure(errors="replace")
        sys.stdout.reconfigure(errors="replace")
        record(sys.argv[2], sys.argv[3])
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
