// Command release-notes-cleanup reads a release-drafter generated release
// body from stdin, rewrites it so that leading emoji and conventional-commit
// prefixes are removed from bullet lines, optionally deduplicates PR
// references across categories, and strips bot authors from the contributor
// footer. The cleaned body is written to stdout. Warnings (e.g. dedupe
// decisions) go to stderr.
//
// Example:
//
//	gh api /repos/gofiber/contrib/releases/123 --jq .body \
//	  | release-notes-cleanup --bots 'dependabot[bot],renovate[bot]' \
//	  | gh api --method PATCH /repos/gofiber/contrib/releases/123 \
//	      --raw-field body=@- -F draft=false
package main

import (
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/gofiber/dotgithub/actions/clean-release-notes/cleanup"
)

func main() {
	exitCode := run(os.Stdin, os.Stdout, os.Stderr, os.Args[1:])
	os.Exit(exitCode)
}

// run is the testable entry point. Returning an int keeps os.Exit out of the
// hot path.
func run(stdin io.Reader, stdout, stderr io.Writer, args []string) int {
	fs := flag.NewFlagSet("release-notes-cleanup", flag.ContinueOnError)
	fs.SetOutput(stderr)

	var (
		bots    string
		dedupe  bool
		dryRun  bool
		verbose bool
	)
	fs.StringVar(&bots, "bots", "",
		"extra bot keywords (comma-separated); a login is a bot if it contains [bot] or any keyword (case-insensitive substring)")
	fs.BoolVar(&dedupe, "dedupe", true,
		"drop bullets whose #PR reference already appears in a higher-priority section")
	fs.BoolVar(&dryRun, "dry-run", false,
		"still write the cleaned body to stdout, but also emit a diff-ish summary to stderr")
	fs.BoolVar(&verbose, "verbose", false,
		"print per-rule notes to stderr even when nothing notable happened")

	if err := fs.Parse(args); err != nil {
		// flag.ContinueOnError already printed the usage; just exit.
		return 2
	}

	raw, err := io.ReadAll(stdin)
	if err != nil {
		fmt.Fprintf(stderr, "release-notes-cleanup: read stdin: %v\n", err)
		return 2
	}
	original := string(raw)

	opts := cleanup.Options{
		BotKeywords: parseCSV(bots),
		Dedupe:      dedupe,
	}
	var warnings []string
	opts.Warnings = &warnings

	cleaned := cleanup.Apply(original, opts)

	if _, err := io.WriteString(stdout, cleaned); err != nil {
		fmt.Fprintf(stderr, "release-notes-cleanup: write stdout: %v\n", err)
		return 2
	}

	for _, w := range warnings {
		fmt.Fprintln(stderr, "note:", w)
	}

	if dryRun {
		fmt.Fprintln(stderr, "--- dry-run summary ---")
		fmt.Fprintf(stderr, "input  bytes: %d\n", len(original))
		fmt.Fprintf(stderr, "output bytes: %d\n", len(cleaned))
		fmt.Fprintf(stderr, "rules:        extra-keywords=%d dedupe=%t\n", len(opts.BotKeywords), opts.Dedupe)
	}

	if verbose && len(warnings) == 0 {
		fmt.Fprintln(stderr, "note: no rules triggered")
	}

	return 0
}

// parseCSV splits a comma-separated string into trimmed, non-empty entries.
func parseCSV(csv string) []string {
	var result []string
	for _, s := range strings.Split(csv, ",") {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		result = append(result, s)
	}
	return result
}
