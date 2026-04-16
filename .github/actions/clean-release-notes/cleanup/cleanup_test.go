package cleanup

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStripBulletEmoji(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "leading single emoji",
			in:   "## Fixes\n- 🐛 fix something (#42)\n",
			want: "## Fixes\n- fix something (#42)\n",
		},
		{
			name: "leading multi-emoji sequence with ZWJ",
			in:   "## Fixes\n- 👨‍💻 refactor the bot (#99)\n",
			want: "## Fixes\n- refactor the bot (#99)\n",
		},
		{
			name: "emoji with VS16",
			in:   "## New\n- ❗️ breaking thing (#1)\n",
			want: "## New\n- breaking thing (#1)\n",
		},
		{
			name: "no emoji means no change",
			in:   "## Fixes\n- fix something (#42)\n",
			want: "## Fixes\n- fix something (#42)\n",
		},
		{
			name: "heading keeps its emoji",
			in:   "## 🐛 Fixes\n- plain bullet (#1)\n",
			want: "## 🐛 Fixes\n- plain bullet (#1)\n",
		},
		{
			name: "non-bullet line untouched",
			in:   "## Fixes\n🐛 not a bullet\n- 🐛 is a bullet (#1)\n",
			want: "## Fixes\n🐛 not a bullet\n- is a bullet (#1)\n",
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			b := Parse(tc.in)
			b.StripBulletEmoji()
			got := b.Render()
			if got != tc.want {
				t.Errorf("StripBulletEmoji mismatch\n--- got\n%q\n--- want\n%q", got, tc.want)
			}
		})
	}
}

func TestStripConvCommitPrefix(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "fix:",
			in:   "## Fixes\n- fix: handle nil pointer (#1)\n",
			want: "## Fixes\n- handle nil pointer (#1)\n",
		},
		{
			name: "feat(scope):",
			in:   "## New\n- feat(api): add endpoint (#2)\n",
			want: "## New\n- add endpoint (#2)\n",
		},
		{
			name: "chore! breaking",
			in:   "## Breaking\n- chore!: drop deprecated flag (#3)\n",
			want: "## Breaking\n- drop deprecated flag (#3)\n",
		},
		{
			name: "case-insensitive",
			in:   "## Fixes\n- FIX: upper case (#4)\n",
			want: "## Fixes\n- upper case (#4)\n",
		},
		{
			name: "not a conv-commit keyword",
			in:   "## Fixes\n- wip: not conv-commit (#5)\n",
			want: "## Fixes\n- wip: not conv-commit (#5)\n",
		},
		{
			name: "applies after emoji strip",
			in:   "## Fixes\n- 🐛 fix: combined (#6)\n",
			want: "## Fixes\n- combined (#6)\n",
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			b := Parse(tc.in)
			b.StripBulletEmoji()
			b.StripConvCommitPrefix()
			got := b.Render()
			if got != tc.want {
				t.Errorf("StripConvCommitPrefix mismatch\n--- got\n%q\n--- want\n%q", got, tc.want)
			}
		})
	}
}

func TestFilterBotContributors(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		in   string
		bots []string
		want string
	}{
		{
			name: "all bots removes the line",
			in:   "## Fixes\n- bug (#1)\n\nThank you @dependabot[bot] @renovate[bot] for making this update possible.\n",
			bots: []string{"dependabot[bot]", "renovate[bot]"},
			want: "## Fixes\n- bug (#1)\n",
		},
		{
			name: "mixed keeps humans, drops bots",
			in:   "## Fixes\n- bug (#1)\n\nThank you @alice @dependabot[bot] @bob for making this update possible.\n",
			bots: []string{"dependabot[bot]"},
			want: "## Fixes\n- bug (#1)\n\nThank you @alice @bob for making this update possible.\n",
		},
		{
			name: "no Thank-you line is a no-op",
			in:   "## Fixes\n- bug (#1)\n",
			bots: []string{"dependabot[bot]"},
			want: "## Fixes\n- bug (#1)\n",
		},
		{
			name: "case-insensitive match",
			in:   "## Fixes\n- bug (#1)\n\nThank you @DependaBot[bot] for making this update possible.\n",
			bots: []string{"dependabot[bot]"},
			want: "## Fixes\n- bug (#1)\n",
		},
		{
			// "Thank you to @alice" is a template variant release-drafter may
			// emit. When no bots are in the list we touch nothing (pass-through
			// preserves the exact original wording). The rewrite branch — which
			// normalizes to "Thank you @...") — only fires when at least one bot
			// is filtered out, which is covered by the other cases above.
			name: "Thank you to variant with no bots present",
			in:   "## Fixes\n- bug (#1)\n\nThank you to @alice for making this update possible.\n",
			bots: []string{"dependabot[bot]"},
			want: "## Fixes\n- bug (#1)\n\nThank you to @alice for making this update possible.\n",
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			b := Parse(tc.in)
			b.FilterBotContributors(tc.bots, nil)
			got := b.Render()
			if got != tc.want {
				t.Errorf("FilterBotContributors mismatch\n--- got\n%q\n--- want\n%q", got, tc.want)
			}
		})
	}
}

func TestDedupe(t *testing.T) {
	t.Parallel()
	// #1 appears in both "Fixes" and "Updates". Fixes has higher priority,
	// so the Updates entry must disappear. #2 is unique.
	in := strings.Join([]string{
		"## 🐛 Fixes",
		"- fix crash (#1)",
		"",
		"## 🧹 Updates",
		"- update dep (#1)",
		"- bump lib (#2)",
		"",
	}, "\n")
	want := strings.Join([]string{
		"## 🐛 Fixes",
		"- fix crash (#1)",
		"",
		"## 🧹 Updates",
		"- bump lib (#2)",
		"",
	}, "\n")

	b := Parse(in)
	var warns []string
	b.Dedupe(func(s string) { warns = append(warns, s) })
	got := b.Render()
	if got != want {
		t.Fatalf("Dedupe mismatch\n--- got\n%q\n--- want\n%q", got, want)
	}
	if len(warns) != 1 || !strings.Contains(warns[0], "#1") {
		t.Fatalf("expected a warning about #1, got %v", warns)
	}
}

func TestDedupeKeepsBulletsWithoutPRRef(t *testing.T) {
	t.Parallel()
	// Bullets without #NNN must always be kept, even if a later section has
	// the same text.
	in := "## New\n- some note\n\n## Updates\n- some note\n"
	b := Parse(in)
	b.Dedupe(nil)
	if b.Render() != in {
		t.Fatalf("Dedupe wrongly removed a non-PR bullet:\n%s", b.Render())
	}
}

func TestApplyOrchestrates(t *testing.T) {
	t.Parallel()
	in := strings.Join([]string{
		"## 🐛 Fixes",
		"- 🐛 fix: handle nil (#1)",
		"",
		"## 🧹 Updates",
		"- chore(deps): bump foo (#1)",
		"- 🤖 bump bar (#2)",
		"",
		"**Full Changelog**: https://github.com/o/r/compare/v1...v2",
		"",
		"Thank you @alice @dependabot[bot] for making this update possible.",
		"",
	}, "\n")
	var warns []string
	out := Apply(in, Options{
		Bots:     []string{"dependabot[bot]"},
		Dedupe:   true,
		Warnings: &warns,
	})

	// Assertions — we don't hard-code the full output to keep this robust
	// against whitespace tweaks; we check the observable invariants.
	mustContain(t, out, "- handle nil (#1)", "emoji + conv-commit prefix should be gone in Fixes")
	mustContain(t, out, "- bump bar (#2)", "bot emoji and leading emoji stripped in Updates")
	mustNotContain(t, out, "bump foo (#1)", "dedupe should have dropped #1 from Updates")
	mustContain(t, out, "Thank you @alice for making this update possible.", "bot filtered from footer")
	mustNotContain(t, out, "@dependabot", "dependabot must not appear anywhere")

	if len(warns) == 0 {
		t.Error("expected at least one warning (dedupe + bot filter)")
	}
}

// Golden-file regression tests. Each .in.md under testdata/ pairs with a
// .out.md; Apply with the flags encoded in the filename (see resolveOpts)
// must reproduce the .out.md exactly.
func TestGoldenFiles(t *testing.T) {
	t.Parallel()
	entries, err := os.ReadDir("testdata")
	if err != nil {
		t.Fatalf("read testdata: %v", err)
	}
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".in.md") {
			continue
		}
		name := strings.TrimSuffix(e.Name(), ".in.md")
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			inPath := filepath.Join("testdata", name+".in.md")
			outPath := filepath.Join("testdata", name+".out.md")

			in, err := os.ReadFile(inPath)
			if err != nil {
				t.Fatalf("read %s: %v", inPath, err)
			}
			want, err := os.ReadFile(outPath)
			if err != nil {
				t.Fatalf("read %s: %v", outPath, err)
			}

			got := Apply(string(in), resolveOpts(name))
			if got != string(want) {
				t.Errorf("golden mismatch for %s\n--- got\n%s\n--- want\n%s", name, got, string(want))
			}
		})
	}
}

// resolveOpts maps a fixture base name to the cleanup options it should be
// applied with. Keeping this mapping in code (rather than per-file config)
// makes the intent explicit and reviewable. The bot allowlist starts from
// the central DefaultBots so the fixtures exercise the same defaults the
// CLI ships with.
func resolveOpts(name string) Options {
	opts := Options{
		Bots:   append([]string(nil), DefaultBots...),
		Dedupe: true,
	}
	switch name {
	case "dedupe-off":
		opts.Dedupe = false
	case "custom-bot":
		opts.Bots = append(opts.Bots, "gofiberbot")
	}
	return opts
}

func mustContain(t *testing.T, haystack, needle, reason string) {
	t.Helper()
	if !strings.Contains(haystack, needle) {
		t.Errorf("expected output to contain %q (%s)\noutput was:\n%s", needle, reason, haystack)
	}
}

func mustNotContain(t *testing.T, haystack, needle, reason string) {
	t.Helper()
	if strings.Contains(haystack, needle) {
		t.Errorf("expected output to NOT contain %q (%s)\noutput was:\n%s", needle, reason, haystack)
	}
}

func FuzzStripBulletEmoji(f *testing.F) {
	seeds := []string{
		"## x\n- 🐛 foo (#1)\n",
		"## x\n- no emoji (#1)\n",
		"",
		"## x\n\n",
		"## x\n- 👨‍💻 zwj (#1)\n",
	}
	for _, s := range seeds {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, body string) {
		// Must never panic, regardless of input.
		b := Parse(body)
		b.StripBulletEmoji()
		b.StripConvCommitPrefix()
		b.FilterBotContributors([]string{"dependabot[bot]"}, nil)
		b.Dedupe(nil)
		_ = b.Render()
	})
}
