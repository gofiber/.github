package main

import (
	"strings"
	"testing"
	"time"
)

func run(id, workflowID int64, branch, sha, event, conclusion string) workflowRun {
	return workflowRun{
		ID:         id,
		Name:       "Tests",
		WorkflowID: workflowID,
		HeadBranch: branch,
		HeadSHA:    sha,
		Event:      event,
		Status:     "completed",
		Conclusion: conclusion,
	}
}

func TestDetectBranchFailuresEdgeTriggered(t *testing.T) {
	// Newest first, as the API returns them.
	cases := []struct {
		name string
		runs []workflowRun
		want int
	}{
		{"green to red fires", []workflowRun{
			run(2, 1, "main", "bbb", "push", "failure"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 1},
		{"green to timeout fires", []workflowRun{
			run(2, 1, "main", "bbb", "push", "timed_out"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 1},
		{"still timing out stays silent", []workflowRun{
			run(3, 1, "main", "ccc", "push", "timed_out"),
			run(2, 1, "main", "bbb", "push", "timed_out"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 0},
		{"still red stays silent", []workflowRun{
			run(3, 1, "main", "ccc", "push", "failure"),
			run(2, 1, "main", "bbb", "push", "failure"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 0},
		{"green stays silent", []workflowRun{
			run(2, 1, "main", "bbb", "push", "success"),
			run(1, 1, "main", "aaa", "push", "failure"),
		}, 0},
		{"first run ever red stays silent", []workflowRun{
			run(1, 1, "main", "aaa", "push", "failure"),
		}, 0},
		{"cancelled run does not mask the edge", []workflowRun{
			run(3, 1, "main", "ccc", "push", "failure"),
			run(2, 1, "main", "bbb", "push", "cancelled"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 1},
		{"fork PR on a branch named main is not default-branch signal", []workflowRun{
			run(2, 1, "main", "bbb", "pull_request", "failure"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := detectBranchFailures("fiber", tc.runs); len(got) != tc.want {
				t.Fatalf("got %d findings, want %d: %+v", len(got), tc.want, got)
			}
		})
	}
}

func TestDetectBranchFailuresClassification(t *testing.T) {
	cases := []struct {
		name string
		runs []workflowRun
		want string
	}{
		{"new commit is a plain master failure", []workflowRun{
			run(2, 1, "main", "bbb", "push", "failure"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, checkMasterFailure},
		{"same sha rerun flip blames the environment", []workflowRun{
			run(2, 1, "main", "aaa", "push", "failure"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, checkSameSHAFlip},
		{"scheduled run without new commits blames the environment", []workflowRun{
			run(2, 1, "main", "aaa", "schedule", "failure"),
			run(1, 1, "main", "aaa", "schedule", "success"),
		}, checkScheduledFailure},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := detectBranchFailures("template", tc.runs)
			if len(got) != 1 {
				t.Fatalf("got %d findings, want 1", len(got))
			}
			if got[0].Check != tc.want {
				t.Fatalf("got check %q, want %q", got[0].Check, tc.want)
			}
		})
	}
}

func TestDetectBranchFailuresStartupFailure(t *testing.T) {
	got := detectBranchFailures("template", []workflowRun{
		run(2, 1, "main", "bbb", "push", "startup_failure"),
		run(1, 1, "main", "aaa", "push", "startup_failure"),
	})
	if len(got) != 1 || got[0].Check != checkStartupFailure {
		t.Fatalf("got %+v, want one startup-failure finding", got)
	}
}

func actorRun(id, workflowID int64, branch, actor string) workflowRun {
	r := run(id, workflowID, branch, branch+"-sha", "pull_request", "failure")
	r.Actor.Login = actor
	return r
}

func TestDetectCrossPRFailures(t *testing.T) {
	threeBranches := []workflowRun{
		run(3, 7, "feat-c", "ccc", "pull_request", "failure"),
		run(2, 7, "feat-b", "bbb", "pull_request", "failure"),
		run(1, 7, "feat-a", "aaa", "pull_request", "failure"),
	}
	if got := detectCrossPRFailures("storage", threeBranches, nil, 3); len(got) != 1 {
		t.Fatalf("3 distinct branches: got %d findings, want 1", len(got))
	}

	onePRRetried := []workflowRun{
		run(3, 7, "feat-a", "a3", "pull_request", "failure"),
		run(2, 7, "feat-a", "a2", "pull_request", "failure"),
		run(1, 7, "feat-a", "a1", "pull_request", "failure"),
	}
	if got := detectCrossPRFailures("storage", onePRRetried, nil, 3); len(got) != 0 {
		t.Fatalf("one PR retried: got %d findings, want 0", len(got))
	}

	twoBranches := threeBranches[1:]
	if got := detectCrossPRFailures("storage", twoBranches, nil, 3); len(got) != 0 {
		t.Fatalf("below threshold: got %d findings, want 0", len(got))
	}
}

// Reference case D: grouped dependency bumps fail many PRs at once while
// the default branch stays green; that is the bumps' own problem.
func TestDetectCrossPRFailuresBotSuppression(t *testing.T) {
	botPRs := []workflowRun{
		actorRun(3, 7, "dependabot/a", "dependabot[bot]"),
		actorRun(2, 7, "dependabot/b", "dependabot[bot]"),
		actorRun(1, 7, "dependabot/c", "dependabot[bot]"),
	}
	greenMain := []workflowRun{run(10, 7, "main", "mmm", "push", "success")}
	redMain := []workflowRun{run(10, 7, "main", "mmm", "push", "failure")}
	otherWorkflowGreen := []workflowRun{run(10, 8, "main", "mmm", "push", "success")}
	mixedActors := append([]workflowRun{actorRun(4, 7, "feat-x", "human")}, botPRs...)

	if got := detectCrossPRFailures("storage", botPRs, greenMain, 3); len(got) != 0 {
		t.Fatalf("sole bot actor with green default branch: got %d findings, want 0", len(got))
	}
	got := detectCrossPRFailures("storage", botPRs, redMain, 3)
	if len(got) != 1 {
		t.Fatalf("sole bot actor without green default branch: got %d findings, want 1", len(got))
	}
	if !strings.Contains(got[0].Detail, "dependabot[bot]") {
		t.Fatalf("finding should name the bot, got detail %q", got[0].Detail)
	}
	if got := detectCrossPRFailures("storage", botPRs, otherWorkflowGreen, 3); len(got) != 1 {
		t.Fatalf("green run of a different workflow must not suppress: got %d findings, want 1", len(got))
	}
	if got := detectCrossPRFailures("storage", mixedActors, greenMain, 3); len(got) != 1 {
		t.Fatalf("mixed actors stay systemic despite green default branch: got %d findings, want 1", len(got))
	}
}

func TestMatchSuppression(t *testing.T) {
	now := time.Date(2026, 6, 11, 12, 0, 0, 0, time.UTC)
	f := Finding{Repo: "storage", Check: checkCrossPR, Workflow: "Tests"}

	active := []Suppression{{Repo: "storage", Check: checkCrossPR, Until: "2026-07-01"}}
	if _, ok := matchSuppression(f, active, now); !ok {
		t.Fatal("active suppression should match")
	}
	if _, ok := matchSuppression(f, []Suppression{{Repo: "storage", Check: checkCrossPR, Until: "2026-06-11"}}, now); !ok {
		t.Fatal("until date is inclusive")
	}
	if _, ok := matchSuppression(f, []Suppression{{Repo: "storage", Check: checkCrossPR, Until: "2026-06-10"}}, now); ok {
		t.Fatal("expired suppression must not match")
	}
	if _, ok := matchSuppression(f, []Suppression{{Repo: "storage", Check: checkCrossPR}}, now); ok {
		t.Fatal("suppression without until must never match")
	}
	if _, ok := matchSuppression(f, []Suppression{{Repo: "fiber", Check: "*", Until: "2026-07-01"}}, now); ok {
		t.Fatal("different repo must not match")
	}
	if _, ok := matchSuppression(f, []Suppression{{Repo: "*", Check: "*", Workflow: "Lint", Until: "2026-07-01"}}, now); ok {
		t.Fatal("different workflow must not match")
	}
}

func TestThresholdOverrides(t *testing.T) {
	cfg := &Config{
		Org:      "gofiber",
		Repos:    []string{"fiber", "schema"},
		Defaults: Thresholds{MaxOpenPRs: 25, MaxOpenIssues: 60, CooldownHours: 72},
		RepoOverrides: map[string]Thresholds{
			"fiber": {MaxOpenIssues: 300},
		},
	}
	th := cfg.thresholds("fiber")
	if th.MaxOpenIssues != 300 {
		t.Fatalf("override not applied: %d", th.MaxOpenIssues)
	}
	if th.MaxOpenPRs != 25 || th.CooldownHours != 72 {
		t.Fatalf("zero override fields must inherit defaults: %+v", th)
	}
	if got := cfg.thresholds("schema"); got != cfg.Defaults {
		t.Fatalf("repo without overrides must get defaults: %+v", got)
	}
}

func TestExcludeRepos(t *testing.T) {
	repos := []string{"boilerplate", "fiber", "storage"}
	got := excludeRepos(repos, []string{"boilerplate"})
	if len(got) != 2 || got[0] != "fiber" || got[1] != "storage" {
		t.Fatalf("got %v", got)
	}
	if got := excludeRepos(repos, nil); len(got) != 3 {
		t.Fatalf("nil exclude must keep all repos, got %v", got)
	}
}

func TestFilterAlertedCooldown(t *testing.T) {
	s := &State{Alerted: map[string]time.Time{}}
	now := time.Date(2026, 6, 11, 12, 0, 0, 0, time.UTC)
	f := []Finding{{Repo: "storage", Key: "storage/cross-pr/7"}}
	const72 := func(string) time.Duration { return 72 * time.Hour }

	if got := s.filterAlerted(f, const72, now); len(got) != 1 {
		t.Fatal("first occurrence must pass")
	}
	if got := s.filterAlerted(f, const72, now.Add(time.Hour)); len(got) != 0 {
		t.Fatal("repeat within cooldown must be dropped")
	}
	if got := s.filterAlerted(f, const72, now.Add(73*time.Hour)); len(got) != 1 {
		t.Fatal("after cooldown it may fire again")
	}
}

func TestFilterAlertedPerRepoCooldown(t *testing.T) {
	s := &State{Alerted: map[string]time.Time{}}
	now := time.Date(2026, 6, 11, 12, 0, 0, 0, time.UTC)
	// storage is given a short 1h window, everything else the 72h default.
	cooldownFor := func(repo string) time.Duration {
		if repo == "storage" {
			return time.Hour
		}
		return 72 * time.Hour
	}
	f := []Finding{{Repo: "storage", Key: "storage/x"}}

	if got := s.filterAlerted(f, cooldownFor, now); len(got) != 1 {
		t.Fatal("first occurrence must pass")
	}
	if got := s.filterAlerted(f, cooldownFor, now.Add(2*time.Hour)); len(got) != 1 {
		t.Fatal("storage's per-repo 1h cooldown must let it fire again after 2h")
	}
}
