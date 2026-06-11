package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"
)

type Thresholds struct {
	MaxOpenPRs          int     `json:"maxOpenPRs"`
	MaxOpenIssues       int     `json:"maxOpenIssues"`
	StalePRDays         int     `json:"stalePRDays"`
	MaxStalePRs         int     `json:"maxStalePRs"`
	UnansweredIssueDays int     `json:"unansweredIssueDays"`
	MaxUnansweredIssues int     `json:"maxUnansweredIssues"`
	CrossPRMinPRs       int     `json:"crossPRMinPRs"`
	CrossPRWindowHours  int     `json:"crossPRWindowHours"`
	IssueSpikeFactor    float64 `json:"issueSpikeFactor"`
	IssueSpikeMinCount  int     `json:"issueSpikeMinCount"`
	CooldownHours       int     `json:"cooldownHours"`
}

type Config struct {
	Org           string                `json:"org"`
	Repos         []string              `json:"repos"`        // empty: discover public non-archived repos via API
	ExcludeRepos  []string              `json:"excludeRepos"` // only applied to discovered repos
	Defaults      Thresholds            `json:"defaults"`
	RepoOverrides map[string]Thresholds `json:"repoOverrides"`
}

func loadConfig(path string) (*Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if c.Org == "" {
		return nil, fmt.Errorf("%s: org is required", path)
	}
	return &c, nil
}

func excludeRepos(repos, exclude []string) []string {
	if len(exclude) == 0 {
		return repos
	}
	skip := map[string]bool{}
	for _, e := range exclude {
		skip[e] = true
	}
	var out []string
	for _, r := range repos {
		if !skip[r] {
			out = append(out, r)
		}
	}
	return out
}

// thresholds returns the defaults with any non-zero per-repo override applied.
func (c *Config) thresholds(repo string) Thresholds {
	t := c.Defaults
	o, ok := c.RepoOverrides[repo]
	if !ok {
		return t
	}
	if o.MaxOpenPRs > 0 {
		t.MaxOpenPRs = o.MaxOpenPRs
	}
	if o.MaxOpenIssues > 0 {
		t.MaxOpenIssues = o.MaxOpenIssues
	}
	if o.StalePRDays > 0 {
		t.StalePRDays = o.StalePRDays
	}
	if o.MaxStalePRs > 0 {
		t.MaxStalePRs = o.MaxStalePRs
	}
	if o.UnansweredIssueDays > 0 {
		t.UnansweredIssueDays = o.UnansweredIssueDays
	}
	if o.MaxUnansweredIssues > 0 {
		t.MaxUnansweredIssues = o.MaxUnansweredIssues
	}
	if o.CrossPRMinPRs > 0 {
		t.CrossPRMinPRs = o.CrossPRMinPRs
	}
	if o.CrossPRWindowHours > 0 {
		t.CrossPRWindowHours = o.CrossPRWindowHours
	}
	if o.IssueSpikeFactor > 0 {
		t.IssueSpikeFactor = o.IssueSpikeFactor
	}
	if o.IssueSpikeMinCount > 0 {
		t.IssueSpikeMinCount = o.IssueSpikeMinCount
	}
	if o.CooldownHours > 0 {
		t.CooldownHours = o.CooldownHours
	}
	return t
}

// A Suppression mutes findings for a known, tracked problem. Until is
// mandatory; entries without a valid expiry date never match, so exceptions
// cannot accumulate silently.
type Suppression struct {
	Repo     string `json:"repo"`
	Check    string `json:"check"`
	Workflow string `json:"workflow"`
	Until    string `json:"until"` // YYYY-MM-DD, inclusive
	Reason   string `json:"reason"`
}

func loadSuppressions(path string) ([]Suppression, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var s []Suppression
	if err := json.Unmarshal(b, &s); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return s, nil
}

func applySuppressions(findings []Finding, sups []Suppression, now time.Time, logf func(string, ...any)) []Finding {
	var out []Finding
	for _, f := range findings {
		if s, ok := matchSuppression(f, sups, now); ok {
			logf("suppressed until %s: %s (%s)", s.Until, f.Title, s.Reason)
			continue
		}
		out = append(out, f)
	}
	return out
}

func matchSuppression(f Finding, sups []Suppression, now time.Time) (Suppression, bool) {
	for _, s := range sups {
		until, err := time.Parse("2006-01-02", s.Until)
		if err != nil {
			continue
		}
		if !now.Before(until.AddDate(0, 0, 1)) {
			continue
		}
		if s.Repo != "" && s.Repo != "*" && s.Repo != f.Repo {
			continue
		}
		if s.Check != "" && s.Check != "*" && s.Check != f.Check {
			continue
		}
		if s.Workflow != "" && s.Workflow != "*" && s.Workflow != f.Workflow {
			continue
		}
		return s, true
	}
	return Suppression{}, false
}
