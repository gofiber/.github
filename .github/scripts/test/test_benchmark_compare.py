#!/usr/bin/env python3
"""Self-check for the benchmark-report compare script: run it directly, it prints OK."""
import contextlib
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


def bench(**benchmarks):
    """`BenchmarkX="100 ns/op"` into something that looks like `go test -bench` output."""
    lines = ["pkg: p"] + [f"{name}-4\t10\t{result}" for name, result in benchmarks.items()]
    return "\n".join(lines) + "\n"


def run(base, current, previous=None, extra=()):
    """Run the script end to end: exit code, report and the key=value lines it printed."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: f"{tmp}/{name}" for name in ("base", "current", "out", "previous")}
        pathlib.Path(paths["base"]).write_text(base, encoding="utf-8")
        pathlib.Path(paths["current"]).write_text(current, encoding="utf-8")
        argv = ["--base", paths["base"], "--current", paths["current"], "--out", paths["out"]]
        if previous is not None:
            pathlib.Path(paths["previous"]).write_text(previous, encoding="utf-8")
            argv += ["--previous", paths["previous"]]
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = compare.main(argv + list(extra))
        outputs = dict(line.split("=", 1) for line in printed.getvalue().splitlines())
        return code, pathlib.Path(paths["out"]).read_text(encoding="utf-8"), outputs


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
    rows = compare.group(better, True)
    assert "∞x" in "\n".join(compare.table(rows, 10, True, 1.5, {}))


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
    slower, faster = bench(BenchmarkFast="100.00 ns/op"), bench(BenchmarkFast="50.00 ns/op")
    code, report, outputs = run(slower, faster)
    assert code == 0, report
    assert "1 benchmark faster" in report and "2.00x" in report, report
    assert outputs["regressed"] == "false" and outputs["significant"] == "true"
    # and the other way round
    code, report, outputs = run(faster, slower)
    assert code == 1
    assert "1 benchmark slower" in report and "2.00x" in report, report
    assert outputs["regressed"] == "true" and outputs["regressions"] == "1"


def test_empty_improve_threshold_mirrors_the_alert_threshold():
    # a 1.4x win must stay quiet when a 1.4x loss would not be believed either
    slower, faster = bench(BenchmarkFast="100.00 ns/op"), bench(BenchmarkFast="71.00 ns/op")
    _, report, _ = run(slower, faster, extra=["--threshold", "150%", "--improve-threshold", ""])
    assert "No significant benchmark change" in report
    # an explicit value still wins over the mirror
    _, report, _ = run(slower, faster, extra=["--threshold", "150%", "--improve-threshold", "125%"])
    assert "faster" in report, report


def test_report_caps_the_table_so_the_comment_fits_github():
    fast = bench(**{f"Benchmark{i}": "10.00 ns/op" for i in range(200)})
    slow = bench(**{f"Benchmark{i}": "100.00 ns/op" for i in range(200)})
    _, report, _ = run(fast, slow)
    assert "and 185 more" in report, report
    assert len(report) < 65536, len(report)


def test_the_units_of_one_benchmark_share_a_single_row():
    base = bench(BenchmarkX="100 ns/op\t24 B/op\t2 allocs/op")
    current = bench(BenchmarkX="300 ns/op\t72 B/op\t6 allocs/op")
    _, report, outputs = run(base, current)
    assert outputs["regressions"] == "3"
    assert report.count("| `BenchmarkX`") == 1, report
    assert "100 → 300 ns/op · 24 → 72 B/op · 2 → 6 allocs/op" in report, report
    assert "1 benchmark slower" in report, report


def test_the_report_shows_the_direction_at_a_glance():
    base = bench(BenchmarkUp="100 ns/op", BenchmarkDown="100 ns/op")
    current = bench(BenchmarkUp="300 ns/op", BenchmarkDown="20 ns/op")
    _, report, _ = run(base, current)
    # regressions are the half that needs acting on, so they are the half left open
    assert "<details open>\n<summary>❗❗ <b>1 benchmark slower</b> (3.00x)" in report, report
    assert "<details>\n<summary>⚡⚡ <b>1 benchmark faster</b> (5.00x)" in report, report


def test_marks_scale_with_the_size_of_the_change():
    assert compare.marks(1.5, 1.5, "!") == "!"
    assert compare.marks(2.9, 1.5, "!") == "!"
    assert compare.marks(3.0, 1.5, "!") == "!!"
    assert compare.marks(6.0, 1.5, "!") == "!!!"
    assert compare.marks(60.0, 1.5, "!") == "!!!"
    assert compare.marks(float("inf"), 1.5, "!") == "!!!"


def test_package_labels_strip_the_shared_module_path():
    root, binder = "github.com/gofiber/fiber/v3", "github.com/gofiber/fiber/v3/binder"
    assert compare.package_labels({root, binder}) == {root: "v3", binder: "v3/binder"}
    # a single package is the whole report, the footer names it instead of every row
    assert compare.package_labels({root}) == {}


def test_the_same_findings_with_wobbling_numbers_do_not_count_as_changed():
    base = bench(BenchmarkX="100 ns/op")
    _, report, _ = run(base, bench(BenchmarkX="300 ns/op"))
    # what comes back from the API is the comment, marker and all
    first = f"<!-- benchmark-report:. -->\n\n{report}"
    # 3.00x -> 3.30x is the runner, not the commit
    _, _, outputs = run(base, bench(BenchmarkX="330 ns/op"), previous=first)
    assert outputs["changed"] == "false"
    _, _, outputs = run(base, bench(BenchmarkX="900 ns/op"), previous=first)
    assert outputs["changed"] == "true"


def test_a_finding_appearing_or_disappearing_counts_as_changed():
    base = bench(BenchmarkX="100 ns/op", BenchmarkY="100 ns/op")
    one = bench(BenchmarkX="300 ns/op", BenchmarkY="100 ns/op")
    both = bench(BenchmarkX="300 ns/op", BenchmarkY="300 ns/op")
    _, first, _ = run(base, one)
    _, second, outputs = run(base, both, previous=first)
    assert outputs["changed"] == "true"
    # and the other way round, once a benchmark is fixed it drops out of the list
    _, _, outputs = run(base, one, previous=second)
    assert outputs["changed"] == "true"


def test_a_clean_run_after_a_clean_run_has_nothing_to_say():
    base = bench(BenchmarkX="100 ns/op")
    _, clean, outputs = run(base, bench(BenchmarkX="101 ns/op"))
    assert outputs["significant"] == "false" and outputs["changed"] == "true"
    _, _, outputs = run(base, bench(BenchmarkX="102 ns/op"), previous=clean)
    assert outputs["significant"] == "false" and outputs["changed"] == "false"


def test_a_report_without_a_fingerprint_counts_as_changed():
    base = bench(BenchmarkX="100 ns/op")
    _, _, outputs = run(base, bench(BenchmarkX="300 ns/op"), previous="## Performance Alert\n")
    assert outputs["changed"] == "true"


def test_too_many_findings_carry_no_fingerprint():
    count = compare.FINGERPRINT_MAX + 1
    fast = bench(**{f"Benchmark{i}": "10.00 ns/op" for i in range(count)})
    slow = bench(**{f"Benchmark{i}": "100.00 ns/op" for i in range(count)})
    _, report, _ = run(fast, slow)
    assert compare.read_digest(report) is None, "fingerprint would outweigh the report"
    # so a rerun of the very same numbers still counts as changed
    _, _, outputs = run(fast, slow, previous=report)
    assert outputs["changed"] == "true"


def test_an_unreadable_fingerprint_counts_as_changed():
    # a report is a comment, and a comment is something anyone can edit into nonsense
    assert compare.read_digest("<!-- benchmark-fingerprint\np|B|ns/op wat\n-->") is None
    assert compare.read_digest("<!-- benchmark-fingerprint\nhalf a line\n-->") is None
    assert compare.read_digest("<!-- benchmark-fingerprint\np|B|ns/op 2\n-->") == {"p|B|ns/op": 2.0}


def test_a_result_sitting_on_the_threshold_does_not_flap():
    base = bench(BenchmarkX="100 ns/op")
    _, report, outputs = run(base, bench(BenchmarkX="152 ns/op"))
    assert outputs["significant"] == "true"
    # dropping to 1.45x is noise, not a fix, and must not shuffle the comment around
    _, _, outputs = run(base, bench(BenchmarkX="145 ns/op"), previous=report)
    assert outputs["significant"] == "false" and outputs["changed"] == "false"
    # actually fixing it is news again
    _, _, outputs = run(base, bench(BenchmarkX="100 ns/op"), previous=report)
    assert outputs["changed"] == "true"


def test_a_benchmark_that_is_gone_counts_as_changed():
    base = bench(BenchmarkX="100 ns/op", BenchmarkY="100 ns/op")
    _, report, _ = run(base, bench(BenchmarkX="300 ns/op", BenchmarkY="100 ns/op"))
    # renamed, deleted or simply not run: the report cannot be trusted either way
    _, _, outputs = run(base, bench(BenchmarkY="100 ns/op"), previous=report)
    assert outputs["changed"] == "true"


def test_the_headline_carries_the_worst_case():
    base = bench(BenchmarkX="100 ns/op", BenchmarkY="100 ns/op")
    current = bench(BenchmarkX="200 ns/op", BenchmarkY="700 ns/op")
    _, report, _ = run(base, current)
    # visible without expanding anything: how many, how bad
    assert "❗❗ <b>2 benchmarks slower</b> (up to 7.00x)" in report, report


def test_metrics_keep_the_order_go_prints_them_in():
    base = bench(BenchmarkX="100 ns/op\t24 B/op")
    current = bench(BenchmarkX="200 ns/op\t240 B/op")
    _, report, _ = run(base, current)
    # B/op moved further, but a row that reshuffles by magnitude is a row nobody can scan
    assert "100 → 200 ns/op · 24 → 240 B/op" in report, report


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
