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


def v2(names_units_values):
    import json
    rows = list(names_units_values)
    return "window.BENCHMARK_DATA = " + json.dumps({
        "version": 2, "lastUpdate": 1, "repoUrl": "r", "runs": [{"id": "a"}],
        "names": [n for n, _, _ in rows],
        "units": [u for _, u, _ in rows],
        "values": [v for _, _, v in rows],
    })


def test_weigh_counts_variants_once_no_matter_how_many_units():
    weights = sb.weigh(v2([
        ("BenchmarkBig/s1 (github.com/x/root) - ns/op", "ns/op", [100.0]),
        ("BenchmarkBig/s1 (github.com/x/root) - B/op", "B/op", [24.0]),
        ("BenchmarkBig/s2 (github.com/x/root) - ns/op", "ns/op", [100.0]),
        ("BenchmarkSmall (github.com/x/root) - ns/op", "ns/op", [100.0]),
    ]))
    # two variants weigh twice one variant; the B/op row adds nothing
    assert weights["github.com/x/root BenchmarkBig"] == 2 * weights["github.com/x/root BenchmarkSmall"], weights


def test_weigh_charges_slow_single_iterations():
    weights = sb.weigh(v2([
        ("BenchmarkSlow (github.com/x/root) - ns/op", "ns/op", [2e9]),
        ("BenchmarkFast (github.com/x/root) - ns/op", "ns/op", [100.0]),
    ]))
    # a 2s/op benchmark overshoots benchtime on every ramp-up run
    assert weights["github.com/x/root BenchmarkSlow"] > 5 * weights["github.com/x/root BenchmarkFast"], weights


def test_weigh_survives_garbage_and_legacy_data():
    assert sb.weigh("not data at all") == {}
    assert sb.weigh('window.BENCHMARK_DATA = {"entries": {"Benchmark": []}}') == {}
    # names without a package suffix cannot be matched to `go test -list` pairs
    assert sb.weigh(v2([("BenchmarkBare - ns/op", "ns/op", [1.0])])) == {}


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


def test_weigh_finds_the_ns_op_row_wherever_it_sits():
    # trim and re-adding can reorder rows; the overshoot must not depend on the
    # ns/op row coming first for its variant
    weights = sb.weigh(v2([
        ("BenchmarkSlow (github.com/x/root) - B/op", "B/op", [24.0]),
        ("BenchmarkSlow (github.com/x/root) - ns/op", "ns/op", [2e9]),
    ]))
    assert weights["github.com/x/root BenchmarkSlow"] > 5, weights


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
