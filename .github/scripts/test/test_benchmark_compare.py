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


def run(base, current, previous=None, retested=None, extra=()):
    """Run the script end to end: exit code, report and the key=value lines it printed."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: f"{tmp}/{name}" for name in ("base", "current", "out", "previous", "retested")}
        pathlib.Path(paths["base"]).write_text(base, encoding="utf-8")
        pathlib.Path(paths["current"]).write_text(current, encoding="utf-8")
        argv = ["--base", paths["base"], "--current", paths["current"], "--out", paths["out"]]
        for name, text in (("previous", previous), ("retested", retested)):
            if text is not None:
                pathlib.Path(paths[name]).write_text(text, encoding="utf-8")
                argv += [f"--{name}", paths[name]]
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = compare.main(argv + list(extra))
        outputs = dict(line.split("=", 1) for line in printed.getvalue().splitlines())
        return code, pathlib.Path(paths["out"]).read_text(encoding="utf-8"), outputs


def plan(base, current, previous=None):
    """The retest plan a run writes: (regex, module lines), or None when empty."""
    with tempfile.TemporaryDirectory() as tmp:
        pathlib.Path(f"{tmp}/go.mod").write_text("module p\n", encoding="utf-8")
        run(base, current, previous=previous,
            extra=["--retest-plan", f"{tmp}/plan", "--workdir", tmp])
        lines = pathlib.Path(f"{tmp}/plan").read_text(encoding="utf-8").splitlines()
        return (lines[0], lines[1:]) if lines else None


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


def test_a_fingerprint_survives_the_crlf_round_trip():
    # GitHub rewrites a comment body to CRLF once anyone edits it in the web UI
    base = bench(BenchmarkX="100 ns/op")
    _, report, _ = run(base, bench(BenchmarkX="300 ns/op"))
    crlf = report.replace("\n", "\r\n")
    assert compare.read_digest(crlf) == compare.read_digest(report) != None  # noqa: E711
    _, _, outputs = run(base, bench(BenchmarkX="310 ns/op"), previous=crlf)
    assert outputs["changed"] == "false"


def test_the_footer_links_to_the_full_run():
    # the tables are capped, so the comment has to say where the rest lives
    base = bench(BenchmarkX="100 ns/op")
    _, report, _ = run(base, bench(BenchmarkX="300 ns/op"), extra=["--run-url", "https://x.test/runs/1"])
    assert "[full results](https://x.test/runs/1)" in report, report


def test_the_footer_links_both_compared_commits():
    base = bench(BenchmarkX="100 ns/op")
    args = [
        "--repo-url", "https://x.test/o/r",
        "--commit", "ede8793aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--baseline-ref", "main",
        "--baseline-sha", "d8ae9aabbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    _, report, _ = run(base, bench(BenchmarkX="300 ns/op"), extra=args)
    assert "[ede8793](https://x.test/o/r/commit/ede8793aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)" in report, report
    assert "vs main@[d8ae9aa](https://x.test/o/r/commit/d8ae9aabbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)" in report, report
    # without a resolved baseline sha the ref still names the branch, unlinked
    _, report, _ = run(base, bench(BenchmarkX="300 ns/op"), extra=args[:6])
    assert "vs main<" in report.replace(" ·", "<"), report


def test_the_retest_plan_names_every_finding_in_both_directions():
    base = "pkg: p\nBenchmarkSlow-4\t10\t100 ns/op\nBenchmarkFast-4\t10\t100 ns/op\nBenchmarkFine-4\t10\t100 ns/op\n"
    base += "pkg: p/q\nBenchmarkSlow-4\t10\t100 ns/op\n"
    current = "pkg: p\nBenchmarkSlow-4\t10\t300 ns/op\nBenchmarkFast-4\t10\t30 ns/op\nBenchmarkFine-4\t10\t100 ns/op\n"
    current += "pkg: p/q\nBenchmarkSlow-4\t10\t300 ns/op\n"
    # a false improvement pollutes the comment just like a false regression fails it
    regex, lines = plan(base, current)
    assert regex == "^(BenchmarkFast|BenchmarkSlow)$", regex
    # both packages belong to the root module, so everything runs from "."
    assert lines == [".\tp p/q"], lines
    # nothing moved, no plan; -bench '' would have run the whole suite again
    assert plan(base, base) is None


def test_the_retest_plan_covers_deviations_from_the_posted_report():
    base = bench(BenchmarkX="100 ns/op")
    _, report, _ = run(base, bench(BenchmarkX="300 ns/op"))
    # this run alone flags nothing, but it contradicts the posted 3.00x, and that
    # contradiction is what would rewrite the comment; verify it first
    assert plan(base, bench(BenchmarkX="120 ns/op"), previous=report) is not None
    # a reported result sitting where the report says needs no second look; use a
    # sub-threshold report (posted before the threshold was raised) so the current
    # value is neither a fresh finding nor a deviation
    _, mild, _ = run(base, bench(BenchmarkX="160 ns/op"), extra=["--threshold", "125%"])
    assert plan(base, bench(BenchmarkX="140 ns/op"), previous=mild) is None


def test_the_retest_plan_targets_the_parent_of_a_subtest():
    base = bench(**{"Benchmark_Ctx_Get/header-8": "100 ns/op"})
    current = bench(**{"Benchmark_Ctx_Get/header-8": "300 ns/op"})
    regex, _ = plan(base, current)
    # -bench matches per slash level, an alternation of full paths would match nothing
    assert regex == "^(Benchmark_Ctx_Get)$", regex


def test_the_plan_maps_packages_to_their_modules():
    base = "pkg: example.com/mod/sub\nBenchmarkA-4\t10\t100 ns/op\n"
    base += "pkg: example.com/other\nBenchmarkB-4\t10\t100 ns/op\n"
    base += "pkg: example.com/root/util\nBenchmarkR-4\t10\t100 ns/op\n"
    base += "pkg: example.com/lost\nBenchmarkC-4\t10\t100 ns/op\n"
    current = base.replace("100 ns/op", "300 ns/op")
    with tempfile.TemporaryDirectory() as tmp:
        # template's shape: a module at the root AND one per subdirectory
        pathlib.Path(f"{tmp}/go.mod").write_text("module example.com/root\n", encoding="utf-8")
        for directory, module in (("ants", "example.com/mod"), ("beta", "example.com/other")):
            pathlib.Path(f"{tmp}/{directory}").mkdir()
            pathlib.Path(f"{tmp}/{directory}/go.mod").write_text(f"module {module}\n", encoding="utf-8")
        run(base, current, extra=["--retest-plan", f"{tmp}/plan", "--workdir", tmp])
        lines = pathlib.Path(f"{tmp}/plan").read_text(encoding="utf-8").splitlines()
    # each package retests inside the module that owns it, and a package no
    # module claims stays unverified rather than guessed at
    assert lines[1:] == [
        ".\texample.com/root/util",
        "ants\texample.com/mod/sub",
        "beta\texample.com/other",
    ], lines


def test_the_retest_takes_the_median_of_repeated_measurements():
    base = bench(BenchmarkX="100 ns/op")
    current = bench(BenchmarkX="300 ns/op")
    # -count=3 prints three results; one of them repeating the wobble must not
    # confirm the regression when the other two cleared it
    noisy = "pkg: p\n" + "".join(f"BenchmarkX-4\t10\t{v} ns/op\n" for v in (110, 300, 115))
    _, _, outputs = run(base, current, retested=noisy)
    assert outputs["regressed"] == "false", outputs
    # and two high readings confirm it with the median value, not the stray low one
    settled = "pkg: p\n" + "".join(f"BenchmarkX-4\t10\t{v} ns/op\n" for v in (280, 300, 100))
    _, report, outputs = run(base, current, retested=settled)
    assert outputs["regressed"] == "true", outputs
    assert "100 → 280 ns/op" in report, report


def test_a_mass_regression_is_not_worth_a_retest():
    count = compare.RETEST_MAX + 1
    base = bench(**{f"Benchmark{i}": "10 ns/op" for i in range(count)})
    current = bench(**{f"Benchmark{i}": "100 ns/op" for i in range(count)})
    assert plan(base, current) is None, "half the suite regressing is not a noise problem"


def test_a_regression_that_does_not_reproduce_is_dropped():
    base = bench(BenchmarkX="100 ns/op")
    code, report, outputs = run(base, bench(BenchmarkX="300 ns/op"), retested=bench(BenchmarkX="110 ns/op"))
    assert code == 0 and outputs["regressed"] == "false"
    assert "No significant benchmark change" in report, report
    assert "retest: 0/1 regressions reproduced" in report, report


def test_a_regression_that_reproduces_stays():
    base = bench(BenchmarkX="100 ns/op")
    code, report, outputs = run(base, bench(BenchmarkX="300 ns/op"), retested=bench(BenchmarkX="290 ns/op"))
    assert code == 1 and outputs["regressed"] == "true"
    # the report carries the number that survived, not the first wobble
    assert "100 → 290 ns/op" in report, report
    assert "retest: 1/1 regressions reproduced" in report, report


def test_the_retest_only_overrides_what_was_flagged():
    base = bench(BenchmarkSlow="100 ns/op", BenchmarkFine="100 ns/op")
    current = bench(BenchmarkSlow="300 ns/op", BenchmarkFine="100 ns/op")
    # the parent-level regex re-measures more than was flagged, and BenchmarkFine
    # happens to wobble high on that second run; it was never flagged, so it stays
    retested = bench(BenchmarkSlow="310 ns/op", BenchmarkFine="900 ns/op")
    code, report, outputs = run(base, current, retested=retested)
    assert code == 1 and outputs["regressions"] == "1"
    assert "BenchmarkFine" not in report, report


def test_an_improvement_that_does_not_reproduce_is_dropped():
    # PR 4570: SendFile showed 2.4x faster on a run that touched nothing near it
    base = bench(BenchmarkX="100 ns/op")
    code, report, outputs = run(base, bench(BenchmarkX="40 ns/op"), retested=bench(BenchmarkX="95 ns/op"))
    assert code == 0 and outputs["significant"] == "false"
    assert "No significant benchmark change" in report, report
    assert "retest: 0/1 improvements reproduced" in report, report


def test_a_lone_direction_flip_is_dropped_entirely():
    base = bench(BenchmarkX="100 ns/op")
    # slower once, faster once: the two runs disagree, so neither is believed
    _, report, outputs = run(base, bench(BenchmarkX="300 ns/op"), retested=bench(BenchmarkX="40 ns/op"))
    assert outputs["significant"] == "false", outputs
    assert "No significant benchmark change" in report, report
    # and the flip must not fail the gate either way round
    code, report, outputs = run(base, bench(BenchmarkX="40 ns/op"), retested=bench(BenchmarkX="300 ns/op"))
    assert code == 0 and outputs["regressed"] == "false", outputs
    assert "No significant benchmark change" in report, report


def test_units_of_one_benchmark_are_verified_independently():
    base = bench(BenchmarkX="100 ns/op\t100 B/op")
    # slower but leaner, and both directions reproduce
    current = bench(BenchmarkX="300 ns/op\t40 B/op")
    code, report, _ = run(base, current, retested=bench(BenchmarkX="290 ns/op\t42 B/op"))
    assert code == 1
    assert "100 → 290 ns/op" in report and "100 → 42 B/op" in report, report
    assert "retest: 1/1 regressions, 1/1 improvements reproduced" in report, report


def test_a_flip_backed_by_the_report_keeps_the_reported_side():
    base = bench(BenchmarkX="100 ns/op")
    _, first, _ = run(base, bench(BenchmarkX="300 ns/op"))
    # run 1 confirms the posted 3.00x, the retest is the lone outlier; two of
    # three readings agree, so the alert must not be cleared by the third
    _, report, outputs = run(base, bench(BenchmarkX="290 ns/op"), previous=first,
                             retested=bench(BenchmarkX="40 ns/op"))
    assert outputs["regressed"] == "true" and outputs["changed"] == "false", outputs
    assert "100 → 290 ns/op" in report, report
    # same for a reported improvement that the retest suddenly calls a regression
    _, first, _ = run(base, bench(BenchmarkX="40 ns/op"))
    code, report, outputs = run(base, bench(BenchmarkX="42 ns/op"), previous=first,
                                retested=bench(BenchmarkX="300 ns/op"))
    assert code == 0 and outputs["changed"] == "false", outputs
    assert "100 → 42 ns/op" in report, report


def test_verified_values_can_seed_the_next_baseline():
    base = bench(BenchmarkX="100 ns/op", BenchmarkY="100 ns/op")
    current = bench(BenchmarkX="300 ns/op", BenchmarkY="100 ns/op")
    with tempfile.TemporaryDirectory() as tmp:
        run(base, current, retested=bench(BenchmarkX="110 ns/op"),
            extra=["--save-verified", f"{tmp}/verified"])
        with open(f"{tmp}/verified", encoding="utf-8") as handle:
            saved, _ = compare.parse(handle)
    # the spike was re-measured, its verified value goes in; the rest is untouched
    assert saved[compare.Key("p", "BenchmarkX", "ns/op")] == 110.0
    assert saved[compare.Key("p", "BenchmarkY", "ns/op")] == 100.0


def test_saved_names_and_values_survive_the_round_trip():
    text = "pkg: p\nBenchmark_Ctx_Get/header-8-4\t100\t0.50 ns/op\t1977.09 MB/s\n"
    text += "pkg: q\nBenchmarkPlain-4\t100\t58 ns/op\t24 B/op\n"
    results = compare.parse(io.StringIO(text))[0]
    with tempfile.TemporaryDirectory() as tmp:
        compare.save_verified(f"{tmp}/v", results)
        with open(f"{tmp}/v", encoding="utf-8") as handle:
            again, duplicates = compare.parse(handle)
    # the -8 must survive: parse strips one trailing -N, save adds a sacrificial one
    assert again == results and duplicates == 0, again


def test_a_reported_regression_that_wobbles_low_is_re_checked_not_cleared():
    base = bench(BenchmarkX="100 ns/op")
    _, first, _ = run(base, bench(BenchmarkX="300 ns/op"))
    # run 1 says "fixed", the re-measurement says the regression is still there
    _, report, outputs = run(base, bench(BenchmarkX="120 ns/op"), previous=first,
                             retested=bench(BenchmarkX="290 ns/op"))
    assert outputs["regressed"] == "true", outputs
    # near the posted 3.00x, so the comment is refreshed, not replaced
    assert outputs["changed"] == "false", outputs
    assert "100 → 290 ns/op" in report, report
    assert "retest: 1 reported re-checked" in report, report
    # and when the fix is real, the retest confirms it and the comment may clear
    _, report, outputs = run(base, bench(BenchmarkX="120 ns/op"), previous=first,
                             retested=bench(BenchmarkX="105 ns/op"))
    assert outputs["regressed"] == "false" and outputs["changed"] == "true", outputs
    assert "No significant benchmark change" in report, report


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
