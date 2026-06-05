#!/usr/bin/env python3
"""Render the gofiber sponsors block into a README.

Reads `SPONSORS_TOKEN`, `ORG`, and `FILE` from the environment, fetches the
org's configured sponsor tiers and current sponsorships via GraphQL, derives
each tier's display title from the first markdown heading in its description
(so changes on github.com/sponsors/<org> propagate automatically), splits
sponsors into monthly vs. one-time, and replaces the content between a
single pair of markers:

  <!-- sponsors --><!-- sponsors -->

The full Supporters layout (section headings + both tables) is emitted by
this script; the README only carries the intro paragraph and the marker
placeholder. Monthly sponsors render with the full tier set and larger
avatars; one-time donors render compactly and collapse every tier below
"Hero" ($100) into a single "Supporter" badge so the section stays short.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from html import escape

ORG = os.environ['ORG']
TOKEN = os.environ['SPONSORS_TOKEN']
FILE = os.environ.get('FILE', 'README.md')

MONTHLY_AVATAR_PX = 60
ONETIME_AVATAR_PX = 40
ONETIME_COLLAPSE_THRESHOLD_CENTS = 10000  # tiers below $100/month collapse into one Supporter bucket

QUERY = """
query($org: String!) {
  organization(login: $org) {
    sponsorsListing {
      tiers(first: 20) {
        nodes {
          monthlyPriceInCents
          description
          isOneTime
        }
      }
    }
    sponsorshipsAsMaintainer(first: 100, activeOnly: false, includePrivate: false) {
      nodes {
        sponsorEntity {
          ... on User { login name url websiteUrl }
          ... on Organization { login name url websiteUrl }
        }
        tier { monthlyPriceInCents isOneTime }
      }
    }
  }
}
"""


def gql(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "gofiber-sync-sponsors/1.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read())
    if "errors" in payload:
        sys.exit(f"GraphQL errors: {json.dumps(payload['errors'], indent=2)}")
    return payload["data"]


def tier_title(description: str | None) -> str | None:
    for line in (description or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return re.sub(r"^#+\s*", "", line).strip()
    return None


def normalize_url(url: str | None, login: str) -> str:
    if not url:
        return f"https://github.com/{login}"
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def render_row(login: str, website: str, badge: str, avatar_px: int) -> str:
    return (
        '<tr>'
        f'<td align="center"><img src="https://github.com/{escape(login, quote=True)}.png" width="{avatar_px}" /></td>'
        f'<td><a href="{escape(website, quote=True)}">@{escape(login, quote=True)}</a></td>'
        f'<td>{badge}</td>'
        '</tr>'
    )


def bucket(tiers: list[dict], cents: int) -> dict | None:
    """Pick the largest tier whose cents <= the sponsor's cents."""
    return next((t for t in tiers if t["cents"] <= cents), None)


def collapse_for_onetime(tier: dict, supporter_title: str) -> str:
    if tier["cents"] < ONETIME_COLLAPSE_THRESHOLD_CENTS:
        return supporter_title
    return tier["title"]


def render_section(heading: str, rows: list[str], empty_message: str) -> str:
    body = "\n".join(rows) if rows else f'<tr><td colspan="3"><em>{empty_message}</em></td></tr>'
    return (
        f"### {heading}\n\n"
        "<table>\n"
        "  <thead>\n"
        "    <tr><th></th><th>User</th><th>Sponsorship</th></tr>\n"
        "  </thead>\n"
        "  <tbody>\n"
        f"{body}\n"
        "  </tbody>\n"
        "</table>"
    )


def main() -> None:
    data = gql(QUERY, {"org": ORG})

    listing = data["organization"].get("sponsorsListing")
    if not listing:
        sys.exit(f"Organization {ORG!r} has no sponsors listing.")

    tiers = sorted(
        (
            {
                "cents": t["monthlyPriceInCents"],
                "title": tier_title(t["description"]) or f"${t['monthlyPriceInCents'] // 100} Sponsor",
            }
            for t in listing["tiers"]["nodes"]
            if not t.get("isOneTime")
        ),
        key=lambda t: -t["cents"],
    )
    if not tiers:
        sys.exit(f"Organization {ORG!r} has no monthly tiers configured.")

    # Pick the badge used for collapsed (small) one-time donations. Prefer the
    # tier whose title contains "Supporter" (gofiber's $10/month tier today),
    # fall back to the lowest tier overall so we still produce a sensible label
    # if the org renames it.
    collapse_candidates = [t for t in tiers if t["cents"] < ONETIME_COLLAPSE_THRESHOLD_CENTS]
    supporter_tier = next(
        (t for t in collapse_candidates if "supporter" in t["title"].lower()),
        collapse_candidates[0] if collapse_candidates else tiers[-1],
    )
    supporter_title = supporter_tier["title"]

    monthly_rows: list[tuple[int, str]] = []
    onetime_rows: list[tuple[int, str]] = []

    for s in data["organization"]["sponsorshipsAsMaintainer"]["nodes"]:
        tier_info = s.get("tier") or {}
        cents = tier_info.get("monthlyPriceInCents") or 0
        is_one_time = tier_info.get("isOneTime") or False
        if cents < tiers[-1]["cents"]:
            continue
        entity = s["sponsorEntity"]
        login = entity["login"]
        website = normalize_url(entity.get("websiteUrl"), login)
        target = bucket(tiers, cents)
        if target is None:
            continue
        if is_one_time:
            badge = collapse_for_onetime(target, supporter_title)
            onetime_rows.append((cents, render_row(login, website, badge, ONETIME_AVATAR_PX)))
        else:
            monthly_rows.append((cents, render_row(login, website, target["title"], MONTHLY_AVATAR_PX)))

    monthly_rows.sort(key=lambda r: -r[0])
    onetime_rows.sort(key=lambda r: -r[0])

    monthly_section = render_section(
        "📅 Monthly Sponsors",
        [row for _, row in monthly_rows],
        f'Be the first monthly sponsor and <a href="https://github.com/sponsors/{ORG}">support {ORG}</a>.',
    )
    onetime_section = render_section(
        "🎁 One-time Sponsors",
        [row for _, row in onetime_rows],
        f'Thank-you donations welcome at <a href="https://github.com/sponsors/{ORG}">github.com/sponsors/{ORG}</a>.',
    )

    block = f"{monthly_section}\n\n{onetime_section}"

    with open(FILE, "r", encoding="utf-8") as fh:
        content = fh.read()

    pattern = re.compile(r"<!-- sponsors -->.*?<!-- sponsors -->", re.DOTALL)
    if not pattern.search(content):
        sys.exit(f"Could not find <!-- sponsors --> markers in {FILE}")
    new_content = pattern.sub(f"<!-- sponsors -->\n{block}\n<!-- sponsors -->", content)

    with open(FILE, "w", encoding="utf-8") as fh:
        fh.write(new_content)

    print(
        f"Wrote {len(monthly_rows)} monthly + {len(onetime_rows)} one-time "
        f"sponsors to {FILE}"
    )


if __name__ == "__main__":
    main()
