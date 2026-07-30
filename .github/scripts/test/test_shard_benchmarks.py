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


BASELINE = """\
goos: linux
goarch: arm64
pkg: github.com/x/root
Benchmark_Big/a-4\t10\t100.00 ns/op\t24 B/op\t1 allocs/op
Benchmark_Big/b-4\t10\t100.00 ns/op
Benchmark_Big/c-4\t10\t100.00 ns/op
Benchmark_Small-4\t10\t100.00 ns/op
PASS
ok  \tgithub.com/x/root\t30.0s
pkg: github.com/x/proxy
Benchmark_Fast-4\t10\t50.00 ns/op
ok  \tgithub.com/x/proxy\t40.0s
pkg: github.com/x/proxy
Benchmark_Fast2-4\t10\t50.00 ns/op
ok  \tgithub.com/x/proxy\t34.0s
pkg: github.com/x/silent
Benchmark_NoOk-4\t10\t50.00 ns/op
"""


def test_weigh_splits_measured_package_seconds_by_variant_share():
    weights = sb.weigh(BASELINE)
    # root measured 30s: Big has 3 of 4 variants, Small 1 of 4. The wall time
    # covers setup that ns/op never shows, which is the whole point.
    assert weights["github.com/x/root Benchmark_Big"] == 22.5, weights
    assert weights["github.com/x/root Benchmark_Small"] == 7.5, weights


def test_weigh_sums_ok_lines_across_shards():
    weights = sb.weigh(BASELINE)
    # the merged baseline carries one ok line per shard that ran the package
    assert weights["github.com/x/proxy Benchmark_Fast"] == 37.0, weights
    assert weights["github.com/x/proxy Benchmark_Fast2"] == 37.0, weights


def test_weigh_skips_packages_without_a_measured_total():
    weights = sb.weigh(BASELINE)
    # no ok line, no guess: the benchmark falls back to the default at shard time
    assert "github.com/x/silent Benchmark_NoOk" not in weights, weights


def test_weigh_survives_garbage():
    assert sb.weigh("") == {}
    assert sb.weigh("not benchmark output at all\nok broken") == {}


def test_weighted_shard_flattens_where_round_robin_cannot():
    pairs = [("p", f"Benchmark{i}") for i in range(8)]
    weights = {"p Benchmark0": 40.0}
    loads = []
    seen = []
    for i in range(4):
        part = sb.shard(pairs, i, 4, weights)
        seen += part
        loads.append(sum(weights.get(f"p {n}", sb.BASE_SECONDS) for _, n in part))
    # every benchmark still lands in exactly one shard
    assert sorted(seen) == sorted(set(pairs)), seen
    # the heavy one sits alone instead of dragging two more along
    heavy_shard = [part for i in range(4) for part in [sb.shard(pairs, i, 4, weights)] if ("p", "Benchmark0") in part]
    assert len(heavy_shard[0]) == 1, heavy_shard
    assert max(loads) - min(loads) < 40.0, loads


def test_new_and_deleted_benchmarks_survive_stale_weights():
    # the listing has a benchmark the history does not know (gets the default
    # weight), the history one that is gone (never scheduled, adds no load)
    pairs = [("p", "BenchmarkNew"), ("p", "BenchmarkA"), ("p", "BenchmarkB")]
    weights = {"p BenchmarkGone": 100.0, "p BenchmarkA": 2.0, "p BenchmarkB": 2.0}
    seen = []
    for i in range(2):
        seen += sb.shard(pairs, i, 2, weights)
    assert sorted(seen) == sorted(set(pairs)), seen


def test_empty_weights_keep_the_round_robin_untouched():
    pairs = sb.parse(io.StringIO(LIST_OUTPUT))
    for i in range(3):
        assert sb.shard(pairs, i, 3, {}) == sb.shard(pairs, i, 3)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
