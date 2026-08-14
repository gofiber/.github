package main

import (
	"fmt"
	"net/url"
	"sort"
	"strings"
	"time"
)

type Finding struct {
	Repo     string
	Check    string
	Workflow string
	Title    string
	Detail   string
	URL      string
	Key      string // dedup/cooldown key in the state file
}

const (
	checkMasterFailure    = "master-failure"
	checkScheduledFailure = "scheduled-failure"
	checkSameSHAFlip      = "same-sha-flip"
	checkStartupFailure   = "startup-failure"
	checkCrossPR          = "cross-pr"
	checkDeadWorkflow     = "dead-workflow"
	checkPRBacklog        = "pr-backlog"
	checkIssueBacklog     = "issue-backlog"
	checkStalePRs         = "stale-prs"
	checkStuckBotPRs      = "stuck-bot-prs"
	checkUnansweredIssues = "unanswered-issues"
	checkIssueSpike       = "issue-spike"
)

// scanRepo runs the frequent checks: default-branch failures and workflows
// failing across several PRs at once.
func scanRepo(g *gitHub, org, repo string, th Thresholds, now time.Time) ([]Finding, error) {
	branch, err := g.defaultBranch(org, repo)
	if err != nil {
		return nil, err
	}
	branchRuns, err := g.listRuns(org, repo, url.Values{"branch": {branch}}, now.Add(-maxFailureAge))
	if err != nil {
		return nil, err
	}
	findings := detectBranchFailures(repo, branchRuns, now)

	since := now.Add(-time.Duration(th.CrossPRWindowHours) * time.Hour)
	prRuns, err := g.listRuns(org, repo, url.Values{
		"event":   {"pull_request"},
		"status":  {"failure"},
		"created": {">=" + since.Format(time.RFC3339)},
	}, since)
	if err != nil {
		return nil, err
	}
	var recentBranchRuns []workflowRun
	for _, r := range branchRuns {
		if r.CreatedAt.After(since) {
			recentBranchRuns = append(recentBranchRuns, r)
		}
	}
	findings = append(findings, detectCrossPRFailures(repo, prRuns, recentBranchRuns, th.CrossPRMinPRs)...)
	return findings, nil
}

// A deleted or dormant workflow keeps its last red run as the latest one
// forever, so without an age bound it re-fires every time its state entry
// expires. utils/Modernize Lint was reported that way since 2026-05-25.
const maxFailureAge = 7 * 24 * time.Hour

// detectBranchFailures reports default-branch workflows whose latest completed
// run failed or timed out while the run before it succeeded (edge-triggered: an
// already-red workflow does not re-alert). A timeout is treated as a failure:
// a job that used to pass and now hangs until the runner kills it is a hung
// dependency, the same infra signal a hard failure is. A failure on the same
// commit that was green before can only be the environment, never the code;
// that distinction is surfaced as its own check.
func detectBranchFailures(repo string, runs []workflowRun, now time.Time) []Finding {
	byWorkflow := map[int64][]workflowRun{}
	for _, r := range runs { // runs arrive newest first
		if r.Status != "completed" {
			continue
		}
		// The branch filter matches head_branch, so a fork PR whose branch is
		// named like the default branch shows up here. PR events are never
		// default-branch signal.
		if r.Event == "pull_request" || r.Event == "pull_request_target" {
			continue
		}
		// GitHub's own managed jobs (dependabot updates, dependency graph,
		// default-setup CodeQL) run as "dynamic". They are not the repo's CI:
		// their failures are dependency problems, they report in their own tab,
		// and their run name carries the updated directories, which makes an
		// unreadable alert title. 6 of the green-to-red edges over 30 days came
		// from them, 4 of those from one known npm pin in docs.
		if r.Event == "dynamic" {
			continue
		}
		// Cancelled/skipped runs carry no green/red signal; keeping them would
		// mask the transition edge (cancel-in-progress on busy branches).
		if r.Conclusion == "cancelled" || r.Conclusion == "skipped" {
			continue
		}
		byWorkflow[r.WorkflowID] = append(byWorkflow[r.WorkflowID], r)
	}

	var findings []Finding
	for _, rs := range byWorkflow {
		latest := rs[0]
		if now.Sub(latest.CreatedAt) > maxFailureAge {
			continue
		}
		switch latest.Conclusion {
		case "failure", "timed_out":
			// Walk back over the failures to the run that broke the streak.
			// Comparing only the newest two loses the transition whenever more
			// than one run lands between two scans, which a batch merge does
			// routinely: 59% of the green-to-red edges over 30 days were never
			// the newest run at any poll.
			i := 0
			for i+1 < len(rs) && (rs[i+1].Conclusion == "failure" || rs[i+1].Conclusion == "timed_out") {
				i++
			}
			if i+1 >= len(rs) || rs[i+1].Conclusion != "success" {
				continue
			}
			broke, prev := rs[i], rs[i+1]
			// The streak, not the newest run, dates the incident: a workflow
			// that has been red for weeks is not news, however fresh its last
			// attempt is.
			if now.Sub(broke.CreatedAt) > maxFailureAge {
				continue
			}
			check := checkMasterFailure
			detail := fmt.Sprintf("previous run on this branch was green (commit %.7s -> %.7s)", prev.HeadSHA, broke.HeadSHA)
			if broke.HeadSHA == prev.HeadSHA {
				check = checkSameSHAFlip
				detail = fmt.Sprintf("commit %.7s was green and is red now with no code change, the environment broke", broke.HeadSHA)
				if broke.Event == "schedule" {
					check = checkScheduledFailure
					detail = fmt.Sprintf("scheduled run went red on commit %.7s with no new commits, the environment broke", broke.HeadSHA)
				}
			}
			if i > 0 {
				detail += fmt.Sprintf(", %d more failed since", i)
			}
			findings = append(findings, Finding{
				Repo:     repo,
				Check:    check,
				Workflow: broke.Name,
				Title:    fmt.Sprintf("%s: %s failed on %s", repo, broke.Name, broke.HeadBranch),
				Detail:   detail,
				URL:      broke.HTMLURL,
				// Keyed by workflow alone, not by run and not by classification:
				// both would hand a flapping workflow a fresh key on every flip
				// and let it alert past its cooldown (storage Benchmark, 3x in 8h,
				// and it alternates between push and schedule runs).
				Key: fmt.Sprintf("%s/branch-failure/%d", repo, latest.WorkflowID),
			})
		case "startup_failure":
			findings = append(findings, Finding{
				Repo:     repo,
				Check:    checkStartupFailure,
				Workflow: latest.Name,
				Title:    fmt.Sprintf("%s: %s cannot start", repo, latest.Name),
				Detail:   "the workflow file itself is broken (startup_failure)",
				URL:      latest.HTMLURL,
				Key:      fmt.Sprintf("%s/%s/%d", repo, checkStartupFailure, latest.WorkflowID),
			})
		}
	}
	sortFindings(findings)
	return findings
}

// detectCrossPRFailures flags a workflow that failed on at least minPRs
// distinct PR branches inside the window. Unrelated PRs cannot all be at
// fault, so the shared infrastructure is. A single PR failing repeatedly
// stays below the threshold by design: that is the PR's own problem.
//
// Exception (reference case D): grouped dependency bumps open many PRs at
// once that all fail on their own breaking change, which looks systemic
// but is not. When every failing run was opened by the same bot AND the
// same workflow had a green run on the default branch inside the window
// (the environment is provably healthy), the finding is suppressed. With
// no green default-branch run the finding still fires, annotated with the
// bot name so the reader can judge.
func detectCrossPRFailures(repo string, prRuns, defaultBranchRuns []workflowRun, minPRs int) []Finding {
	greenOnDefault := map[int64]bool{}
	for _, r := range defaultBranchRuns {
		if r.Status == "completed" && r.Conclusion == "success" {
			greenOnDefault[r.WorkflowID] = true
		}
	}

	type agg struct {
		branches map[string]bool
		actors   map[string]bool
		latest   workflowRun
	}
	byWorkflow := map[int64]*agg{}
	for _, r := range prRuns { // newest first
		if r.Conclusion != "failure" {
			continue
		}
		a := byWorkflow[r.WorkflowID]
		if a == nil {
			a = &agg{branches: map[string]bool{}, actors: map[string]bool{}, latest: r}
			byWorkflow[r.WorkflowID] = a
		}
		a.branches[r.HeadBranch] = true
		a.actors[r.Actor.Login] = true
	}

	var findings []Finding
	for id, a := range byWorkflow {
		if len(a.branches) < minPRs {
			continue
		}
		bot, soleBot := soleBotActor(a.actors)
		if soleBot && greenOnDefault[id] {
			continue
		}
		detail := "the same workflow fails on PRs from different branches, this is systemic, not the PRs' fault"
		if soleBot {
			detail = fmt.Sprintf("the same workflow fails on PRs from different branches, all opened by %s; grouped dependency bumps can look like this without being systemic, but there is no green default-branch run in the window to clear the environment", bot)
		}
		findings = append(findings, Finding{
			Repo:     repo,
			Check:    checkCrossPR,
			Workflow: a.latest.Name,
			Title:    fmt.Sprintf("%s: %s is failing across %d PRs", repo, a.latest.Name, len(a.branches)),
			Detail:   detail,
			URL:      a.latest.HTMLURL,
			Key:      fmt.Sprintf("%s/%s/%d", repo, checkCrossPR, id),
		})
	}
	sortFindings(findings)
	return findings
}

// soleBotActor reports whether every aggregated run came from one single
// actor and that actor is a bot account.
func soleBotActor(actors map[string]bool) (string, bool) {
	if len(actors) != 1 {
		return "", false
	}
	for a := range actors {
		return a, strings.HasSuffix(a, "[bot]")
	}
	return "", false
}

// The line is auto-merged or not, not bot or human: dependency PRs merge
// themselves within minutes and swing by ten per batch, which would drown the
// review signal, so they get their own check. Anything else a bot opens
// (copilot, renovate) waits for a human like any PR and stays review debt.
// Repeated author: qualifiers are OR'd by the search API.
const (
	excludeAutoMerged = " -author:app/dependabot -author:app/github-actions"
	onlyAutoMerged    = " author:app/dependabot author:app/github-actions"
)

// digestRepo runs the daily backlog and hygiene checks.
func digestRepo(g *gitHub, org, repo string, th Thresholds, now time.Time) ([]Finding, error) {
	var findings []Finding
	full := org + "/" + repo

	// All six counts come back in a single GraphQL request, off the REST search
	// rate limit. Issue spike: the last 24h against the 14-day average; a sudden
	// burst of new issues is the earliest external signal of a broken release.
	staleDate := now.AddDate(0, 0, -th.StalePRDays).Format("2006-01-02")
	unansweredDate := now.AddDate(0, 0, -th.UnansweredIssueDays).Format("2006-01-02")
	counts, err := g.searchCounts(map[string]string{
		"openPRs":    "repo:" + full + " is:pr is:open" + excludeAutoMerged,
		"openIssues": "repo:" + full + " is:issue is:open",
		"stale":      "repo:" + full + " is:pr is:open draft:false review:none created:<" + staleDate + excludeAutoMerged,
		"stuckBots":  "repo:" + full + " is:pr is:open draft:false created:<" + staleDate + onlyAutoMerged,
		"unanswered": "repo:" + full + " is:issue is:open comments:0 created:<" + unansweredDate,
		"lastDay":    "repo:" + full + " is:issue created:>=" + now.Add(-24*time.Hour).Format("2006-01-02T15:04:05Z"),
		"twoWeeks":   "repo:" + full + " is:issue created:>=" + now.AddDate(0, 0, -14).Format("2006-01-02"),
	})
	if err != nil {
		return nil, err
	}
	openPRs := counts["openPRs"]
	openIssues := counts["openIssues"]
	stale := counts["stale"]
	stuckBots := counts["stuckBots"]
	unanswered := counts["unanswered"]
	lastDay := counts["lastDay"]
	twoWeeks := counts["twoWeeks"]

	if openPRs > th.MaxOpenPRs {
		findings = append(findings, Finding{
			Repo:   repo,
			Check:  checkPRBacklog,
			Title:  fmt.Sprintf("%s: PR backlog", repo),
			Detail: fmt.Sprintf("%d open PRs (threshold %d)", openPRs, th.MaxOpenPRs),
			URL:    fmt.Sprintf("https://github.com/%s/pulls", full),
			Key:    repo + "/" + checkPRBacklog,
		})
	}

	if openIssues > th.MaxOpenIssues {
		findings = append(findings, Finding{
			Repo:   repo,
			Check:  checkIssueBacklog,
			Title:  fmt.Sprintf("%s: issue backlog", repo),
			Detail: fmt.Sprintf("%d open issues (threshold %d)", openIssues, th.MaxOpenIssues),
			URL:    fmt.Sprintf("https://github.com/%s/issues", full),
			Key:    repo + "/" + checkIssueBacklog,
		})
	}

	if stale > th.MaxStalePRs {
		findings = append(findings, Finding{
			Repo:   repo,
			Check:  checkStalePRs,
			Title:  fmt.Sprintf("%s: PRs without review", repo),
			Detail: fmt.Sprintf("%d open PRs older than %d days with no review (threshold %d)", stale, th.StalePRDays, th.MaxStalePRs),
			URL:    fmt.Sprintf("https://github.com/%s/pulls?q=is%%3Apr+is%%3Aopen+draft%%3Afalse+review%%3Anone", full),
			Key:    repo + "/" + checkStalePRs,
		})
	}

	if stuckBots > th.MaxStuckBotPRs {
		findings = append(findings, Finding{
			Repo:   repo,
			Check:  checkStuckBotPRs,
			Title:  fmt.Sprintf("%s: dependency PRs are not merging", repo),
			Detail: fmt.Sprintf("%d bot PRs open for more than %d days (threshold %d), auto-merge holds back majors by design but the rest means its CI is red", stuckBots, th.StalePRDays, th.MaxStuckBotPRs),
			// Same filters as the count, age included: a link that lists every
			// open bot PR next to a number that only counts the old ones reads
			// like the check miscounted.
			URL: fmt.Sprintf("https://github.com/%s/pulls?q=is%%3Apr+is%%3Aopen+draft%%3Afalse+created%%3A%%3C%s+author%%3Aapp%%2Fdependabot+author%%3Aapp%%2Fgithub-actions", full, staleDate),
			Key: repo + "/" + checkStuckBotPRs,
		})
	}

	if unanswered > th.MaxUnansweredIssues {
		findings = append(findings, Finding{
			Repo:   repo,
			Check:  checkUnansweredIssues,
			Title:  fmt.Sprintf("%s: unanswered issues", repo),
			Detail: fmt.Sprintf("%d open issues older than %d days with zero comments (threshold %d)", unanswered, th.UnansweredIssueDays, th.MaxUnansweredIssues),
			URL:    fmt.Sprintf("https://github.com/%s/issues?q=is%%3Aissue+is%%3Aopen+comments%%3A0", full),
			Key:    repo + "/" + checkUnansweredIssues,
		})
	}

	avg := float64(twoWeeks) / 14
	if lastDay >= th.IssueSpikeMinCount && float64(lastDay) >= th.IssueSpikeFactor*avg {
		findings = append(findings, Finding{
			Repo:   repo,
			Check:  checkIssueSpike,
			Title:  fmt.Sprintf("%s: issue spike", repo),
			Detail: fmt.Sprintf("%d new issues in 24h against a 14-day average of %.1f/day, possibly a broken release", lastDay, avg),
			URL:    fmt.Sprintf("https://github.com/%s/issues?q=is%%3Aissue+sort%%3Acreated-desc", full),
			Key:    repo + "/" + checkIssueSpike,
		})
	}

	workflows, err := g.listWorkflows(org, repo)
	if err != nil {
		return nil, err
	}
	for _, w := range workflows {
		if w.State != "disabled_inactivity" {
			continue
		}
		findings = append(findings, Finding{
			Repo:     repo,
			Check:    checkDeadWorkflow,
			Workflow: w.Name,
			Title:    fmt.Sprintf("%s: %s was disabled by GitHub", repo, w.Name),
			Detail:   "scheduled workflow disabled after 60 days of repo inactivity, re-enable it if it is still needed",
			URL:      w.HTMLURL,
			Key:      fmt.Sprintf("%s/%s/%d", repo, checkDeadWorkflow, w.ID),
		})
	}

	sortFindings(findings)
	return findings, nil
}

func sortFindings(fs []Finding) {
	sort.Slice(fs, func(i, j int) bool { return fs[i].Key < fs[j].Key })
}
