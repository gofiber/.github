package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

// resetNow makes rateLimitWait floor its wait to the 1s minimum, keeping the
// retry tests fast while still exercising the full backoff path.
func resetNow() string { return strconv.FormatInt(time.Now().Unix(), 10) }

func TestDoRequestRetriesOnRESTRateLimit(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.Header().Set("X-RateLimit-Reset", resetNow())
			w.WriteHeader(http.StatusForbidden)
			io.WriteString(w, `{"message":"rate limit exceeded"}`)
			return
		}
		io.WriteString(w, `{"default_branch":"main"}`)
	}))
	defer srv.Close()

	gh := newGitHub("")
	gh.baseURL = srv.URL
	branch, err := gh.defaultBranch("o", "r")
	if err != nil {
		t.Fatal(err)
	}
	if branch != "main" {
		t.Fatalf("branch = %q, want main", branch)
	}
	if calls != 2 {
		t.Fatalf("calls = %d, want 2 (one retry then success)", calls)
	}
}

func TestGraphqlRetriesOnRateLimited(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			// GraphQL signals throttling as HTTP 200 + a RATE_LIMITED error.
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.Header().Set("X-RateLimit-Reset", resetNow())
			io.WriteString(w, `{"data":null,"errors":[{"type":"RATE_LIMITED","message":"rate limited"}]}`)
			return
		}
		io.WriteString(w, `{"data":{"a0":{"issueCount":7}}}`)
	}))
	defer srv.Close()

	gh := newGitHub("")
	gh.baseURL = srv.URL
	counts, err := gh.searchCounts(map[string]string{"openPRs": "repo:x is:pr is:open"})
	if err != nil {
		t.Fatal(err)
	}
	if counts["openPRs"] != 7 {
		t.Fatalf("openPRs = %d, want 7", counts["openPRs"])
	}
	if calls != 2 {
		t.Fatalf("calls = %d, want 2 (one retry then success)", calls)
	}
}

func TestGraphqlSurfacesNonRateLimitError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"data":null,"errors":[{"type":"FORBIDDEN","message":"resource not accessible"}]}`)
	}))
	defer srv.Close()

	gh := newGitHub("")
	gh.baseURL = srv.URL
	if _, err := gh.searchCounts(map[string]string{"openPRs": "q"}); err == nil ||
		!strings.Contains(err.Error(), "resource not accessible") {
		t.Fatalf("err = %v, want it to surface the GraphQL error message", err)
	}
}

func TestSearchCountsRemapsAliasesBySortedKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Sorted keys are lastDay(a0) < openPRs(a1) < twoWeeks(a2).
		io.WriteString(w, `{"data":{"a0":{"issueCount":1},"a1":{"issueCount":2},"a2":{"issueCount":3}}}`)
	}))
	defer srv.Close()

	gh := newGitHub("")
	gh.baseURL = srv.URL
	counts, err := gh.searchCounts(map[string]string{"openPRs": "p", "lastDay": "l", "twoWeeks": "t"})
	if err != nil {
		t.Fatal(err)
	}
	if counts["lastDay"] != 1 || counts["openPRs"] != 2 || counts["twoWeeks"] != 3 {
		t.Fatalf("counts = %v, want lastDay=1 openPRs=2 twoWeeks=3", counts)
	}
}

func TestGraphqlEmptyResponseErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"data":null}`)
	}))
	defer srv.Close()

	gh := newGitHub("")
	gh.baseURL = srv.URL
	if _, err := gh.searchCounts(map[string]string{"x": "q"}); err == nil {
		t.Fatal("want error on empty data with no errors, got nil")
	}
}

func TestBuildSearchCountsQuery(t *testing.T) {
	query, vars, keys := buildSearchCountsQuery(map[string]string{
		"openPRs":  "repo:gofiber/fiber is:pr is:open",
		"lastDay":  "repo:gofiber/fiber is:issue created:>=2026-06-13",
		"twoWeeks": "repo:gofiber/fiber is:issue created:>=2026-05-31",
	})

	// Keys are sorted so the query is deterministic.
	want := []string{"lastDay", "openPRs", "twoWeeks"}
	if strings.Join(keys, ",") != strings.Join(want, ",") {
		t.Fatalf("keys = %v, want %v", keys, want)
	}
	if len(vars) != 3 {
		t.Fatalf("vars = %v, want 3 entries", vars)
	}
	// Each key maps to its alias index via the sorted order.
	for i, k := range keys {
		alias := "a" + strconv.Itoa(i)
		varRef := "$q" + strconv.Itoa(i)
		if !strings.Contains(query, alias+": search(query: "+varRef+", type: ISSUE) { issueCount }") {
			t.Fatalf("query missing alias %s for key %s:\n%s", alias, k, query)
		}
		if vars["q"+strconv.Itoa(i)] == "" {
			t.Fatalf("missing variable q%d", i)
		}
	}
	if !strings.HasPrefix(query, "query($q0: String!, $q1: String!, $q2: String!) {") {
		t.Fatalf("unexpected query header:\n%s", query)
	}
}

func TestBuildSearchCountsQueryEmpty(t *testing.T) {
	query, vars, keys := buildSearchCountsQuery(map[string]string{})
	if len(keys) != 0 || len(vars) != 0 {
		t.Fatalf("expected empty, got keys=%v vars=%v", keys, vars)
	}
	if !strings.HasPrefix(query, "query() {") {
		t.Fatalf("unexpected empty query: %q", query)
	}
}

func TestRateLimitWait(t *testing.T) {
	t.Run("retry-after wins", func(t *testing.T) {
		h := http.Header{}
		h.Set("Retry-After", "30")
		h.Set("X-RateLimit-Remaining", "0")
		d, ok := rateLimitWait(h)
		if !ok || d < 30*time.Second {
			t.Fatalf("got %v, %v; want >=30s, true", d, ok)
		}
	})

	t.Run("primary reset in the future", func(t *testing.T) {
		h := http.Header{}
		h.Set("X-RateLimit-Remaining", "0")
		h.Set("X-RateLimit-Reset", strconv.FormatInt(time.Now().Add(20*time.Second).Unix(), 10))
		d, ok := rateLimitWait(h)
		if !ok || d <= 0 || d > 30*time.Second {
			t.Fatalf("got %v, %v; want (0,30s], true", d, ok)
		}
	})

	t.Run("primary reset already passed", func(t *testing.T) {
		h := http.Header{}
		h.Set("X-RateLimit-Remaining", "0")
		h.Set("X-RateLimit-Reset", strconv.FormatInt(time.Now().Add(-time.Minute).Unix(), 10))
		d, ok := rateLimitWait(h)
		if !ok || d < time.Second {
			t.Fatalf("got %v, %v; want >=1s, true", d, ok)
		}
	})

	t.Run("not rate limited", func(t *testing.T) {
		h := http.Header{}
		h.Set("X-RateLimit-Remaining", "57")
		d, ok := rateLimitWait(h)
		if ok || d != 0 {
			t.Fatalf("got %v, %v; want 0, false", d, ok)
		}
	})
}
