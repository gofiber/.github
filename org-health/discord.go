package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type discordEmbed struct {
	Title       string `json:"title,omitempty"`
	Description string `json:"description,omitempty"`
	URL         string `json:"url,omitempty"`
	Color       int    `json:"color,omitempty"`
}

const (
	colorRed    = 0xE74C3C
	colorOrange = 0xE67E22

	maxEmbeds         = 10   // Discord limit per message
	maxDescriptionLen = 4000 // Discord allows 4096, keep headroom
)

func postFindings(webhook, mode, org string, findings []Finding) error {
	var embeds []discordEmbed
	if mode == "digest" {
		var b strings.Builder
		for _, f := range findings {
			line := fmt.Sprintf("- **%s**: %s ([details](%s))\n", f.Repo, f.Detail, f.URL)
			if b.Len()+len(line) > maxDescriptionLen {
				b.WriteString("- ... (truncated)\n")
				break
			}
			b.WriteString(line)
		}
		embeds = []discordEmbed{{
			Title:       fmt.Sprintf("%s org health digest: %d findings", org, len(findings)),
			Description: b.String(),
			Color:       colorOrange,
		}}
	} else {
		for i, f := range findings {
			if i == maxEmbeds-1 && len(findings) > maxEmbeds {
				embeds = append(embeds, discordEmbed{
					Title: fmt.Sprintf("... and %d more findings", len(findings)-i),
					Color: colorRed,
				})
				break
			}
			embeds = append(embeds, discordEmbed{
				Title:       f.Title,
				Description: f.Detail,
				URL:         f.URL,
				Color:       colorRed,
			})
		}
	}

	payload, err := json.Marshal(map[string]any{"embeds": embeds})
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Post(webhook, "application/json", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("discord webhook: %s: %s", resp.Status, b)
	}
	return nil
}
