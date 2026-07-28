#!/usr/bin/env python3
"""Partition a module's benchmarks across shards.

Reads `go test -list '^Benchmark' ./...` on stdin and writes the slice belonging
to SHARD_INDEX as one `<package> <regex>` line per package, ready to be fed into
`go test -bench <regex> <package>`.

Benchmark names are NOT unique across packages (fiber has five that repeat), so a
single regex over ./... would run those in every package that defines them and
measure them twice. Hence one invocation per package.
"""
import os
import re
import sys


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


def shard(pairs, index, total):
    """Round-robin over the sorted list: balances by count and is stable across
    shards, so every benchmark lands in exactly one of them."""
    ordered = sorted(set(pairs))
    return [p for i, p in enumerate(ordered) if i % total == index]


def render(selected):
    by_pkg = {}
    for pkg, name in selected:
        by_pkg.setdefault(pkg, []).append(name)
    for pkg in sorted(by_pkg):
        names = "|".join(re.escape(n) for n in sorted(by_pkg[pkg]))
        yield f"{pkg} ^({names})$"


def main():
    index = int(os.environ["SHARD_INDEX"])
    total = int(os.environ["SHARD_TOTAL"])
    if not 0 <= index < total:
        sys.exit(f"SHARD_INDEX {index} out of range for {total} shards")

    pairs = parse(sys.stdin)
    if not pairs:
        sys.exit("no benchmarks found in `go test -list` output")
    selected = shard(pairs, index, total)
    print(
        f"shard {index + 1}/{total}: {len(selected)} of {len(set(pairs))} benchmarks",
        file=sys.stderr,
    )
    for line in render(selected):
        print(line)


if __name__ == "__main__":
    main()
