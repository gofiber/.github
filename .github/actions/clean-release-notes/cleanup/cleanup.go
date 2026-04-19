// Package cleanup contains pure, testable helpers that rewrite a release-drafter
// generated release body so it matches the style the GoFiber maintainers want
// before a release is published.
//
// The rules, in the order Apply invokes them:
//
//  1. Parse the body into a preamble, a set of H2 sections ("## ..."), and an
//     epilogue (trailing lines that are not part of any section, e.g. the
//     "Full Changelog" / "Thank you ..." footer).
//  2. Strip a leading emoji from every bullet line ("- ...") inside each
//     section. Category headings keep their emoji.
//  3. Strip a conventional-commit prefix ("fix:", "feat(scope):", "chore!:") from
//     every bullet line.
//  4. Remove bot logins from the "Thank you @a @b ..." footer. If the resulting
//     list is empty, drop the footer line entirely.
//  5. Optionally dedupe PR references that appear in more than one section
//     (priority: Breaking > New > Fixes > Updates > Docs; first win).
//
// Every function is deterministic and side-effect free except for WARN messages
// routed through the Warnings slice on Options.
package cleanup

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// DefaultBotKeywords is the organization-wide list of keywords used to
// identify bot accounts in the contributor footer. A login is considered a
// bot if it contains "[bot]" (any case) OR if it contains any of these
// keywords as a case-insensitive substring. Adding an entry here propagates
// to every caller (CLI + composite action).
var DefaultBotKeywords = []string{
	"[bot]",
	"anthropic",
	"claude",
	"copilot",
	"codex",
	"dependabot",
	"github",
	"gemini",
	"renovate",
}

// isBot returns true if a login should be treated as a bot account.
// A login is a bot if it contains any of the keywords as a case-insensitive
// substring (e.g. "[bot]" matches "dependabot[bot]", "claude" matches
// "anthropic-claude", etc.).
func isBot(login string, keywords []string) bool {
	lower := strings.ToLower(login)
	for _, kw := range keywords {
		if strings.Contains(lower, strings.ToLower(kw)) {
			return true
		}
	}
	return false
}

// Options controls Apply.
type Options struct {
	// BotKeywords is a list of keywords used to identify bot accounts.
	// A login is a bot if it contains "[bot]" OR any of these keywords
	// (case-insensitive substring match). Merged with DefaultBotKeywords.
	BotKeywords []string
	// Dedupe, when true, removes duplicate PR references (#123) that appear in
	// multiple sections. The occurrence in the highest-priority section wins.
	Dedupe bool
	// Warnings, if non-nil, receives human-readable notes such as "dropped
	// duplicate #123 from Updates (kept in Fixes)". Callers typically hand
	// these to os.Stderr.
	Warnings *[]string
}

// Section is a single "## Heading" block in the release body.
type Section struct {
	// Heading is the full heading line, e.g. "## 🐛 Fixes".
	Heading string
	// Lines are every line between this heading and the next heading or EOF,
	// inclusive of blank lines. The heading itself is NOT part of Lines.
	Lines []string
}

// Body is the parsed form of a release-drafter body.
type Body struct {
	// Preamble holds any text BEFORE the first H2 heading (usually empty).
	Preamble []string
	// Sections are the H2 blocks in document order.
	Sections []Section
	// Epilogue holds the post-sections footer that release-drafter emits via
	// its template (e.g. "Full Changelog" + "Thank you $CONTRIBUTORS ...").
	// We keep it as a separate slice so the emoji/prefix rules do not touch
	// it and the bot-filter rule has an unambiguous target.
	Epilogue []string
}

// sectionPriority ranks categories from most specific to least specific for
// the Dedupe rule. Keys are matched case-insensitively as substrings of the
// H2 heading (so "## 🐛 Fixes" matches "fixes").
var sectionPriority = []string{
	"breaking",
	"new",
	"feature",
	"fixes",
	"bug",
	"updates",
	"performance",
	"dependencies",
	"maintenance",
	"documentation",
	"docs",
}

// priorityOf returns a score where smaller = higher priority. Unknown
// headings rank after every known one (so they keep their bullets rather
// than losing them to a dedupe against a known category).
func priorityOf(heading string) int {
	h := strings.ToLower(heading)
	for i, key := range sectionPriority {
		if strings.Contains(h, key) {
			return i
		}
	}
	return len(sectionPriority) + 1
}

// headingRe matches an H2 heading at the start of a line.
var headingRe = regexp.MustCompile(`^##\s+`)

// bulletRe matches a bullet line. We only touch bullets (lines starting with
// "- "), never sub-bullets ("  - ...") — release-drafter does not produce
// those for its categories.
var bulletRe = regexp.MustCompile(`^- `)

// emojiRe strips ONE or more leading emoji/pictograph characters (plus the
// Variation-Selector-16 and Zero-Width-Joiner that emoji sequences use)
// followed by whitespace, from the start of a bullet's text.
//
// The ranges cover:
//
//	U+2300..U+23FF   Misc Technical (⌨, ⏲, …)
//	U+2600..U+27BF   Misc Symbols + Dingbats (✂, ☢, ✏, ❗, …)
//	U+2B00..U+2BFF   Misc Symbols and Arrows
//	U+1F300..U+1FAFF Supplemental Symbols & Pictographs, Emoji, Supplemental
//	                 Symbols-B, Symbols for Legacy Computing (covers almost
//	                 every modern emoji we will ever see)
//	U+FE0F           Variation Selector-16 (the "use emoji presentation" marker)
//	U+200D           Zero Width Joiner (used in compound emoji)
var emojiRe = regexp.MustCompile(
	`^([\x{2300}-\x{23FF}\x{2600}-\x{27BF}\x{2B00}-\x{2BFF}\x{1F300}-\x{1FAFF}\x{FE0F}\x{200D}]+\s+)`,
)

// convCommitRe matches a conventional-commit prefix at the start of a
// bullet's text. Case-insensitive. Handles optional scope and breaking "!".
var convCommitRe = regexp.MustCompile(
	`^(?i)(feat|fix|bug|docs|doc|chore|refactor|test|ci|perf|build|style|revert)(\([^)]+\))?!?:\s+`,
)

// prRefRe captures "#NNN" references inside a bullet.
var prRefRe = regexp.MustCompile(`#(\d+)`)

// thankYouRe matches the contributor footer release-drafter emits. The
// capture group holds the raw "@login" list.
var thankYouRe = regexp.MustCompile(
	`^Thank you(?: to)?\s+(.+?)\s+for making this (?:update|release) possible\.?\s*$`,
)

// contributorRe finds both "@login" mentions and "[login[bot]](url)" markdown
// links in the contributor footer. Group 1 captures @mention logins, group 2
// captures markdown-link bot logins.
var contributorRe = regexp.MustCompile(
	`(?:@([A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?)|\[([A-Za-z0-9][A-Za-z0-9-]*\[bot\])\]\([^)]+\))`,
)

// Parse splits body into Preamble / Sections / Epilogue.
//
// The epilogue heuristic: release-drafter's standard template emits
// "**Full Changelog**: ..." and/or "Thank you ... for making this update
// possible." AFTER the categorized bullets, with at least one blank line
// between them and the last section. We grab everything from the first such
// footer marker (or the trailing blank-line-then-body boundary) to EOF.
func Parse(body string) Body {
	lines := strings.Split(body, "\n")

	// Find section starts.
	var sectionStarts []int
	for i, ln := range lines {
		if headingRe.MatchString(ln) {
			sectionStarts = append(sectionStarts, i)
		}
	}

	if len(sectionStarts) == 0 {
		// No sections — treat the whole thing as preamble. Apply becomes a
		// no-op for the emoji/prefix/dedupe rules; the bot filter still runs
		// on the epilogue if it happens to be at the tail.
		return Body{Preamble: lines}
	}

	// Everything before the first heading is preamble.
	preamble := append([]string(nil), lines[:sectionStarts[0]]...)

	// Find where the epilogue starts. We walk backwards from EOF and keep
	// lines that look like epilogue content ("Full Changelog", "Thank you",
	// blank lines separating them) until we hit a line that belongs to a
	// section body.
	lastSectionStart := sectionStarts[len(sectionStarts)-1]
	epilogueStart := len(lines)
	for i := len(lines) - 1; i > lastSectionStart; i-- {
		ln := strings.TrimSpace(lines[i])
		if ln == "" {
			epilogueStart = i
			continue
		}
		if strings.Contains(ln, "Full Changelog") || thankYouRe.MatchString(ln) {
			epilogueStart = i
			continue
		}
		// Hit content belonging to the last section — stop expanding epilogue.
		break
	}

	// Build sections.
	var sections []Section
	for idx, start := range sectionStarts {
		end := epilogueStart
		if idx+1 < len(sectionStarts) {
			end = sectionStarts[idx+1]
		}
		sec := Section{Heading: lines[start]}
		if start+1 <= end {
			sec.Lines = append([]string(nil), lines[start+1:end]...)
		}
		sections = append(sections, sec)
	}

	var epilogue []string
	if epilogueStart < len(lines) {
		epilogue = append([]string(nil), lines[epilogueStart:]...)
	}

	return Body{Preamble: preamble, Sections: sections, Epilogue: epilogue}
}

// Render is Parse's inverse.
func (b Body) Render() string {
	var parts []string
	parts = append(parts, b.Preamble...)
	for _, sec := range b.Sections {
		parts = append(parts, sec.Heading)
		parts = append(parts, sec.Lines...)
	}
	parts = append(parts, b.Epilogue...)
	return strings.Join(parts, "\n")
}

// StripBulletEmoji removes leading emoji from every bullet line in every
// section. Headings and epilogue are left alone.
func (b *Body) StripBulletEmoji() {
	for si := range b.Sections {
		for li, ln := range b.Sections[si].Lines {
			if !bulletRe.MatchString(ln) {
				continue
			}
			after := ln[2:] // strip "- "
			after = emojiRe.ReplaceAllString(after, "")
			b.Sections[si].Lines[li] = "- " + after
		}
	}
}

// StripConvCommitPrefix removes "feat:", "fix(scope):" etc. from every bullet
// line in every section. Run AFTER StripBulletEmoji so that bullets like
// "- 🐛 fix: foo" collapse to "- foo".
func (b *Body) StripConvCommitPrefix() {
	for si := range b.Sections {
		for li, ln := range b.Sections[si].Lines {
			if !bulletRe.MatchString(ln) {
				continue
			}
			after := ln[2:]
			after = convCommitRe.ReplaceAllString(after, "")
			b.Sections[si].Lines[li] = "- " + after
		}
	}
}

// FilterBotContributors removes bot mentions from the "Thank you ..." line in
// the epilogue. If the list is empty after filtering, the whole line is
// dropped.
func (b *Body) FilterBotContributors(keywords []string, warn func(string)) {
	if len(b.Epilogue) == 0 {
		return
	}

	for i, ln := range b.Epilogue {
		m := thankYouRe.FindStringSubmatch(ln)
		if m == nil {
			continue
		}
		mentions := contributorRe.FindAllStringSubmatch(m[1], -1)
		var kept []string
		dropped := 0
		for _, mn := range mentions {
			login := mn[1] // @mention format
			if login == "" {
				login = mn[2] // [bot](url) markdown format
			}
			if isBot(login, keywords) {
				dropped++
				continue
			}
			kept = append(kept, "@"+login)
		}
		switch {
		case len(kept) == 0:
			// Remove the whole line.
			b.Epilogue = append(b.Epilogue[:i], b.Epilogue[i+1:]...)
			// Also remove a directly preceding blank line if present, to
			// avoid leaving a double-blank gap.
			if i > 0 && strings.TrimSpace(b.Epilogue[i-1]) == "" {
				b.Epilogue = append(b.Epilogue[:i-1], b.Epilogue[i:]...)
			}
			if warn != nil && dropped > 0 {
				warn(fmt.Sprintf("removed contributor footer (all %d authors were bots)", dropped))
			}
		case dropped > 0:
			var rebuilt string
			if len(kept) == 1 {
				rebuilt = kept[0]
			} else {
				rebuilt = strings.Join(kept[:len(kept)-1], ", ") + " and " + kept[len(kept)-1]
			}
			// Preserve the original wording (update/release/etc.)
			suffix := "for making this update possible."
			if strings.Contains(ln, "release possible") {
				suffix = "for making this release possible."
			}
			b.Epilogue[i] = "Thank you " + rebuilt + " " + suffix
			if warn != nil {
				warn(fmt.Sprintf("filtered %d bot(s) from contributor footer", dropped))
			}
		}
		return // there is only ever one "Thank you" line
	}
}

// Dedupe drops bullets whose "#NNN" reference already appears in a
// higher-priority section. A bullet with no "#NNN" is never dropped.
func (b *Body) Dedupe(warn func(string)) {
	// Rank sections.
	type ranked struct {
		idx  int
		prio int
	}
	ranks := make([]ranked, len(b.Sections))
	for i, sec := range b.Sections {
		ranks[i] = ranked{idx: i, prio: priorityOf(sec.Heading)}
	}
	order := append([]ranked(nil), ranks...)
	sort.SliceStable(order, func(i, j int) bool { return order[i].prio < order[j].prio })

	seen := make(map[string]string) // PR ref -> heading that kept it
	for _, r := range order {
		sec := &b.Sections[r.idx]
		kept := sec.Lines[:0]
		for _, ln := range sec.Lines {
			refs := prRefRe.FindAllStringSubmatch(ln, -1)
			if len(refs) == 0 {
				kept = append(kept, ln)
				continue
			}
			// Use the first PR ref on the line as its identity.
			ref := "#" + refs[0][1]
			if prev, dup := seen[ref]; dup {
				if warn != nil {
					warn(fmt.Sprintf("dedupe: dropped %s from %q (kept in %q)",
						ref, strings.TrimSpace(sec.Heading), strings.TrimSpace(prev)))
				}
				continue
			}
			seen[ref] = sec.Heading
			kept = append(kept, ln)
		}
		sec.Lines = kept
	}
}

// Apply is the convenience orchestrator used by main.go. It runs the rules
// in the documented order and returns the cleaned body.
func Apply(body string, opts Options) string {
	parsed := Parse(body)

	parsed.StripBulletEmoji()
	parsed.StripConvCommitPrefix()

	warn := func(msg string) {
		if opts.Warnings != nil {
			*opts.Warnings = append(*opts.Warnings, msg)
		}
	}

	// Merge caller keywords with defaults for bot detection.
	keywords := append(append([]string(nil), DefaultBotKeywords...), opts.BotKeywords...)
	parsed.FilterBotContributors(keywords, warn)

	if opts.Dedupe {
		parsed.Dedupe(warn)
	}

	return parsed.Render()
}
