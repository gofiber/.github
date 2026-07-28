#!/usr/bin/env python3
"""Self-check for shard_benchmarks: run it directly, it asserts and prints OK."""
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import shard_benchmarks as sb  # noqa: E402

LIST_OUTPUT = """\
BenchmarkA
BenchmarkB
BenchmarkShared
ok  \tgithub.com/x/root\t0.1s
?   \tgithub.com/x/notests\t[no test files]
BenchmarkC
BenchmarkShared
ok  \tgithub.com/x/sub\t0.2s
"""


def test_parse():
    pairs = sb.parse(io.StringIO(LIST_OUTPUT))
    assert pairs == [
        ("github.com/x/root", "BenchmarkA"),
        ("github.com/x/root", "BenchmarkB"),
        ("github.com/x/root", "BenchmarkShared"),
        ("github.com/x/sub", "BenchmarkC"),
        ("github.com/x/sub", "BenchmarkShared"),
    ], pairs
    # a package without test files must not inherit the previous package's names
    assert not any(p == "github.com/x/notests" for p, _ in pairs)


def test_every_benchmark_lands_in_exactly_one_shard():
    pairs = sb.parse(io.StringIO(LIST_OUTPUT))
    for total in (1, 2, 3, 5, 8):
        seen = []
        for i in range(total):
            seen += sb.shard(pairs, i, total)
        assert sorted(seen) == sorted(set(pairs)), (total, seen)


def test_same_name_in_two_packages_stays_separate():
    pairs = sb.parse(io.StringIO(LIST_OUTPUT))
    shared = [p for p in pairs if p[1] == "BenchmarkShared"]
    assert len(shared) == 2
    placed = [(i, p) for i in range(3) for p in sb.shard(pairs, i, 3) if p[1] == "BenchmarkShared"]
    assert len(placed) == 2, placed
    # both copies survive, each tied to its own package
    assert {p[0] for _, p in placed} == {"github.com/x/root", "github.com/x/sub"}


def test_render_is_a_per_package_anchored_regex():
    lines = list(sb.render([("github.com/x/root", "BenchmarkA"), ("github.com/x/root", "BenchmarkB")]))
    assert lines == ["github.com/x/root ^(BenchmarkA|BenchmarkB)$"], lines
    # one field for the package, one for the regex: `read -r pkg regex` in the workflow
    assert len(lines[0].split()) == 2


def test_balance():
    pairs = [(f"pkg{i % 4}", f"Benchmark{i}") for i in range(100)]
    sizes = [len(sb.shard(pairs, i, 4)) for i in range(4)]
    assert max(sizes) - min(sizes) <= 1, sizes


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
