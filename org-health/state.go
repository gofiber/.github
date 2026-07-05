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
}

func loadState(path string) *State {
	s := &State{Alerted: map[string]time.Time{}}
	b, err := os.ReadFile(path)
	if err != nil {
		return s // first run or cache miss, start empty
	}
	if err := json.Unmarshal(b, s); err != nil || s.Alerted == nil {
		s.Alerted = map[string]time.Time{}
	}
	return s
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
}

func saveState(path string, s *State) error {
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o644)
}
