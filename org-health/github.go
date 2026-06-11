package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"time"
)

const apiBase = "https://api.github.com"

type gitHub struct {
	token      string
	httpc      *http.Client
	lastSearch time.Time
}

func newGitHub(token string) *gitHub {
	return &gitHub{token: token, httpc: &http.Client{Timeout: 30 * time.Second}}
}

func (g *gitHub) get(path string, query url.Values, out any) error {
	u := apiBase + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if g.token != "" {
		req.Header.Set("Authorization", "Bearer "+g.token)
	}
	resp, err := g.httpc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("GET %s: %s: %s", path, resp.Status, b)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

type workflowRun struct {
	ID         int64     `json:"id"`
	Name       string    `json:"name"`
	WorkflowID int64     `json:"workflow_id"`
	HeadBranch string    `json:"head_branch"`
	HeadSHA    string    `json:"head_sha"`
	Event      string    `json:"event"`
	Status     string    `json:"status"`
	Conclusion string    `json:"conclusion"`
	CreatedAt  time.Time `json:"created_at"`
	HTMLURL    string    `json:"html_url"`
	Actor      struct {
		Login string `json:"login"`
	} `json:"actor"`
}

type workflowInfo struct {
	ID      int64  `json:"id"`
	Name    string `json:"name"`
	Path    string `json:"path"`
	State   string `json:"state"`
	HTMLURL string `json:"html_url"`
}

// listOrgRepos returns the names of all public, non-archived repos of the org.
func (g *gitHub) listOrgRepos(org string) ([]string, error) {
	var names []string
	for page := 1; ; page++ {
		var repos []struct {
			Name     string `json:"name"`
			Archived bool   `json:"archived"`
		}
		q := url.Values{"type": {"public"}, "per_page": {"100"}, "page": {fmt.Sprint(page)}}
		if err := g.get("/orgs/"+org+"/repos", q, &repos); err != nil {
			return nil, err
		}
		for _, r := range repos {
			if !r.Archived {
				names = append(names, r.Name)
			}
		}
		if len(repos) < 100 {
			break
		}
	}
	sort.Strings(names)
	return names, nil
}

func (g *gitHub) defaultBranch(org, repo string) (string, error) {
	var info struct {
		DefaultBranch string `json:"default_branch"`
	}
	if err := g.get("/repos/"+org+"/"+repo, nil, &info); err != nil {
		return "", err
	}
	return info.DefaultBranch, nil
}

// listRuns returns up to one page (100) of runs, newest first. The detection
// windows are short enough that a single page always covers them.
func (g *gitHub) listRuns(org, repo string, query url.Values) ([]workflowRun, error) {
	query.Set("per_page", "100")
	var out struct {
		WorkflowRuns []workflowRun `json:"workflow_runs"`
	}
	if err := g.get("/repos/"+org+"/"+repo+"/actions/runs", query, &out); err != nil {
		return nil, err
	}
	return out.WorkflowRuns, nil
}

func (g *gitHub) listWorkflows(org, repo string) ([]workflowInfo, error) {
	var out struct {
		Workflows []workflowInfo `json:"workflows"`
	}
	q := url.Values{"per_page": {"100"}}
	if err := g.get("/repos/"+org+"/"+repo+"/actions/workflows", q, &out); err != nil {
		return nil, err
	}
	return out.Workflows, nil
}

// searchCount returns the total_count of an issue/PR search. The search API
// allows 30 requests per minute, so calls are paced to stay under that.
func (g *gitHub) searchCount(query string) (int, error) {
	if wait := 2100*time.Millisecond - time.Since(g.lastSearch); wait > 0 {
		time.Sleep(wait)
	}
	g.lastSearch = time.Now()
	q := url.Values{"q": {query}, "per_page": {"1"}, "advanced_search": {"true"}}
	var out struct {
		TotalCount int `json:"total_count"`
	}
	if err := g.get("/search/issues", q, &out); err != nil {
		return 0, err
	}
	return out.TotalCount, nil
}
