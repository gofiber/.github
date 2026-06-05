#!/usr/bin/env python3
"""Render the gofiber sponsors table into a README.

Reads `SPONSORS_TOKEN`, `ORG`, and `FILE` from the environment, fetches the
org's configured sponsor tiers and current sponsorships via GraphQL, derives
each tier's display title from the first markdown heading in its description
(so changes on github.com/sponsors/<org> propagate automatically), groups
sponsors by tier (each sponsor lands in the largest tier whose monthly
price is <= the sponsor's tier price, which correctly buckets both monthly
and one-time donations), and replaces the content between two
`<!-- sponsors-table -->` markers in the README.
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
        tier { monthlyPriceInCents }
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


def render_row(login: str, website: str, badge: str) -> str:
    return (
        '<tr>'
        f'<td align="center"><img src="https://github.com/{escape(login, quote=True)}.png" width="40" /></td>'
        f'<td><a href="{escape(website, quote=True)}">@{escape(login, quote=True)}</a></td>'
        f'<td>{badge}</td>'
        '</tr>'
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

    sponsors = data["organization"]["sponsorshipsAsMaintainer"]["nodes"]

    rows: list[tuple[int, str]] = []
    for s in sponsors:
        cents = (s.get("tier") or {}).get("monthlyPriceInCents") or 0
        if cents < tiers[-1]["cents"]:
            continue  # below the lowest configured tier (custom-amount donations under threshold)
        entity = s["sponsorEntity"]
        login = entity["login"]
        website = normalize_url(entity.get("websiteUrl"), login)
        target = next(t for t in tiers if t["cents"] <= cents)
        rows.append((target["cents"], render_row(login, website, target["title"])))

    rows.sort(key=lambda r: -r[0])

    if rows:
        block = "\n".join(row for _, row in rows)
    else:
        block = (
            '<tr><td colspan="3"><em>Be the first to '
            f'<a href="https://github.com/sponsors/{ORG}">sponsor {ORG}</a>.</em></td></tr>'
        )

    with open(FILE, "r", encoding="utf-8") as fh:
        content = fh.read()

    pattern = re.compile(r"<!-- sponsors-table -->.*?<!-- sponsors-table -->", re.DOTALL)
    if not pattern.search(content):
        sys.exit(f"Could not find <!-- sponsors-table --> markers in {FILE}")

    new_content = pattern.sub(
        f"<!-- sponsors-table -->\n{block}\n<!-- sponsors-table -->",
        content,
    )

    with open(FILE, "w", encoding="utf-8") as fh:
        fh.write(new_content)

    print(f"Wrote {len(rows)} sponsor rows across {len({r[0] for r in rows})} tiers to {FILE}")


if __name__ == "__main__":
    main()
