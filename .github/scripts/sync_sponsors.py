#!/usr/bin/env python3
"""Render the gofiber sponsors block into a README.

Reads `SPONSORS_TOKEN`, `ORG`, and `FILE` from the environment, fetches the
org's configured sponsor tiers and current sponsorships via GraphQL, derives
each tier's display title from the first markdown heading in its
description, and replaces the content between a single pair of markers:

  <!-- sponsors --><!-- sponsors -->

Layout: one HTML table per section, with one row per tier. Each row carries
the tier label in the first cell and an avatar wall of every sponsor at that
tier in the second cell. Avatar size shrinks for the lower tiers so the
section stays compact regardless of sponsor count. Monthly sponsors are
filtered to currently-active subscriptions; one-time donations are bucketed
into the same tier set so the labels match what donors see on the platform.
One-time donations are additionally limited to the last six months so the
section reflects recent supporters rather than growing without bound.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape

ORG = os.environ['ORG']
TOKEN = os.environ['SPONSORS_TOKEN']
FILE = os.environ.get('FILE', 'README.md')

# Only one-time donations created within this window are shown; monthly
# sponsors are unaffected. Roughly six months (counted in days so it stays a
# plain stdlib timedelta).
ONETIME_WINDOW_DAYS = 183

# Avatar sizes per tier value (cents). Bigger tiers render larger; tiers full
# of sponsors render smaller so a packed row still fits comfortably.
MONTHLY_TIER_PX = {
    100000: 60,  # $1000+
    50000:  55,  # $500+
    25000:  50,  # $250+
    10000:  45,  # $100+
    5000:   40,  # $50+
    1000:   34,  # $10+
    500:    32,  # $5+
}
# One-time visual prominence is dialled down a bit so the section reads as
# secondary to the monthly subscribers.
ONETIME_TIER_PX = {
    100000: 50,
    50000:  45,
    25000:  42,
    10000:  40,
    5000:   34,
    1000:   28,
    500:    26,
}

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
    all: sponsorshipsAsMaintainer(first: 100, activeOnly: false, includePrivate: false, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        createdAt
        tierSelectedAt
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


def avatar_wall(sponsors: list[tuple[str, str]], px: int) -> str:
    return "&nbsp;".join(avatar(login, website, px) for login, website in sponsors)


def size_for(cents: int, sizing: dict[int, int]) -> int:
    for threshold in sorted(sizing, reverse=True):
        if cents >= threshold:
            return sizing[threshold]
    return sizing[min(sizing)]


def bucket_for(tiers: list[dict], cents: int) -> dict | None:
    return next((t for t in tiers if t["cents"] <= cents), None)


def render_table(
    tiers: list[dict],
    grouped: dict[int, list[tuple[str, str]]],
    sizing: dict[int, int],
    empty_message: str,
) -> str:
    rows = []
    for t in tiers:  # high to low
        sponsors = grouped.get(t["cents"], [])
        if not sponsors:
            continue
        rows.append(
            "<tr>"
            f'<td valign="top"><strong>{t["title"]}</strong></td>'
            f'<td>{avatar_wall(sponsors, size_for(t["cents"], sizing))}</td>'
            "</tr>"
        )
    if not rows:
        rows.append(f'<tr><td colspan="2"><em>{empty_message}</em></td></tr>')
    body = "\n".join(rows)
    return (
        "<table>\n"
        f"{body}\n"
        "</table>"
    )


def parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect(
    nodes: list[dict],
    tiers: list[dict],
    want_one_time: bool,
    since: datetime | None = None,
) -> dict[int, list[tuple[str, str]]]:
    grouped: dict[int, list[tuple[int, str, str]]] = {t["cents"]: [] for t in tiers}
    for s in nodes:
        tier_info = s.get("tier") or {}
        if bool(tier_info.get("isOneTime")) != want_one_time:
            continue
        if since is not None:
            # Prefer the tier-selection date (when the donation was actually
            # made) and fall back to the sponsorship creation date.
            donated_at = parse_created_at(s.get("tierSelectedAt")) or parse_created_at(s.get("createdAt"))
            if donated_at is None or donated_at < since:
                continue
        cents = tier_info.get("monthlyPriceInCents") or 0
        if cents < tiers[-1]["cents"]:
            continue
        target = bucket_for(tiers, cents)
        if target is None:
            continue
        entity = s["sponsorEntity"]
        login = entity["login"]
        website = normalize_url(entity.get("websiteUrl"), login)
        grouped[target["cents"]].append((cents, login, website))
    # Sort within each bucket by sponsor cents desc, then drop the cents key.
    return {
        cents_key: [(login, website) for _, login, website in sorted(rows, key=lambda r: -r[0])]
        for cents_key, rows in grouped.items()
    }


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

    onetime_cutoff = datetime.now(timezone.utc) - timedelta(days=ONETIME_WINDOW_DAYS)
    monthly = collect(data["organization"]["active"]["nodes"], tiers, want_one_time=False)
    onetime = collect(
        data["organization"]["all"]["nodes"], tiers, want_one_time=True, since=onetime_cutoff
    )

    monthly_table = render_table(
        tiers,
        monthly,
        MONTHLY_TIER_PX,
        f'Be the first monthly sponsor and <a href="https://github.com/sponsors/{ORG}">support {ORG}</a>.',
    )
    onetime_table = render_table(
        tiers,
        onetime,
        ONETIME_TIER_PX,
        f'Thank-you donations welcome at <a href="https://github.com/sponsors/{ORG}">github.com/sponsors/{ORG}</a>.',
    )

    block = (
        "### 📅 Monthly Sponsors\n\n"
        f"{monthly_table}\n\n"
        "### 🎁 One-time Sponsors\n\n"
        f"{onetime_table}"
    )

    with open(FILE, "r", encoding="utf-8") as fh:
        content = fh.read()

    pattern = re.compile(r"<!-- sponsors -->.*?<!-- sponsors -->", re.DOTALL)
    if not pattern.search(content):
        sys.exit(f"Could not find <!-- sponsors --> markers in {FILE}")
    # Keep a blank line after the opening marker so the rendered block stays
    # Prettier-compliant; otherwise `prettier --check` fails on every PR.
    new_content = pattern.sub(f"<!-- sponsors -->\n\n{block}\n<!-- sponsors -->", content)

    with open(FILE, "w", encoding="utf-8") as fh:
        fh.write(new_content)

    monthly_count = sum(len(v) for v in monthly.values())
    onetime_count = sum(len(v) for v in onetime.values())
    print(f"Wrote {monthly_count} active monthly + {onetime_count} one-time sponsors to {FILE}")


if __name__ == "__main__":
    main()
