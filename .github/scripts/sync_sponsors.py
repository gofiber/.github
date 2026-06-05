#!/usr/bin/env python3
"""Render the gofiber sponsors block into a README.

Reads `SPONSORS_TOKEN`, `ORG`, and `FILE` from the environment, fetches the
org's configured sponsor tiers and current sponsorships via GraphQL, derives
each monthly tier's display title from the first markdown heading in its
description (so changes on github.com/sponsors/<org> propagate
automatically), and replaces the content between a single pair of markers:

  <!-- sponsors --><!-- sponsors -->

The README only carries the intro paragraph and the marker placeholder.
The script emits a compact avatar-wall layout: one tier label per row
followed by inline avatars, sized down as the tier value drops so the
section stays short. Monthly sponsors are filtered to currently-active
sponsorships only. One-time donations are split into two buckets
("$100+" and "Other") to keep them compact while still acknowledging
the bigger one-time gifts.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from html import escape
from typing import Iterable

ORG = os.environ['ORG']
TOKEN = os.environ['SPONSORS_TOKEN']
FILE = os.environ.get('FILE', 'README.md')

# Avatar sizes shrink as we go down the prominence ladder so the section
# stays compact even with many sponsors at the lower tiers.
MONTHLY_TIER_SIZES = {  # cents threshold -> px
    50000: 60,  # >= $500
    25000: 55,  # >= $250
    10000: 50,  # >= $100
    5000:  45,  # >= $50
    1000:  35,  # >= $10
    0:     32,  # below $10 (Friend)
}
ONETIME_HIGH_THRESHOLD_CENTS = 10000  # >= $100 one-time goes in the prominent bucket
ONETIME_HIGH_PX = 50
ONETIME_LOW_PX = 30

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
    active: sponsorshipsAsMaintainer(first: 100, activeOnly: true, includePrivate: false) {
      nodes {
        sponsorEntity {
          ... on User { login name url websiteUrl }
          ... on Organization { login name url websiteUrl }
        }
        tier { monthlyPriceInCents isOneTime }
      }
    }
    all: sponsorshipsAsMaintainer(first: 100, activeOnly: false, includePrivate: false) {
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


def avatar(login: str, website: str, px: int) -> str:
    return (
        f'<a href="{escape(website, quote=True)}" title="@{escape(login, quote=True)}">'
        f'<img src="https://github.com/{escape(login, quote=True)}.png" '
        f'width="{px}" alt="@{escape(login, quote=True)}" />'
        f'</a>'
    )


def avatar_wall(sponsors: Iterable[tuple[str, str]], px: int) -> str:
    return "&nbsp;".join(avatar(login, website, px) for login, website in sponsors)


def size_for(cents: int) -> int:
    for threshold in sorted(MONTHLY_TIER_SIZES, reverse=True):
        if cents >= threshold:
            return MONTHLY_TIER_SIZES[threshold]
    return MONTHLY_TIER_SIZES[0]


def render_tier_line(label: str, sponsors: list[tuple[str, str]], px: int) -> str:
    if not sponsors:
        return ""
    return f"**{label}** &nbsp; {avatar_wall(sponsors, px)}"


def render_monthly(tiered_monthly: list[tuple[dict, list[tuple[str, str]]]]) -> str:
    body_lines = []
    for tier, sponsors in tiered_monthly:
        if not sponsors:
            continue
        body_lines.append(render_tier_line(tier["title"], sponsors, size_for(tier["cents"])))
    if not body_lines:
        body = (
            f"_Be the first monthly sponsor and "
            f"[support {ORG}](https://github.com/sponsors/{ORG})._"
        )
    else:
        body = "\n\n".join(body_lines)
    return f"### 📅 Monthly Sponsors\n\n{body}"


def render_onetime(high: list[tuple[str, str]], low: list[tuple[str, str]]) -> str:
    body_lines = []
    if high:
        body_lines.append(render_tier_line("🚀 $100+", high, ONETIME_HIGH_PX))
    if low:
        body_lines.append(render_tier_line("☕ Other", low, ONETIME_LOW_PX))
    if not body_lines:
        body = (
            f"_Thank-you donations welcome at "
            f"[github.com/sponsors/{ORG}](https://github.com/sponsors/{ORG})._"
        )
    else:
        body = "\n\n".join(body_lines)
    return f"### 🎁 One-time Sponsors\n\n{body}"


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

    # Each monthly tier collects its (cents, login, website) entries.
    monthly_by_tier: dict[int, list[tuple[int, str, str]]] = {t["cents"]: [] for t in tiers}
    onetime_high: list[tuple[int, str, str]] = []
    onetime_low: list[tuple[int, str, str]] = []

    # Active sponsorships -> monthly buckets (only currently-paying monthly subscribers).
    for s in data["organization"]["active"]["nodes"]:
        tier_info = s.get("tier") or {}
        if tier_info.get("isOneTime"):
            continue
        cents = tier_info.get("monthlyPriceInCents") or 0
        if cents < tiers[-1]["cents"]:
            continue
        target = next((t for t in tiers if t["cents"] <= cents), None)
        if target is None:
            continue
        entity = s["sponsorEntity"]
        login = entity["login"]
        website = normalize_url(entity.get("websiteUrl"), login)
        monthly_by_tier[target["cents"]].append((cents, login, website))

    # All sponsorships -> one-time buckets (keep historical donations, split $100+ vs other).
    for s in data["organization"]["all"]["nodes"]:
        tier_info = s.get("tier") or {}
        if not tier_info.get("isOneTime"):
            continue
        cents = tier_info.get("monthlyPriceInCents") or 0
        if cents < 1:
            continue
        entity = s["sponsorEntity"]
        login = entity["login"]
        website = normalize_url(entity.get("websiteUrl"), login)
        (onetime_high if cents >= ONETIME_HIGH_THRESHOLD_CENTS else onetime_low).append(
            (cents, login, website)
        )

    # Stable ordering: each bucket sorted by cents descending so larger sponsors lead.
    tiered_monthly = [
        (
            t,
            [(login, website) for _, login, website in sorted(monthly_by_tier[t["cents"]], key=lambda r: -r[0])],
        )
        for t in tiers
    ]
    onetime_high.sort(key=lambda r: -r[0])
    onetime_low.sort(key=lambda r: -r[0])
    high = [(login, website) for _, login, website in onetime_high]
    low = [(login, website) for _, login, website in onetime_low]

    block = f"{render_monthly(tiered_monthly)}\n\n{render_onetime(high, low)}"

    with open(FILE, "r", encoding="utf-8") as fh:
        content = fh.read()

    pattern = re.compile(r"<!-- sponsors -->.*?<!-- sponsors -->", re.DOTALL)
    if not pattern.search(content):
        sys.exit(f"Could not find <!-- sponsors --> markers in {FILE}")
    new_content = pattern.sub(f"<!-- sponsors -->\n{block}\n<!-- sponsors -->", content)

    with open(FILE, "w", encoding="utf-8") as fh:
        fh.write(new_content)

    monthly_count = sum(len(rows) for _, rows in tiered_monthly)
    print(
        f"Wrote {monthly_count} active monthly + {len(high)} one-time $100+ + "
        f"{len(low)} other one-time to {FILE}"
    )


if __name__ == "__main__":
    main()
