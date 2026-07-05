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
	branchRuns, err := g.listRuns(org, repo, url.Values{"branch": {branch}})
	if err != nil {
		return nil, err
	}
	findings := detectBranchFailures(repo, branchRuns)

	since := now.Add(-time.Duration(th.CrossPRWindowHours) * time.Hour)
	prRuns, err := g.listRuns(org, repo, url.Values{
		"event":   {"pull_request"},
		"status":  {"failure"},
		"created": {">=" + since.Format(time.RFC3339)},
	})
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

// detectBranchFailures reports default-branch workflows whose latest completed
// run failed or timed out while the run before it succeeded (edge-triggered: an
// already-red workflow does not re-alert). A timeout is treated as a failure:
// a job that used to pass and now hangs until the runner kills it is a hung
// dependency, the same infra signal a hard failure is. A failure on the same
// commit that was green before can only be the environment, never the code;
// that distinction is surfaced as its own check.
func detectBranchFailures(repo string, runs []workflowRun) []Finding {
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
		switch latest.Conclusion {
		case "failure", "timed_out":
			if len(rs) < 2 || rs[1].Conclusion != "success" {
				continue
			}
			prev := rs[1]
			check := checkMasterFailure
			detail := fmt.Sprintf("previous run on this branch was green (commit %.7s -> %.7s)", prev.HeadSHA, latest.HeadSHA)
			if latest.HeadSHA == prev.HeadSHA {
				check = checkSameSHAFlip
				detail = fmt.Sprintf("commit %.7s was green and is red now with no code change, the environment broke", latest.HeadSHA)
				if latest.Event == "schedule" {
					check = checkScheduledFailure
					detail = fmt.Sprintf("scheduled run went red on commit %.7s with no new commits, the environment broke", latest.HeadSHA)
				}
			}
			findings = append(findings, Finding{
				Repo:     repo,
				Check:    check,
				Workflow: latest.Name,
				Title:    fmt.Sprintf("%s: %s failed on %s", repo, latest.Name, latest.HeadBranch),
				Detail:   detail,
				URL:      latest.HTMLURL,
				Key:      fmt.Sprintf("%s/%s/%d/run-%d", repo, check, latest.WorkflowID, latest.ID),
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
		"openPRs":    "repo:" + full + " is:pr is:open",
		"openIssues": "repo:" + full + " is:issue is:open",
		"stale":      "repo:" + full + " is:pr is:open draft:false review:none created:<" + staleDate,
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
