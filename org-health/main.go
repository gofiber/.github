// Command org-health detects systemic anomalies across the gofiber org and
// reports them to a Discord channel via webhook.
//
// Two modes:
//   - scan: frequent and cheap. Detects default-branch workflows that flipped
//     from green to red and workflows failing across several PRs at once.
//   - digest: daily. Detects backlog anomalies (PR/issue counts, stale PRs,
//     unanswered issues, issue spikes) and workflows GitHub disabled.
//
// Design rule: a single red build is never reported. Contributors breaking
// lint or tests in their own PR is the CI doing its job. Only patterns that
// point at broken infrastructure produce findings: the same workflow failing
// across unrelated PRs, runs turning red without any code change, or a
// default branch that was merged green and is red now. A posted finding that a
// later run proves fixed gets a resolved post.
package main

import (
	"flag"
	"fmt"
	"log"
	"maps"
	"os"
	"strings"
	"time"
)

func main() {
	var (
		mode       = flag.String("mode", envOr("MODE", "scan"), `"scan" or "digest"`)
		configPath = flag.String("config", envOr("CONFIG_FILE", "config.json"), "config file")
		knownPath  = flag.String("known-issues", envOr("KNOWN_ISSUES_FILE", "known-issues.json"), "suppression list")
		statePath  = flag.String("state", envOr("STATE_FILE", "state.json"), "state file")
		dryRun     = flag.Bool("dry-run", os.Getenv("DRY_RUN") == "true", "print findings instead of posting")
	)
	flag.Parse()

	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatal(err)
	}
	sups, err := loadSuppressions(*knownPath)
	if err != nil {
		log.Fatal(err)
	}
	webhook := os.Getenv("DISCORD_WEBHOOK_URL")
	if webhook == "" && !*dryRun {
		log.Fatal("DISCORD_WEBHOOK_URL is not set (use --dry-run to run without posting)")
	}

	gh := newGitHub(os.Getenv("GITHUB_TOKEN"))
	state := loadState(*statePath)
	now := time.Now().UTC()

	repos := cfg.Repos
	if len(repos) == 0 {
		repos, err = gh.listOrgRepos(cfg.Org)
		if err != nil {
			log.Fatalf("discover repos: %v", err)
		}
		repos = excludeRepos(repos, cfg.ExcludeRepos)
		log.Printf("discovered %d public non-archived repos in %s", len(repos), cfg.Org)
	}

	var findings []Finding
	healthy := map[string]string{} // finding key -> URL of the proof, see resolved
	var failed []string
	for _, repo := range repos {
		th := cfg.thresholds(repo)
		var fs []Finding
		var hs map[string]string
		var err error
		switch *mode {
		case "scan":
			fs, hs, err = scanRepo(gh, cfg.Org, repo, th, now)
		case "digest":
			fs, hs, err = digestRepo(gh, cfg.Org, repo, th, now)
		default:
			log.Fatalf("unknown mode %q", *mode)
		}
		if err != nil {
			log.Printf("%s: %v", repo, err)
			failed = append(failed, repo)
			continue
		}
		findings = append(findings, fs...)
		maps.Copy(healthy, hs)
	}

	findings = applySuppressions(findings, sups, now, log.Printf)
	findings = state.filterAlerted(findings, func(repo string) time.Duration {
		return time.Duration(cfg.thresholds(repo).CooldownHours) * time.Hour
	}, now)
	resolved := state.resolved(healthy)

	switch {
	case len(findings) == 0:
		log.Printf("%s: no findings", *mode)
	case *dryRun:
		log.Printf("%s: %d findings (dry run, not posting)", *mode, len(findings))
		for _, f := range findings {
			fmt.Printf("[%s] %s\n    %s\n    %s\n", f.Check, f.Title, f.Detail, f.URL)
		}
	default:
		if err := postFindings(webhook, *mode, cfg.Org, findings); err != nil {
			log.Fatalf("post to Discord: %v", err)
		}
		state.markOpen(findings)
		log.Printf("%s: posted %d findings", *mode, len(findings))
	}

	// A failed resolved post must not fail the run before the state is saved:
	// the findings above are posted already and would be posted twice.
	var resolvedErr error
	switch {
	case len(resolved) == 0:
	case *dryRun:
		log.Printf("%s: %d resolved (dry run, not posting)", *mode, len(resolved))
		for _, f := range resolved {
			fmt.Printf("[resolved %s] %s\n    %s\n", f.Check, f.Title, f.URL)
		}
	default:
		if resolvedErr = postResolved(webhook, cfg.Org, resolved); resolvedErr == nil {
			state.close(resolved)
			log.Printf("%s: posted %d resolved", *mode, len(resolved))
		}
	}

	// Dry runs must not consume the cooldown of findings they did not post.
	if !*dryRun {
		state.prune(now)
		if err := saveState(*statePath, state); err != nil {
			log.Printf("save state: %v", err)
		}
	}
	if resolvedErr != nil {
		log.Fatalf("post resolved to Discord: %v", resolvedErr)
	}
	if len(failed) > 0 {
		log.Fatalf("repos with errors: %s", strings.Join(failed, ", "))
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
