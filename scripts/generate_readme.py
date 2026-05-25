#!/usr/bin/env python3
"""
Generate GitHub profile README.md with dark/light SVG charts via QuickChart.

Assets produced (./assets/):
  pie-dark.svg / pie-light.svg           — language doughnut (no center label)
  commits-7d-dark.svg / commits-7d-light.svg — last 7 days commits line chart

README.md stats badges and activity block are patched from live GitHub GraphQL data.
Token: GH_PAT repo secret — classic PAT with read:user + repo scopes (for private repos)

macOS (python.org): if HTTPS fails with CERTIFICATE_VERIFY_FAILED, run:
  pip install certifi
or: /Applications/Python 3.12/Install Certificates.command
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date as dt_date, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
ASSETS_DIR = ROOT / "assets"

# ── Design tokens — exact values from global.css ──────────────────────────────
DARK = {
    "bg":         "#0d0d0d",
    "fg":         "#f0f0f0",
    "fg_sec":     "#a0a0a0",
    "fg_muted":   "#555555",
    "accent":     "#2563eb",
    "accent_hov": "#3b82f6",
    "border_hex": "#1a1a1a",
    "grid_line":  "#111111",
}
LIGHT = {
    "bg":         "#f5f5f4",
    "fg":         "#1c1917",
    "fg_sec":     "#57534e",
    "fg_muted":   "#a8a29e",
    "accent":     "#2563eb",
    "accent_hov": "#1d4ed8",
    "border_hex": "#e7e5e4",
    "grid_line":  "#ebebea",
}

# Fallback language colors (used when GraphQL doesn't return a color)
LANG_COLORS_FALLBACK: dict[str, str] = {
    "TypeScript":   "#3178c6",
    "JavaScript":   "#f1e05a",
    "Python":       "#3572A5",
    "Dart":         "#00B4AB",
    "Kotlin":       "#A97BFF",
    "Java":         "#b07219",
    "PHP":          "#4F5D95",
    "Swift":        "#F05138",
    "Go":           "#00ADD8",
    "Rust":         "#dea584",
    "Ruby":         "#701516",
    "C":            "#555555",
    "C++":          "#f34b7d",
    "C#":           "#178600",
    "Shell":        "#89e051",
    "HTML":         "#e34c26",
    "CSS":          "#563d7c",
    "Astro":        "#ff5a03",
    "Svelte":       "#ff3e00",
    "Vue":          "#41b883",
}

# Max languages in doughnut (portfolio shows 6)
MAX_LANGS = 6

# ── GitHub GraphQL ─────────────────────────────────────────────────────────────
# Language breakdown matches portfolio githubService.ts ACTIVITY_QUERY:
# count repos by primaryLanguage (public, non-fork), not byte-weighted languages().

LANG_QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(first: 100, privacy: PUBLIC, isFork: false) {
      nodes {
        primaryLanguage { name color }
      }
    }
  }
}
"""

GRAPHQL_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!, $after: String) {
  user(login: $login) {
    followers { totalCount }
    repositories(first: 100, after: $after, isFork: false, ownerAffiliations: OWNER) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
      }
    }
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date contributionCount }
        }
      }
    }
  }
}
"""


def _ssl_context() -> ssl.SSLContext:
    """CA bundle for HTTPS (fixes python.org macOS installs missing root certs)."""
    for cafile in (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
    ):
        if cafile and os.path.isfile(cafile):
            return ssl.create_default_context(cafile=cafile)

    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass

    for cafile in (
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",
    ):
        if os.path.isfile(cafile):
            return ssl.create_default_context(cafile=cafile)

    return ssl.create_default_context()


def _urlopen(req: urllib.request.Request, timeout: int = 30):
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
            raise RuntimeError(
                "HTTPS certificate verification failed. On macOS with python.org Python, run:\n"
                "  pip install certifi\n"
                "or open: Applications → Python 3.12 → Install Certificates.command"
            ) from e
        raise


def _graphql_request(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type":  "application/json",
            "User-Agent":    "charlymech-readme-generator",
        },
    )
    with _urlopen(req) as resp:
        raw = json.loads(resp.read())
    if "errors" in raw:
        raise RuntimeError(json.dumps(raw["errors"], indent=2))
    return raw["data"]


def fetch_languages(username: str, token: str) -> tuple[dict[str, int], dict[str, str]]:
    """Repo-count by primaryLanguage — same logic as portfolio githubService.ts."""
    data = _graphql_request(token, LANG_QUERY, {"login": username})
    lang_counts: dict[str, int] = defaultdict(int)
    lang_colors: dict[str, str] = {}

    for repo in data["user"]["repositories"]["nodes"]:
        pl = repo.get("primaryLanguage")
        if not pl:
            continue
        name = pl["name"]
        lang_counts[name] += 1
        if pl.get("color") and name not in lang_colors:
            lang_colors[name] = pl["color"]

    for name, fallback in LANG_COLORS_FALLBACK.items():
        if name not in lang_colors:
            lang_colors[name] = fallback

    return dict(lang_counts), lang_colors


def fetch_github(username: str, token: str, year: int) -> tuple[list[dict], dict]:
    """Fetch contribution days and profile stats (paginated owner repos)."""
    all_stars = 0
    repo_count = 0
    days: list[dict] = []
    stats: dict = {}

    cursor = None
    first_page = True

    while True:
        variables: dict = {
            "login": username,
            "from":  f"{year}-01-01T00:00:00Z",
            "to":    f"{year}-12-31T23:59:59Z",
            "after": cursor,
        }
        data = _graphql_request(token, GRAPHQL_QUERY, variables)
        user = data["user"]

        repos = user["repositories"]
        repo_count = repos["totalCount"]

        for repo in repos["nodes"]:
            all_stars += repo["stargazerCount"]

        if first_page:
            cc = user["contributionsCollection"]
            weeks = cc["contributionCalendar"]["weeks"]
            days = [
                {"date": d["date"], "count": d["contributionCount"]}
                for w in weeks for d in w["contributionDays"]
            ]
            stats = {
                "followers":           user["followers"]["totalCount"],
                "repos":               repo_count,
                "stars":               all_stars,
                "total_contributions": cc["contributionCalendar"]["totalContributions"],
            }
            first_page = False

        if repos["pageInfo"]["hasNextPage"]:
            cursor = repos["pageInfo"]["endCursor"]
        else:
            break

    stats["repos"] = repo_count
    stats["stars"] = all_stars

    return days, stats


# ── QuickChart helpers ─────────────────────────────────────────────────────────

QUICKCHART_URL = "https://quickchart.io/chart"


def _quickchart_svg(chart_config: dict, width: int = 860, height: int = 300, bg: str = "transparent") -> bytes:
    payload = json.dumps({
        "chart": chart_config,
        "width": width,
        "height": height,
        "backgroundColor": bg,
        "format": "svg",
        "version": "4",
    }).encode()
    req = urllib.request.Request(
        QUICKCHART_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with _urlopen(req) as resp:
        return resp.read()


def generate_pie_chart(lang_map: dict[str, int], lang_colors: dict[str, str], theme: str) -> bytes:
    t = DARK if theme == "dark" else LIGHT
    top = sorted(lang_map.items(), key=lambda x: x[1], reverse=True)[:MAX_LANGS]
    if not top:
        top = [("N/A", 1)]
    total = sum(v for _, v in top) or 1

    labels: list[str] = []
    data: list[int] = []
    colors: list[str] = []
    for name, count in top:
        pct = round(count / total * 100)
        labels.append(f"{name} {pct}%")
        data.append(count)
        colors.append(lang_colors.get(name, "#60a5fa"))

    config = {
        "type": "doughnut",
        "data": {
            "labels": labels,
            "datasets": [{
                "data": data,
                "backgroundColor": colors,
                "borderColor": t["bg"],
                "borderWidth": 2,
            }],
        },
        "options": {
            "layout": {"padding": {"top": 28, "left": 8, "right": 8}},
            "plugins": {
                "legend": {
                    "position": "right",
                    "labels": {
                        "color": t["fg_sec"],
                        "font": {"family": "'JetBrains Mono', monospace", "size": 11},
                        "padding": 14,
                        "usePointStyle": True,
                        "pointStyle": "circle",
                    },
                },
                "datalabels": {"display": False},
                "title": {
                    "display": True,
                    "text": "// languages",
                    "color": t["accent_hov"],
                    "font": {"family": "'JetBrains Mono', monospace", "size": 11},
                    "align": "start",
                    "padding": {"bottom": 8},
                },
            },
            "cutout": "55%",
        },
    }
    return _quickchart_svg(config, width=860, height=280, bg=t["bg"])


def generate_commits_7d_chart(days: list[dict], theme: str) -> bytes:
    t = DARK if theme == "dark" else LIGHT
    today = dt_date.today()
    last_7 = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

    day_map = {d["date"]: d["count"] for d in days}
    values = [day_map.get(d, 0) for d in last_7]
    labels = [d[5:] for d in last_7]  # MM-DD

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "Commits",
                "data": values,
                "borderColor": t["accent_hov"],
                "backgroundColor": t["accent"] + "33",
                "fill": True,
                "tension": 0.3,
                "pointBackgroundColor": t["accent_hov"],
                "pointBorderColor": t["bg"],
                "pointBorderWidth": 2,
                "pointRadius": 5,
            }],
        },
        "options": {
            "plugins": {
                "legend": {"display": False},
                "title": {
                    "display": True,
                    "text": "// commits last 7 days",
                    "color": t["accent_hov"],
                    "font": {"family": "'JetBrains Mono', monospace", "size": 11},
                    "align": "start",
                },
                "datalabels": {
                    "display": "function(ctx) { return ctx.dataset.data[ctx.dataIndex] > 0; }",
                    "align": "top",
                    "anchor": "end",
                    "offset": 4,
                    "color": t["fg_sec"],
                    "font": {"size": 10, "family": "'JetBrains Mono', monospace"},
                    "formatter": "function(value) { return value; }",
                },
            },
            "scales": {
                "x": {
                    "ticks": {"color": t["fg_muted"], "font": {"family": "'JetBrains Mono', monospace", "size": 10}},
                    "grid": {"color": t["grid_line"]},
                },
                "y": {
                    "beginAtZero": True,
                    "ticks": {"color": t["fg_muted"], "font": {"family": "'JetBrains Mono', monospace", "size": 10}, "stepSize": 1},
                    "grid": {"color": t["grid_line"]},
                },
            },
        },
    }
    return _quickchart_svg(config, width=860, height=220, bg=t["bg"])


# ── README patch markers ───────────────────────────────────────────────────────

STATS_START    = "<!-- readme:stats:start -->"
STATS_END      = "<!-- readme:stats:end -->"
ACTIVITY_START = "<!-- readme:activity:start -->"
ACTIVITY_END   = "<!-- readme:activity:end -->"


def build_badges(username: str, stats: dict, year: int) -> str:
    _ = stats, year  # reserved for custom badge variants
    return (
        f'![GitHub followers](https://img.shields.io/github/followers/{username}?style=flat) '
        f'![GithubStars](https://img.shields.io/github/stars/{username}?style=flat) '
        f'![Profile views](https://komarev.com/ghpvc/?username={username}&label=Profile%20views&style=flat)'
    )


def build_activity_block() -> str:
    return (
        '<picture>\n'
        '  <source media="(prefers-color-scheme: dark)"  srcset="./assets/commits-7d-dark.svg">\n'
        '  <source media="(prefers-color-scheme: light)" srcset="./assets/commits-7d-light.svg">\n'
        '  <img src="./assets/commits-7d-dark.svg" alt="Last 7 Days Commits" width="860"/>\n'
        '</picture>'
    )


def _replace_block(text: str, start: str, end: str, content: str) -> str:
    i = text.find(start)
    j = text.find(end)
    if i == -1 or j == -1:
        raise RuntimeError(f"README markers not found: {start!r} … {end!r}")
    j += len(end)
    return text[: i + len(start)] + "\n" + content + "\n" + end + text[j:]


def patch_readme(readme_path: Path, username: str, year: int, stats: dict) -> None:
    text = readme_path.read_text()
    text = _replace_block(text, STATS_START, STATS_END, build_badges(username, stats, year))
    text = _replace_block(text, ACTIVITY_START, ACTIVITY_END, build_activity_block())
    readme_path.write_text(text)


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    token    = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    username = os.environ.get("GITHUB_ACTOR", "CharlyMech")
    year     = dt_date.today().year

    if not token:
        print(
            "GH_PAT is required (classic PAT with read:user + repo scopes). "
            "Add it as a repo secret for the workflow, or export it locally.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Fetching GitHub data for {username} ({year})...")
    lang_map, lang_colors = fetch_languages(username, token)
    days, stats = fetch_github(username, token, year)

    ASSETS_DIR.mkdir(exist_ok=True)

    print("Generating charts via QuickChart...")
    for theme in ("dark", "light"):
        (ASSETS_DIR / f"pie-{theme}.svg").write_bytes(
            generate_pie_chart(lang_map, lang_colors, theme)
        )
        (ASSETS_DIR / f"commits-7d-{theme}.svg").write_bytes(
            generate_commits_7d_chart(days, theme)
        )

    patch_readme(ROOT / "README.md", username, year, stats)
    print(f"✓  4 SVGs written to assets/ + README.md updated ({year})")


if __name__ == "__main__":
    main()
