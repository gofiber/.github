package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

var testNow = time.Date(2026, 6, 11, 12, 0, 0, 0, time.UTC)

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
		CreatedAt:  testNow,
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
		// The repeat is held off by the cooldown in the state file, not by the
		// shape of the run list: requiring the run before the newest one to be
		// green lost the transition whenever a batch landed between two scans.
		{"a streak of failures still reports its transition", []workflowRun{
			run(3, 1, "main", "ccc", "push", "timed_out"),
			run(2, 1, "main", "bbb", "push", "failure"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 1},
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
		{"GitHub's own managed jobs are not the repo's CI", []workflowRun{
			run(2, 1, "main", "bbb", "dynamic", "failure"),
			run(1, 1, "main", "aaa", "dynamic", "success"),
		}, 0},
		{"a run batch between two scans must not hide the transition", []workflowRun{
			run(4, 1, "main", "ddd", "push", "failure"),
			run(3, 1, "main", "ccc", "push", "failure"),
			run(2, 1, "main", "bbb", "push", "failure"),
			run(1, 1, "main", "aaa", "push", "success"),
		}, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := detectBranchFailures("fiber", tc.runs, testNow); len(got) != tc.want {
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
			got := detectBranchFailures("template", tc.runs, testNow)
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
	}, testNow)
	if len(got) != 1 || got[0].Check != checkStartupFailure {
		t.Fatalf("got %+v, want one startup-failure finding", got)
	}
}

// A workflow that was deleted keeps its last red run as the latest one; utils
// reported a flip from 2026-05-25 for months because nothing bounded its age.
func TestDetectBranchFailuresIgnoresStaleFlip(t *testing.T) {
	red := run(2, 1, "master", "bbb", "push", "failure")
	red.CreatedAt = testNow.Add(-maxFailureAge - time.Hour)
	green := run(1, 1, "master", "aaa", "push", "success")
	green.CreatedAt = red.CreatedAt.Add(-time.Hour)
	if got := detectBranchFailures("utils", []workflowRun{red, green}, testNow); len(got) != 0 {
		t.Fatalf("flip older than %s must stay silent, got %+v", maxFailureAge, got)
	}
	red.CreatedAt = testNow.Add(-time.Hour)
	if got := detectBranchFailures("utils", []workflowRun{red, green}, testNow); len(got) != 1 {
		t.Fatalf("fresh flip must still fire, got %d findings", len(got))
	}

	// A workflow that has been red for weeks keeps producing fresh runs; the
	// streak dates the incident, not the newest attempt.
	oldBreak := run(2, 1, "master", "bbb", "push", "failure")
	oldBreak.CreatedAt = testNow.Add(-maxFailureAge - time.Hour)
	oldGreen := run(1, 1, "master", "aaa", "push", "success")
	oldGreen.CreatedAt = oldBreak.CreatedAt.Add(-time.Hour)
	freshRetry := run(3, 1, "master", "ccc", "push", "failure")
	if got := detectBranchFailures("utils", []workflowRun{freshRetry, oldBreak, oldGreen}, testNow); len(got) != 0 {
		t.Fatalf("an old streak stays silent however fresh its last attempt is, got %+v", got)
	}
}

// The cooldown only works if a repeated flip of the same workflow keeps its
// key; keying by run id gave every flip a fresh one (storage Benchmark, 3x/8h).
func TestDetectBranchFailuresKeyIsWorkflowScoped(t *testing.T) {
	first := detectBranchFailures("storage", []workflowRun{
		run(2, 7, "main", "bbb", "push", "failure"),
		run(1, 7, "main", "aaa", "push", "success"),
	}, testNow)
	second := detectBranchFailures("storage", []workflowRun{
		run(4, 7, "main", "ddd", "push", "failure"),
		run(3, 7, "main", "ccc", "push", "success"),
	}, testNow)
	// Same workflow, other classification: storage/Benchmark alternates between
	// push and schedule runs, which must not buy it a second key either.
	third := detectBranchFailures("storage", []workflowRun{
		run(6, 7, "main", "eee", "schedule", "failure"),
		run(5, 7, "main", "eee", "schedule", "success"),
	}, testNow)
	if len(first) != 1 || len(second) != 1 || len(third) != 1 {
		t.Fatalf("all three flips must be detected: %d/%d/%d", len(first), len(second), len(third))
	}
	if third[0].Check != checkScheduledFailure {
		t.Fatalf("classification must stay intact, got %q", third[0].Check)
	}
	if first[0].Key != second[0].Key || first[0].Key != third[0].Key {
		t.Fatalf("keys must match across flips: %q / %q / %q", first[0].Key, second[0].Key, third[0].Key)
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

// scanRepo has to hand listRuns a cutoff in the past. A sign error there costs
// nothing visible, it just silently drops back to a single page of runs.
func TestScanRepoPagesTheBranchWindow(t *testing.T) {
	var branchPages int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch q := r.URL.Query(); {
		case !strings.HasSuffix(r.URL.Path, "/actions/runs"):
			io.WriteString(w, `{"default_branch":"main"}`)
		case q.Get("branch") != "":
			branchPages++
			io.WriteString(w, runsPage(100, time.Hour)) // full page, inside the window
		default:
			io.WriteString(w, `{"workflow_runs":[]}`) // the cross-PR query
		}
	}))
	defer srv.Close()

	gh := newGitHub("")
	gh.baseURL = srv.URL
	if _, err := scanRepo(gh, "gofiber", "storage", Thresholds{CrossPRMinPRs: 3, CrossPRWindowHours: 24}, testNow); err != nil {
		t.Fatal(err)
	}
	if branchPages != maxRunPages {
		t.Fatalf("branch runs fetched on %d page(s), want %d: the window cutoff is not in the past", branchPages, maxRunPages)
	}
}

// Dependabot PRs that sit open for weeks mean auto-merge never got them
// through; multi-labeler had five of them, the oldest from November 2025.
func TestDigestRepoStuckBotPRs(t *testing.T) {
	var body string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			b, _ := io.ReadAll(r.Body)
			body = string(b)
			// Aliases follow the sorted keys: lastDay, openIssues, openPRs,
			// stale, stuckBots, twoWeeks, unanswered.
			io.WriteString(w, `{"data":{"a0":{"issueCount":0},"a1":{"issueCount":0},"a2":{"issueCount":0},"a3":{"issueCount":0},"a4":{"issueCount":3},"a5":{"issueCount":0},"a6":{"issueCount":0}}}`)
			return
		}
		io.WriteString(w, `{"workflows":[]}`)
	}))
	defer srv.Close()

	gh := newGitHub("")
	gh.baseURL = srv.URL
	th := Thresholds{StalePRDays: 14, MaxStuckBotPRs: 2, UnansweredIssueDays: 14, IssueSpikeMinCount: 5, IssueSpikeFactor: 3}
	got, err := digestRepo(gh, "gofiber", "multi-labeler", th, testNow)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Check != checkStuckBotPRs {
		t.Fatalf("want one stuck-bot-prs finding, got %+v", got)
	}
	// Every PR query is checked on its own, and against literals rather than
	// the constants: asserting with those would swap along with them and stay
	// green while the check measures the opposite.
	const (
		botFilter   = " author:app/dependabot author:app/github-actions"
		humanFilter = " -author:app/dependabot -author:app/github-actions"
	)
	var sent struct {
		Variables map[string]string `json:"variables"`
	}
	if err := json.Unmarshal([]byte(body), &sent); err != nil {
		t.Fatalf("request body is not JSON: %v", err)
	}
	bots, humans := 0, 0
	for _, q := range sent.Variables {
		switch {
		case strings.Contains(q, botFilter):
			bots++
		case !strings.Contains(q, "is:pr"):
		case strings.Contains(q, humanFilter):
			humans++
		default:
			t.Fatalf("PR query without an author filter: %q", q)
		}
	}
	if bots != 1 || humans != 2 {
		t.Fatalf("want 1 bot query and 2 human PR queries, got %d/%d: %v", bots, humans, sent.Variables)
	}
	if !strings.Contains(sent.Variables["q4"], "draft:false") {
		t.Fatalf("the stuck-bot query must skip drafts: %q", sent.Variables["q4"])
	}

	th.MaxStuckBotPRs = 3
	if got, _ := digestRepo(gh, "gofiber", "multi-labeler", th, testNow); len(got) != 0 {
		t.Fatalf("at the threshold it must stay silent, got %+v", got)
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
			"fiber": {MaxOpenIssues: 300, MaxStuckBotPRs: 5},
		},
	}
	th := cfg.thresholds("fiber")
	if th.MaxOpenIssues != 300 || th.MaxStuckBotPRs != 5 {
		t.Fatalf("override not applied: %+v", th)
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
