#!/usr/bin/env python3
"""Self-check for the benchmark-pages publish script: run it directly, it prints OK."""
import contextlib
import io
import json
import pathlib
import sys
import tempfile

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1].parent / "actions" / "benchmark-pages")
)
import publish  # noqa: E402

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

# what github-action-benchmark wrote for the first line of OUTPUT: the bare
# duplicate first, with the whole tail as its unit, then one entry per metric
LEGACY = {
    "lastUpdate": 1700000000000,
    "repoUrl": "https://github.com/gofiber/fiber",
    "entries": {
        "Benchmark": [
            {
                "commit": {
                    "author": {"name": "A"},
                    "committer": {"name": "A"},
                    "distinct": True,
                    "id": "a" * 40,
                    "message": "feat: first line\n\nbody text",
                    "timestamp": "2026-07-01T10:00:00+02:00",
                    "tree_id": "t",
                    "url": "https://github.com/gofiber/fiber/commit/" + "a" * 40,
                },
                "date": 1700000000001,
                "tool": "go",
                "benches": [
                    {
                        "name": "Benchmark_NewError (github.com/gofiber/fiber/v3)",
                        "value": 58,
                        "unit": "ns/op\t      24 B/op\t       1 allocs/op",
                        "extra": "19444994 times\n4 procs",
                    },
                    {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - ns/op", "value": 58, "unit": "ns/op"},
                    {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - B/op", "value": 24, "unit": "B/op"},
                    {"name": "Benchmark_NewError (github.com/gofiber/fiber/v3) - allocs/op", "value": 1, "unit": "allocs/op"},
                    {"name": "Benchmark_Old_Gone", "value": 7, "unit": "ns/op"},
                ],
            }
        ]
    },
}


def run_main(argv):
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = publish.main(argv)
    return code, printed.getvalue()


def written(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    assert text.startswith("window.BENCHMARK_DATA = "), text[:40]
    return json.loads(text[text.find("{") : text.rfind("}") + 1])


def test_extract_names_series_like_the_gab_extractor_did():
    results = publish.extract(OUTPUT, force_package_suffix=True)
    names = [name for name, _, _ in results]
    # multi-metric lines: one series per metric, no bare duplicate
    assert "Benchmark_NewError (github.com/gofiber/fiber/v3) - ns/op" in names
    assert "Benchmark_NewError (github.com/gofiber/fiber/v3) - B/op" in names
    assert "Benchmark_NewError (github.com/gofiber/fiber/v3) - allocs/op" in names
    assert "Benchmark_NewError (github.com/gofiber/fiber/v3)" not in names
    # throughput keeps its own series
    assert "BenchmarkAppendMsg (github.com/gofiber/fiber/v3) - MB/s" in names
    # a single-metric line stays bare, and only the GOMAXPROCS suffix is stripped
    assert "Benchmark_Ctx_Get/header-8 (github.com/gofiber/fiber/v3)" in names
    # same benchmark name in a second package is its own series
    assert "Benchmark_NewError (github.com/gofiber/fiber/v3/binder) - ns/op" in names
    values = dict((name, value) for name, _, value in results)
    assert values["Benchmark_NewError (github.com/gofiber/fiber/v3) - B/op"] == 24.0


def test_extract_only_suffixes_multi_package_output():
    single = "pkg: github.com/gofiber/storage/redis/v3\nBenchmark_Redis_Set-4\t10\t100 ns/op\t278 B/op\t9 allocs/op\n"
    names = [name for name, _, _ in publish.extract(single, force_package_suffix=False)]
    assert names == ["Benchmark_Redis_Set - ns/op", "Benchmark_Redis_Set - B/op", "Benchmark_Redis_Set - allocs/op"], names
    # even with the force flag: gab only suffixed when the run had several packages
    names = [name for name, _, _ in publish.extract(single, force_package_suffix=True)]
    assert names[0] == "Benchmark_Redis_Set - ns/op", names


def test_extract_skips_the_suffix_when_the_name_already_names_the_package():
    output = "pkg: example.com/mod/redis\nBenchmark_redis_Set-4\t10\t100 ns/op\n"
    output += "pkg: example.com/mod/other\nBenchmarkPlain-4\t10\t100 ns/op\n"
    names = {name for name, _, _ in publish.extract(output, force_package_suffix=False)}
    # "mod/redis" is not in the name, "mod_redis" neither; one-segment matches do not count
    assert "Benchmark_redis_Set (example.com/mod/redis)" in names, names
    ref = "pkg: example.com/mod/redis\nBenchmark_mod_redis_Set-4\t10\t100 ns/op\n"
    ref += "pkg: example.com/mod/other\nBenchmarkPlain-4\t10\t100 ns/op\n"
    names = {name for name, _, _ in publish.extract(ref, force_package_suffix=False)}
    assert "Benchmark_mod_redis_Set" in names, names


def test_convert_drops_the_bare_duplicates_and_keeps_the_rest():
    data = publish.convert(LEGACY)
    assert data["version"] == 2
    assert "Benchmark_NewError (github.com/gofiber/fiber/v3)" not in data["names"]
    assert "Benchmark_NewError (github.com/gofiber/fiber/v3) - ns/op" in data["names"]
    # a single-metric benchmark has no " - ns/op" sibling and survives bare
    assert "Benchmark_Old_Gone" in data["names"]
    assert data["runs"][0]["id"] == "a" * 40
    assert data["runs"][0]["message"] == "feat: first line"
    assert data["repoUrl"] == "https://github.com/gofiber/fiber"


def test_publish_appends_and_a_second_run_lines_up():
    with tempfile.TemporaryDirectory() as tmp:
        data_path = f"{tmp}/data.js"
        out = f"{tmp}/output.txt"
        pathlib.Path(data_path).write_text(
            "window.BENCHMARK_DATA = " + json.dumps(LEGACY), encoding="utf-8"
        )
        pathlib.Path(out).write_text(OUTPUT, encoding="utf-8")
        argv = [
            "--data", data_path, "--output", out, "--max-items", "185",
            "--commit-id", "b" * 40, "--commit-timestamp", "2026-07-30T12:00:00+02:00",
            "--commit-message", "fix: second\n\nmore", "--force-package-suffix", "true",
            "--now-ms", "1753900000000",
        ]
        code, printed = run_main(argv + ["--cpu", "Ampere-1a (GOMAXPROCS=4)"])
        assert code == 0, printed
        data = written(data_path)
        assert len(data["runs"]) == 2
        assert data["runs"][1]["message"] == "fix: second"
        assert data["runs"][1]["url"].endswith("/commit/" + "b" * 40)
        # the CPU rides along per run; legacy runs simply have none
        assert data["runs"][1]["cpu"] == "Ampere-1a (GOMAXPROCS=4)"
        assert "cpu" not in data["runs"][0]
        assert data["lastUpdate"] == 1753900000000
        row = data["names"].index("Benchmark_NewError (github.com/gofiber/fiber/v3) - ns/op")
        # legacy run and fresh run land in the same series, no fork at the cutover
        assert data["values"][row] == [58, 58.0], data["values"][row]
        gone = data["names"].index("Benchmark_Old_Gone")
        assert data["values"][gone] == [7, None]
        new = data["names"].index("BenchmarkAppendMsg (github.com/gofiber/fiber/v3) - MB/s")
        assert data["values"][new] == [None, 1977.09]


def test_trim_caps_runs_and_drops_dead_series():
    with tempfile.TemporaryDirectory() as tmp:
        data_path = f"{tmp}/data.js"
        out = f"{tmp}/output.txt"
        pathlib.Path(data_path).write_text(
            "window.BENCHMARK_DATA = " + json.dumps(LEGACY), encoding="utf-8"
        )
        pathlib.Path(out).write_text(OUTPUT, encoding="utf-8")
        argv = [
            "--data", data_path, "--output", out, "--max-items", "1",
            "--commit-id", "b" * 40, "--commit-timestamp", "t", "--commit-message", "m",
            "--force-package-suffix", "true", "--now-ms", "1",
        ]
        run_main(argv)
        data = written(data_path)
        assert len(data["runs"]) == 1 and data["runs"][0]["id"] == "b" * 40
        # the legacy-only series lost its last value and disappears with its run
        assert "Benchmark_Old_Gone" not in data["names"]
        assert all(len(values) == 1 for values in data["values"])


def test_convert_mode_needs_no_output_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        data_path = f"{tmp}/data.js"
        pathlib.Path(data_path).write_text(
            "window.BENCHMARK_DATA = " + json.dumps(LEGACY), encoding="utf-8"
        )
        code, _ = run_main(["--data", data_path, "--convert"])
        assert code == 0
        first = written(data_path)
        assert first["version"] == 2
        code, _ = run_main(["--data", data_path, "--convert"])
        assert written(data_path) == first
        # and lastUpdate is carried over, not reset
        assert first["lastUpdate"] == 1700000000000


def test_ragged_rows_stay_aligned_through_trim_and_append():
    # convert leaves a row short when its series stops mid-history; rows are
    # prefix-aligned (index == run index), so slicing and extending must keep that
    data = {
        "version": 2, "lastUpdate": 1, "repoUrl": "",
        "runs": [{"id": "r0"}, {"id": "r1"}, {"id": "r2"}],
        "names": ["A - ns/op"], "units": ["ns/op"],
        "values": [[10, 20]],  # absent in r2, trailing null compressed away
    }
    publish.trim(data, 2)
    # the window is now r1+r2; A's r1 value must sit at index 0
    assert data["values"] == [[20]], data["values"]
    publish.append(data, [("B - ns/op", "ns/op", 5.0)], {"id": "r3"})
    assert data["values"][0] == [20, None, None], data["values"]
    assert data["values"][1] == [None, None, 5.0], data["values"]


def test_first_publish_without_existing_data():
    with tempfile.TemporaryDirectory() as tmp:
        data_path = f"{tmp}/fresh/data.js"
        pathlib.Path(f"{tmp}/fresh").mkdir()
        out = f"{tmp}/output.txt"
        pathlib.Path(out).write_text(OUTPUT, encoding="utf-8")
        code, _ = run_main([
            "--data", data_path, "--output", out, "--max-items", "10",
            "--repo-url", "https://github.com/gofiber/fiber",
            "--commit-id", "c" * 40, "--commit-timestamp", "t", "--commit-message", "m",
            "--now-ms", "5",
        ])
        assert code == 0
        data = written(data_path)
        assert data["repoUrl"] == "https://github.com/gofiber/fiber"
        assert len(data["runs"]) == 1 and len(data["names"]) > 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
