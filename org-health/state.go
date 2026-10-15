package main

import (
	"encoding/json"
	"os"
	"time"
)

// State remembers which finding keys were already reported, persisted between
// runs via actions/cache. It is what makes alerts edge-triggered: a finding
// fires once and then stays silent for the cooldown.
type State struct {
	Alerted map[string]time.Time `json:"alerted"`
	// Open holds the posted findings that no run has reported resolved yet.
	Open map[string]Finding `json:"open"`
}

func loadState(path string) *State {
	s := &State{Alerted: map[string]time.Time{}, Open: map[string]Finding{}}
	b, err := os.ReadFile(path)
	if err != nil {
		return s // first run or cache miss, start empty
	}
	if err := json.Unmarshal(b, s); err != nil || s.Alerted == nil {
		s.Alerted = map[string]time.Time{}
	}
	if s.Open == nil {
		s.Open = map[string]Finding{}
	}
	return s
}

func (s *State) markOpen(findings []Finding) {
	for _, f := range findings {
		s.Open[f.Key] = f
	}
}

// resolved returns the open findings that healthy proves fixed, linked to the
// proving run where there is one.
func (s *State) resolved(healthy map[string]string) []Finding {
	var out []Finding
	for key, f := range s.Open {
		evidence, ok := healthy[key]
		if !ok {
			continue
		}
		if evidence != "" {
			f.URL = evidence
		}
		out = append(out, f)
	}
	sortFindings(out)
	return out
}

// close forgets resolved findings, cooldown included: the channel last said
// fixed, so a relapse has to alert right away.
func (s *State) close(findings []Finding) {
	for _, f := range findings {
		delete(s.Open, f.Key)
		delete(s.Alerted, f.Key)
	}
}

// filterAlerted drops findings whose key already fired within the cooldown
// and records the ones that pass. cooldownFor yields the cooldown for a
// finding's repo, so a noisier repo can be given a longer quiet window than
// the org default without a suppression entry.
func (s *State) filterAlerted(findings []Finding, cooldownFor func(repo string) time.Duration, now time.Time) []Finding {
	var out []Finding
	for _, f := range findings {
		if t, ok := s.Alerted[f.Key]; ok && now.Sub(t) < cooldownFor(f.Repo) {
			continue
		}
		s.Alerted[f.Key] = now
		out = append(out, f)
	}
	return out
}

func (s *State) prune(now time.Time) {
	for k, t := range s.Alerted {
		if now.Sub(t) > 14*24*time.Hour {
			delete(s.Alerted, k)
		}
	}
	// still failing re-alerts every cooldown, so a missing alert means it stopped being tracked
	for k := range s.Open {
		if _, ok := s.Alerted[k]; !ok {
			delete(s.Open, k)
		}
	}
}

func saveState(path string, s *State) error {
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o644)
}
