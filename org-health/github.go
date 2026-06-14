package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

const apiBase = "https://api.github.com"

// Rate-limit retry budget. GitHub's REST search limit resets on a per-minute
// window, so a single wait of up to ~a minute clears it. maxTotalBackoff caps
// the time the whole run may spend sleeping across all calls so a sustained
// throttle can never blow the 15-minute job timeout.
const (
	maxRateLimitRetries = 4
	maxRateLimitWait    = 90 * time.Second
	maxTotalBackoff     = 8 * time.Minute
)

type gitHub struct {
	token        string
	baseURL      string
	httpc        *http.Client
	backoffSpent time.Duration // total time slept on rate-limit backoff this run
}

func newGitHub(token string) *gitHub {
	return &gitHub{token: token, baseURL: apiBase, httpc: &http.Client{Timeout: 30 * time.Second}}
}

func (g *gitHub) get(path string, query url.Values, out any) error {
	u := g.baseURL + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	data, _, err := g.doRequest(http.MethodGet, u, path, nil)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, out)
}

// doRequest performs an HTTP request and returns the response body and headers,
// retrying on GitHub's REST rate-limit responses (a non-200 carrying Retry-After
// or X-RateLimit-Remaining: 0 + Reset). label is the short path used in logs and
// errors. body, if non-nil, is the JSON payload, re-sent fresh on each retry.
// The body is returned raw so callers can inspect it; GraphQL in particular
// reports throttling as a 200 with an errors array, handled in graphql().
func (g *gitHub) doRequest(method, fullURL, label string, body []byte) ([]byte, http.Header, error) {
	for attempt := 0; ; attempt++ {
		var r io.Reader
		if body != nil {
			r = bytes.NewReader(body)
		}
		req, err := http.NewRequest(method, fullURL, r)
		if err != nil {
			return nil, nil, err
		}
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if g.token != "" {
			req.Header.Set("Authorization", "Bearer "+g.token)
		}
		resp, err := g.httpc.Do(req)
		if err != nil {
			return nil, nil, err
		}
		data, _ := io.ReadAll(resp.Body) // fully drained so the connection is reused
		resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			return data, resp.Header, nil
		}

		// A real 4xx with no rate-limit signal is surfaced immediately.
		wait, limited := rateLimitWait(resp.Header)
		if limited && g.backoff(wait, attempt, method, label) {
			continue
		}
		return nil, resp.Header, fmt.Errorf("%s %s: %s: %.512s", method, label, resp.Status, data)
	}
}

// backoff sleeps for wait and reports whether the caller should retry. It
// returns false once the per-call retry count, the per-wait cap, or the
// run-wide backoff budget is exhausted, bounding total wall-clock.
func (g *gitHub) backoff(wait time.Duration, attempt int, method, label string) bool {
	if attempt >= maxRateLimitRetries || wait > maxRateLimitWait {
		return false
	}
	if g.backoffSpent+wait > maxTotalBackoff {
		return false
	}
	g.backoffSpent += wait
	log.Printf("rate limited on %s %s, waiting %s before retry (%d/%d, %s of %s budget)",
		method, label, wait.Round(time.Second), attempt+1, maxRateLimitRetries,
		g.backoffSpent.Round(time.Second), maxTotalBackoff)
	time.Sleep(wait)
	return true
}

// rateLimitWait inspects a response and reports how long to wait before retrying,
// plus whether the response actually carries a rate-limit signal (so genuine
// errors are surfaced immediately instead of being retried). It works for both
// REST non-200 responses and GraphQL 200 throttle responses, which carry the
// same X-RateLimit-* headers.
func rateLimitWait(h http.Header) (time.Duration, bool) {
	// Secondary limit / abuse detection: an explicit number of seconds to wait.
	if ra := strings.TrimSpace(h.Get("Retry-After")); ra != "" {
		if secs, err := strconv.Atoi(ra); err == nil && secs >= 0 {
			return time.Duration(secs)*time.Second + time.Second, true
		}
	}
	// Primary limit: the budget is exhausted and Reset carries the unix time the
	// window reopens.
	if h.Get("X-RateLimit-Remaining") == "0" {
		if reset := strings.TrimSpace(h.Get("X-RateLimit-Reset")); reset != "" {
			if ts, err := strconv.ParseInt(reset, 10, 64); err == nil {
				wait := time.Until(time.Unix(ts, 0)) + time.Second
				if wait < time.Second {
					wait = time.Second // clock skew or window already reopened
				}
				return wait, true
			}
		}
	}
	return 0, false
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

type graphQLError struct {
	Type    string `json:"type"`
	Message string `json:"message"`
}

// graphql runs a single GraphQL query and decodes its data into out. GraphQL
// reports request errors (including throttling) as HTTP 200 with an errors
// array rather than a 4xx, so they are handled here: a RATE_LIMITED error is
// retried with the same header-driven backoff as the REST path, anything else
// is surfaced as an error.
func (g *gitHub) graphql(query string, vars map[string]any, out any) error {
	body, err := json.Marshal(map[string]any{"query": query, "variables": vars})
	if err != nil {
		return err
	}
	for attempt := 0; ; attempt++ {
		data, header, err := g.doRequest(http.MethodPost, g.baseURL+"/graphql", "/graphql", body)
		if err != nil {
			return err
		}
		var env struct {
			Data   json.RawMessage `json:"data"`
			Errors []graphQLError  `json:"errors"`
		}
		if err := json.Unmarshal(data, &env); err != nil {
			return err
		}
		if len(env.Errors) > 0 {
			if isRateLimited(env.Errors) {
				if wait, limited := rateLimitWait(header); limited && g.backoff(wait, attempt, http.MethodPost, "/graphql") {
					continue
				}
			}
			return fmt.Errorf("graphql: %s", env.Errors[0].Message)
		}
		if len(env.Data) == 0 || string(env.Data) == "null" {
			return fmt.Errorf("graphql: empty response with no errors")
		}
		return json.Unmarshal(env.Data, out)
	}
}

func isRateLimited(errs []graphQLError) bool {
	for _, e := range errs {
		if e.Type == "RATE_LIMITED" {
			return true
		}
	}
	return false
}

// searchCounts returns the match count of several issue/PR searches in a single
// GraphQL request. Each key maps to a search query and comes back as that key's
// count. The REST /search/issues endpoint is limited to 30 requests per minute,
// which the daily digest (6 searches per repo, ~14 repos) brushes up against;
// GraphQL search draws from the separate, far larger points budget (each count
// costs ~1 point against a 5000/hour budget) and returns every count in one
// round trip, so the digest's ~14 daily requests stay far under the limit.
func (g *gitHub) searchCounts(queries map[string]string) (map[string]int, error) {
	if len(queries) == 0 {
		return map[string]int{}, nil
	}
	query, vars, keys := buildSearchCountsQuery(queries)
	var data map[string]struct {
		IssueCount int `json:"issueCount"`
	}
	if err := g.graphql(query, vars, &data); err != nil {
		return nil, err
	}
	counts := make(map[string]int, len(keys))
	for i, k := range keys {
		counts[k] = data[fmt.Sprintf("a%d", i)].IssueCount
	}
	return counts, nil
}

// buildSearchCountsQuery assembles one aliased GraphQL query that asks for the
// issueCount of every search. Keys are sorted so the query and variables are
// deterministic; the returned keys slice maps alias index back to the caller's
// key. type: ISSUE covers both issues and PRs (is:pr / is:issue narrows them).
func buildSearchCountsQuery(queries map[string]string) (string, map[string]any, []string) {
	keys := make([]string, 0, len(queries))
	for k := range queries {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	vars := make(map[string]any, len(keys))
	var b strings.Builder
	b.WriteString("query(")
	for i := range keys {
		if i > 0 {
			b.WriteString(", ")
		}
		fmt.Fprintf(&b, "$q%d: String!", i)
	}
	b.WriteString(") {")
	for i := range keys {
		fmt.Fprintf(&b, " a%d: search(query: $q%d, type: ISSUE) { issueCount }", i, i)
		vars[fmt.Sprintf("q%d", i)] = queries[keys[i]]
	}
	b.WriteString(" }")
	return b.String(), vars, keys
}
