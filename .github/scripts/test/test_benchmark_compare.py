#!/usr/bin/env python3
"""Self-check for the benchmark-report compare script: run it directly, it prints OK."""
import io
import pathlib
import sys
import tempfile

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1].parent / "actions" / "benchmark-report")
)
import compare  # noqa: E402

OUTPUT = """\
goos: linux
goarch: arm64
pkg: github.com/gofiber/fiber/v3
Benchmark_NewError-4                	19444994	        58.00 ns/op	      24 B/op	       1 allocs/op
BenchmarkAppendMsg-4                	74023837	        16.19 ns/op	1977.09 MB/s	       0 B/op	       0 allocs/op
Benchmark_Ctx_Get/header-8-4        	 1000000	         0.50 ns/op
PASS
ok  	github.com/gofiber/fiber/v3	3.6s
goos: linux
pkg: github.com/gofiber/fiber/v3/binder
Benchmark_NewError-4                	 1000000	      1118 ns/op	     160 B/op	       8 allocs/op
PASS
"""


def parsed(text=OUTPUT):
    return compare.parse(io.StringIO(text))[0]


def test_parse_keys_on_package_name_and_unit():
    results = parsed()
    assert results[compare.Key("github.com/gofiber/fiber/v3", "Benchmark_NewError", "ns/op")] == 58.0
    # same benchmark name in two packages must not collide
    assert results[compare.Key("github.com/gofiber/fiber/v3/binder", "Benchmark_NewError", "ns/op")] == 1118.0
    assert results[compare.Key("github.com/gofiber/fiber/v3", "Benchmark_NewError", "B/op")] == 24.0
    assert results[compare.Key("github.com/gofiber/fiber/v3", "Benchmark_NewError", "allocs/op")] == 1.0


def test_parse_strips_only_the_gomaxprocs_suffix():
    names = {key.name for key in parsed()}
    # the -8 belongs to the subtest, the -4 is GOMAXPROCS
    assert "Benchmark_Ctx_Get/header-8" in names, names


def test_parse_rejects_lines_with_an_odd_tail():
    assert not parsed("pkg: x\nBenchmarkBroken-4  100  58.00 ns/op  24\n")


def test_parse_counts_duplicates():
    doubled = OUTPUT + "Benchmark_NewError-4\t10\t99.00 ns/op\n"
    assert compare.parse(io.StringIO(doubled))[1] == 1


def test_direction_is_per_unit_not_per_tool():
    # the whole point: MB/s going up is an improvement, ns/op going up is not
    assert compare.bigger_is_better("MB/s")
    assert not compare.bigger_is_better("ns/op")
    assert not compare.bigger_is_better("B/op")
    assert not compare.bigger_is_better("allocs/op")
    assert compare.ratio("MB/s", 100, 200) == 0.5
    assert compare.ratio("ns/op", 100, 200) == 2.0


def test_throughput_gain_is_not_reported_as_a_regression():
    key = compare.Key("p", "B", "MB/s")
    worse, better = compare.compare({key: 1000.0}, {key: 2000.0}, 1.5, 1.1, 1.0)
    assert not worse, worse
    assert [c.key for c in better] == [key]


def test_throughput_loss_is_reported():
    # this is what stripping MB/s used to hide
    key = compare.Key("p", "B", "MB/s")
    worse, _ = compare.compare({key: 2000.0}, {key: 1000.0}, 1.5, 1.1, 1.0)
    assert [c.key for c in worse] == [key]
    assert worse[0].ratio == 2.0


def test_dropping_to_zero_is_an_improvement_not_a_regression():
    # allocs 1 -> 0 and 0 -> 1 both divide by zero, they must not land in the same bucket
    key = compare.Key("p", "B", "allocs/op")
    worse, better = compare.compare({key: 1.0}, {key: 0.0}, 1.5, 1.1, 1.0)
    assert not worse and [c.key for c in better] == [key]
    worse, better = compare.compare({key: 0.0}, {key: 1.0}, 1.5, 1.1, 1.0)
    assert [c.key for c in worse] == [key] and not better
    # and the same for a throughput metric, where the direction is flipped
    rate = compare.Key("p", "B", "MB/s")
    worse, better = compare.compare({rate: 100.0}, {rate: 0.0}, 1.5, 1.1, 1.0)
    assert [c.key for c in worse] == [rate] and not better


def test_a_zero_ratio_still_renders():
    key = compare.Key("p", "B", "allocs/op")
    _, better = compare.compare({key: 1.0}, {key: 0.0}, 1.5, 1.1, 1.0)
    assert "inf" in "\n".join(compare.table(better, "Better by", 10, True))


def test_slower_nanoseconds_are_reported():
    key = compare.Key("p", "B", "ns/op")
    worse, better = compare.compare({key: 100.0}, {key: 200.0}, 1.5, 1.1, 1.0)
    assert [c.key for c in worse] == [key]
    assert not better


def test_min_ns_drops_every_unit_of_that_benchmark():
    ns = compare.Key("p", "B", "ns/op")
    allocs = compare.Key("p", "B", "allocs/op")
    base = {ns: 0.3, allocs: 1.0}
    current = {ns: 0.9, allocs: 10.0}
    worse, _ = compare.compare(base, current, 1.5, 1.1, 1.0)
    assert not worse, worse
    # above the floor the same pair is compared normally
    worse, _ = compare.compare(base, {ns: 2.0, allocs: 10.0}, 1.5, 1.1, 1.0)
    assert {c.key for c in worse} == {ns, allocs}


def test_benchmarks_missing_on_one_side_are_skipped():
    key = compare.Key("p", "Gone", "ns/op")
    worse, better = compare.compare({key: 1.0}, {compare.Key("p", "New", "ns/op"): 500.0}, 1.5, 1.1, 1.0)
    assert not worse and not better


def test_report_lists_improvements_and_exit_code_only_follows_regressions():
    faster = "pkg: p\nBenchmarkFast-4\t10\t50.00 ns/op\n"
    slower = "pkg: p\nBenchmarkFast-4\t10\t100.00 ns/op\n"
    with tempfile.TemporaryDirectory() as tmp:
        base, current, out = (f"{tmp}/{n}" for n in ("base.txt", "cur.txt", "out.md"))
        for path, text in ((base, slower), (current, faster)):
            pathlib.Path(path).write_text(text, encoding="utf-8")
        code = compare.main(["--base", base, "--current", current, "--out", out])
        report = pathlib.Path(out).read_text(encoding="utf-8")
        assert code == 0, report
        assert "Improvement" in report and "2.00x" in report, report
        # and the other way round
        code = compare.main(["--base", current, "--current", base, "--out", out])
        report = pathlib.Path(out).read_text(encoding="utf-8")
        assert code == 1
        assert "Performance Alert" in report and "Regressions" in report, report


def test_empty_improve_threshold_mirrors_the_alert_threshold():
    # a 1.4x win must stay quiet when a 1.4x loss would not be believed either
    faster = "pkg: p\nBenchmarkFast-4\t10\t71.00 ns/op\n"
    slower = "pkg: p\nBenchmarkFast-4\t10\t100.00 ns/op\n"
    with tempfile.TemporaryDirectory() as tmp:
        base, current, out = (f"{tmp}/{n}" for n in ("base.txt", "cur.txt", "out.md"))
        pathlib.Path(base).write_text(slower, encoding="utf-8")
        pathlib.Path(current).write_text(faster, encoding="utf-8")
        argv = ["--base", base, "--current", current, "--out", out, "--threshold", "150%"]
        compare.main(argv + ["--improve-threshold", ""])
        assert "No significant change" in pathlib.Path(out).read_text(encoding="utf-8")
        # an explicit value still wins over the mirror
        compare.main(argv + ["--improve-threshold", "125%"])
        assert "Improvement" in pathlib.Path(out).read_text(encoding="utf-8")


def test_report_caps_the_table_so_the_comment_fits_github():
    many = "pkg: p\n" + "".join(f"Benchmark{i}-4\t10\t10.00 ns/op\n" for i in range(200))
    slow = "pkg: p\n" + "".join(f"Benchmark{i}-4\t10\t100.00 ns/op\n" for i in range(200))
    with tempfile.TemporaryDirectory() as tmp:
        base, current, out = (f"{tmp}/{n}" for n in ("base.txt", "cur.txt", "out.md"))
        pathlib.Path(base).write_text(many, encoding="utf-8")
        pathlib.Path(current).write_text(slow, encoding="utf-8")
        compare.main(["--base", base, "--current", current, "--out", out])
        report = pathlib.Path(out).read_text(encoding="utf-8")
        assert "and 150 more" in report, report
        assert len(report) < 65536, len(report)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
