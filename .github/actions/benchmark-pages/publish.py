#!/usr/bin/env python3
"""Append a `go test -bench` run to the gh-pages benchmark data.

Replaces the publishing half of github-action-benchmark, whose per-run format
repeated every benchmark name once per run and pretty-printed the JSON: fiber's
data.js hit 85 MB for ~1.4 MB of information, a third of GitHub's file limit
spent on indentation. The v2 format is columnar - each series name once, values
as a matrix - and the page expands it back to the old shape on load.

A legacy data.js is converted in place on the first publish, so history is
kept and no repo needs a manual migration. The series names are produced with
the exact rules of github-action-benchmark's Go extractor, otherwise every
existing chart would fork into a second series at the cutover.
"""
import argparse
import json
import os
import re
import sys
import time

# github-action-benchmark's Go line regex, ported verbatim (dist/src/extract.js
# @ 52576c92): the odd character class contains the `*-=` range on purpose.
LINE_RE = re.compile(
    r'^(?P<name>Benchmark\w+[\w()$%^&*-=|,\[\]{}"#]*?)(?P<procs>-\d+)?\s+(?P<times>\d+)\s+(?P<remainder>.+)$'
)
PKG_RE = re.compile(r"^pkg:\s+(?P<pkg>\S+)")
# a bare name is one without the " - <unit>" suffix the extractor appends
METRIC_SUFFIX_RE = re.compile(r" - [^ ]*/[^ ]*$")
VERSION = 2


def contains_package_ref(name, pkg):
    """Whether the name already carries the package, gab's heuristic verbatim."""
    segments = pkg.split("/")
    # at least 2 segments, to avoid false positives like "cache" in BenchmarkCache
    for start in range(len(segments) - 1):
        suffix = "/".join(segments[start:])
        if suffix in name or suffix.replace("/", "_") in name:
            return True
    return False


def extract(output, force_package_suffix):
    """The (name, unit, value) series of one run, named exactly like gab named them.

    gab additionally emitted every multi-metric line once more under its bare
    name, with the whole tail as the unit; the page always skipped those
    duplicates, so they are not produced (and are dropped from converted data).
    """
    sections = []
    current = ("", [])
    sections.append(current)
    for line in output.splitlines():
        header = PKG_RE.match(line)
        if header:
            current = (header.group("pkg"), [])
            sections.append(current)
        else:
            current[1].append(line)
    packages = {pkg for pkg, _ in sections if pkg}
    multiple = len(packages) > 1

    results = []
    for pkg, lines in sections:
        for line in lines:
            match = LINE_RE.match(line)
            if not match:
                continue
            pieces = re.split(r"[ \t]+", match.group("remainder"))
            pairs = [(pieces[i * 2], pieces[i * 2 + 1]) for i in range(len(pieces) // 2)]
            if not pairs:
                continue
            name = match.group("name")
            if multiple and pkg and (force_package_suffix or not contains_package_ref(name, pkg)):
                name = f"{name} ({pkg})"
            for value, unit in pairs:
                try:
                    number = float(value)
                except ValueError:
                    continue
                series = name if len(pairs) == 1 else f"{name} - {unit}"
                results.append((series, unit, number))
    return results


def convert(legacy):
    """A legacy github-action-benchmark data.js object as v2 columns."""
    suites = list((legacy.get("entries") or {}).values())
    entries = suites[0] if suites else []
    data = {
        "version": VERSION,
        "lastUpdate": legacy.get("lastUpdate", 0),
        "repoUrl": legacy.get("repoUrl", ""),
        "runs": [],
        "names": [],
        "units": [],
        "values": [],
    }
    all_names = set()
    for entry in entries:
        for bench in entry.get("benches") or []:
            all_names.add(bench.get("name"))
    index = {}
    for entry in entries:
        commit = entry.get("commit") or {}
        data["runs"].append(
            {
                "id": commit.get("id", ""),
                "timestamp": commit.get("timestamp", ""),
                "url": commit.get("url", ""),
                # the page only ever showed the first line
                "message": str(commit.get("message", "")).split("\n")[0],
            }
        )
        run = len(data["runs"]) - 1
        for bench in entry.get("benches") or []:
            name = bench.get("name")
            if name is None or not isinstance(bench.get("value"), (int, float)):
                continue
            # the bare duplicate of a multi-metric line; the page skipped it too
            if not METRIC_SUFFIX_RE.search(name) and f"{name} - ns/op" in all_names:
                continue
            set_value(data, index, name, str(bench.get("unit", "")), bench["value"], run)
    return data


def set_value(data, index, name, unit, value, run):
    row = index.get(name)
    if row is None:
        row = index[name] = len(data["names"])
        data["names"].append(name)
        data["units"].append(unit)
        data["values"].append([None] * len(data["runs"]))
    values = data["values"][row]
    values.extend([None] * (len(data["runs"]) - len(values)))
    values[run] = value


def load(path):
    """The stored data in v2 form: converted when legacy, fresh when missing."""
    if not os.path.exists(path):
        return {
            "version": VERSION,
            "lastUpdate": 0,
            "repoUrl": "",
            "runs": [],
            "names": [],
            "units": [],
            "values": [],
        }
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise SystemExit(f"{path} does not look like a benchmark data.js")
    data = json.loads(text[start : end + 1])
    return data if data.get("version") == VERSION else convert(data)


def append(data, results, commit):
    data["runs"].append(commit)
    run = len(data["runs"]) - 1
    index = {name: row for row, name in enumerate(data["names"])}
    for name, unit, value in results:
        set_value(data, index, name, unit, value, run)
    for values in data["values"]:
        values.extend([None] * (len(data["runs"]) - len(values)))


def trim(data, max_items):
    """Keep the newest runs and drop series that no longer have any value."""
    cut = len(data["runs"]) - max_items
    if cut > 0:
        data["runs"] = data["runs"][cut:]
        data["values"] = [values[cut:] for values in data["values"]]
    keep = [row for row, values in enumerate(data["values"]) if any(v is not None for v in values)]
    if len(keep) != len(data["names"]):
        for column in ("names", "units", "values"):
            data[column] = [data[column][row] for row in keep]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--commit-id", default="")
    parser.add_argument("--commit-timestamp", default="")
    parser.add_argument("--commit-message", default="")
    parser.add_argument("--force-package-suffix", default="false")
    # convert/trim without appending a run, for one-off migrations
    parser.add_argument("--convert", action="store_true")
    parser.add_argument("--now-ms", type=int, default=0)
    args = parser.parse_args(argv)

    data = load(args.data)
    if args.repo_url:
        data["repoUrl"] = data["repoUrl"] or args.repo_url

    if not args.convert:
        if not args.output or args.max_items <= 0:
            raise SystemExit("--output and --max-items are required to publish")
        with open(args.output, encoding="utf-8") as handle:
            results = extract(handle.read(), args.force_package_suffix == "true")
        if not results:
            raise SystemExit(f"no benchmark results found in {args.output}")
        commit = {
            "id": args.commit_id,
            "timestamp": args.commit_timestamp,
            "url": f"{data['repoUrl']}/commit/{args.commit_id}" if data["repoUrl"] else "",
            "message": args.commit_message.split("\n")[0],
        }
        append(data, results, commit)
        data["lastUpdate"] = args.now_ms or int(time.time() * 1000)
    if args.max_items > 0:
        trim(data, args.max_items)

    with open(args.data, "w", encoding="utf-8") as handle:
        handle.write("window.BENCHMARK_DATA = ")
        json.dump(data, handle, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
    print(f"{len(data['names'])} series x {len(data['runs'])} runs, {os.path.getsize(args.data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
