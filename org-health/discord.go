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
	colorGreen  = 0x2ECC71

	maxEmbeds         = 10   // Discord limit per message
	maxDescriptionLen = 4000 // Discord allows 4096, keep headroom
)

func postFindings(webhook, mode, org string, findings []Finding) error {
	var embeds []discordEmbed
	if mode == "digest" {
		lines := make([]string, len(findings))
		for i, f := range findings {
			lines[i] = fmt.Sprintf("- **%s**: %s ([details](%s))\n", f.Repo, f.Detail, f.URL)
		}
		embeds = []discordEmbed{{
			Title:       fmt.Sprintf("%s org health digest: %d findings", org, len(findings)),
			Description: bulletList(lines),
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
	return postEmbeds(webhook, embeds)
}

// postResolved strikes through the alert titles of fixed findings in one message.
func postResolved(webhook, org string, resolved []Finding) error {
	lines := make([]string, len(resolved))
	for i, f := range resolved {
		lines[i] = fmt.Sprintf("- ~~%s~~ ([details](%s))\n", f.Title, f.URL)
	}
	return postEmbeds(webhook, []discordEmbed{{
		Title:       fmt.Sprintf("%s org health: %d resolved", org, len(resolved)),
		Description: bulletList(lines),
		Color:       colorGreen,
	}})
}

func bulletList(lines []string) string {
	var b strings.Builder
	for _, line := range lines {
		if b.Len()+len(line) > maxDescriptionLen {
			b.WriteString("- ... (truncated)\n")
			break
		}
		b.WriteString(line)
	}
	return b.String()
}

func postEmbeds(webhook string, embeds []discordEmbed) error {
	payload, err := json.Marshal(map[string]any{"embeds": embeds})
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Post(webhook, "application/json", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("discord webhook: %s: %s", resp.Status, b)
	}
	return nil
}
