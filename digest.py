#!/usr/bin/env python3
"""Weekly competitor digest.

Fetches every source in sources.json, pulls out headings and links, diffs them
against state.json, and reports whatever is new. Standard library only.

Reporting needs no credentials of yours. Running inside GitHub Actions, it opens
an issue in the repo, which GitHub notifies you about. It also writes digest.json
so a web page can render the history.

Environment variables:
  GITHUB_TOKEN          supplied by GitHub Actions. Scoped to this repo, expires
                        with the run. Needs `permissions: issues: write`.
  GITHUB_REPOSITORY     supplied by GitHub Actions, as owner/repo.
  ANTHROPIC_API_KEY     optional. Adds a short ranked summary on top of the diff.
  ANTHROPIC_MODEL       optional. Defaults to claude-haiku-5-5.

Run locally without reporting anything:
  python3 digest.py --dry-run
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
STATE_FILE = ROOT / "state.json"
DIGEST_FILE = ROOT / "digest.json"
HISTORY_LIMIT = 52

USER_AGENT = "Mozilla/5.0 (compatible; CompetitorDigest/1.0)"
BLOCK_TAGS = r"script|style|noscript|svg|nav|header|footer|form|aside"
HEADING_RE = re.compile(r"<h([1-4])\b[^>]*>(.*?)</h\1>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")

# Headings that appear on every page and carry no news.
NOISE = {
    "menu", "search", "navigation", "resources", "company", "products",
    "solutions", "legal", "follow us", "newsletter", "subscribe", "footer",
    "on this page", "table of contents", "related", "get started", "contact us",
}


def fetch(url: str, timeout: int = 30) -> str:
    """GET a URL and return decoded text."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def clean_text(markup: str) -> str:
    """Strip markup and page furniture, leaving readable text."""
    start = markup.lower().find("<body")
    body = markup[start:] if start > 0 else markup
    body = re.sub(rf"<({BLOCK_TAGS})\b[\s\S]*?</\1>", " ", body, flags=re.I)
    body = re.sub(r"<!--[\s\S]*?-->", " ", body)
    body = TAG_RE.sub(" ", body)
    return re.sub(r"\s+", " ", html_mod.unescape(body)).strip()


def extract(markup: str, base_url: str, link_pattern: str | None) -> list[str]:
    """Return the entries we track for one page: headings, then matching links."""
    entries: list[str] = []
    seen: set[str] = set()

    stripped = re.sub(rf"<({BLOCK_TAGS})\b[\s\S]*?</\1>", " ", markup, flags=re.I)
    for _, inner in HEADING_RE.findall(stripped):
        text = re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", inner))).strip()
        if 3 <= len(text) <= 200 and text.lower() not in NOISE and text not in seen:
            seen.add(text)
            entries.append(text)

    if link_pattern:
        for href in re.findall(r'href="([^"]+)"', markup, flags=re.I):
            href = href.split("#")[0].split("?")[0].rstrip("/")
            if href and re.search(link_pattern, href):
                url = urljoin(base_url, href)
                if url not in seen:
                    seen.add(url)
                    entries.append(url)

    return entries


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def sections(markup: str) -> dict[str, list[str]]:
    """Group a page's headings into {section: [features]}.

    h2 starts a section, h3 and h4 are the features inside it, and h1 (the page
    title) is skipped. Pages with no h2 put everything under "".
    """
    stripped = re.sub(rf"<({BLOCK_TAGS})\b[\s\S]*?</\1>", " ", markup, flags=re.I)
    grouped: dict[str, list[str]] = {}
    current = ""
    for level, inner in HEADING_RE.findall(stripped):
        text = re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", inner))).strip()
        if not (3 <= len(text) <= 200) or text.lower() in NOISE:
            continue
        if level == "1":
            continue
        if level == "2":
            current = text
            grouped.setdefault(current, [])
        else:
            grouped.setdefault(current, []).append(text)
    return {k: v for k, v in grouped.items() if v}


def follow(urls: list[str], limit: int = 3) -> dict[str, dict[str, list[str]]]:
    """Open each new page and group its headings, so the report names the features."""
    details: dict[str, dict[str, list[str]]] = {}
    for url in urls[:limit]:
        if not url.startswith("http"):
            continue
        try:
            grouped = sections(fetch(url))
        except Exception as exc:  # noqa: BLE001
            print(f"  could not open {url}: {exc}", file=sys.stderr)
            continue
        if grouped:
            details[url] = grouped
    return details


def summarize(findings: list[dict], api_key: str, model: str) -> str | None:
    """Optional. Ask Claude to rank the new items. Returns None on any failure."""
    lines = []
    for f in findings:
        lines.append(f"## {f['name']} ({f['url']})")
        lines.extend(f"- {item}" for item in f["new"][:60])
        for url, grouped in f.get("details", {}).items():
            lines.append(f"### contents of {url}")
            for section, features in grouped.items():
                if section:
                    lines.append(f"{section}:")
                lines.extend(f"- {h}" for h in features)
    prompt = (
        "You track competitors for a hotel CRM and marketing automation product.\n"
        "Below are items that appeared on competitor pages since the last check.\n"
        "Rank the 3 to 5 most relevant and give one line each on why it matters.\n"
        "Tag each: pricing, new feature, AI/agent, partnership, or other.\n"
        "Skip API, SDK, security and admin-setting items unless they affect "
        "marketers or guest messaging. Be terse. No preamble.\n\n"
        + "\n".join(lines)
    )
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(
            block.get("text", "") for block in data.get("content", [])
        ).strip() or None
    except Exception as exc:  # noqa: BLE001 - the digest still goes out without it
        print(f"  summary skipped: {exc}", file=sys.stderr)
        return None


def build_report(findings: list[dict], baselines: list[str], summary: str | None) -> str:
    """Markdown body for the GitHub issue."""
    parts: list[str] = []
    if summary:
        parts.append("## What matters\n")
        parts.append(summary + "\n")
    parts.append("## What changed\n")
    for f in findings:
        parts.append(f"### {f['name']} ({len(f['new'])} new)")
        parts.append(f"<{f['url']}>\n")
        for item in f["new"][:40]:
            parts.append(f"- {item}")
        if len(f["new"]) > 40:
            parts.append(f"- ...and {len(f['new']) - 40} more")
        for url, grouped in f.get("details", {}).items():
            parts.append(f"\n**{url}**\n")
            for section, features in grouped.items():
                if section:
                    parts.append(f"\n*{section}*\n")
                parts.extend(f"- {h}" for h in features)
        parts.append("")
    if baselines:
        parts.append("First check, baseline saved for: " + ", ".join(baselines))
    return "\n".join(parts).strip() + "\n"


def post_issue(title: str, body: str) -> bool:
    """Open an issue in this repo. Uses the token GitHub Actions supplies."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body, "labels": ["digest"]}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"opened issue: {json.loads(resp.read())['html_url']}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"could not open issue: {exc}", file=sys.stderr)
        return False


def record(findings: list[dict], summary: str | None) -> None:
    """Append this run to digest.json so a web page can render the history."""
    digest = load_json(DIGEST_FILE, {"updated": None, "runs": []})
    digest["updated"] = date.today().isoformat()
    digest["runs"].insert(
        0,
        {
            "date": date.today().isoformat(),
            "summary": summary,
            "findings": findings,
        },
    )
    digest["runs"] = digest["runs"][:HISTORY_LIMIT]
    DIGEST_FILE.write_text(
        json.dumps(digest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def backfill_details(sources: list[dict]) -> int:
    """Open release links in past runs that have no details yet, and fill them in.

    One-off repair for history written before details existed. Touches only
    digest.json: no diffing, no state changes, no issue.
    """
    followable = {s["name"] for s in sources if s.get("follow_new_links")}
    digest = load_json(DIGEST_FILE, {"updated": None, "runs": []})
    filled = 0
    for run in digest["runs"]:
        for f in run.get("findings", []):
            if f["name"] not in followable:
                continue
            missing = [
                u for u in f.get("new", [])
                if u.startswith("http") and u not in f.get("details", {})
            ]
            if not missing:
                continue
            found = follow(missing, limit=len(missing))
            if found:
                f.setdefault("details", {}).update(found)
                filled += len(found)
                print(f"  {run['date']} {f['name']}: filled {len(found)} of {len(missing)}")
            else:
                print(f"  {run['date']} {f['name']}: nothing found", file=sys.stderr)
    if filled:
        DIGEST_FILE.write_text(
            json.dumps(digest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(f"backfill done, {filled} release pages filled in")
    return 0


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    sources = load_json(SOURCES_FILE, [])
    if not sources:
        print(f"No sources found in {SOURCES_FILE}", file=sys.stderr)
        return 1

    if "--backfill-details" in sys.argv:
        return backfill_details(sources)

    state = load_json(STATE_FILE, {})
    findings: list[dict] = []
    baselines: list[str] = []

    for source in sources:
        name, url = source["name"], source["url"]
        print(f"checking {name}")
        try:
            markup = fetch(url)
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the rest
            print(f"  fetch failed: {exc}", file=sys.stderr)
            continue

        entries = extract(markup, url, source.get("link_pattern"))
        if not entries:
            # No headings or links worth tracking. Fall back to the page text.
            entries = [s.strip() for s in clean_text(markup).split(". ") if len(s) > 40]
        if not entries:
            print("  nothing extracted, skipping", file=sys.stderr)
            continue

        previous = state.get(name, {}).get("entries", [])
        if not previous:
            baselines.append(name)
            print(f"  baseline: {len(entries)} entries")
        else:
            new = [e for e in entries if e not in set(previous)]
            if new:
                finding = {"name": name, "url": url, "new": new}
                if source.get("follow_new_links"):
                    finding["details"] = follow([e for e in new if e.startswith("http")])
                findings.append(finding)
                print(f"  {len(new)} new")
            else:
                print("  no change")

        state[name] = {"entries": entries, "checked": date.today().isoformat()}

    if not dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not findings:
        print("nothing new, staying quiet")
        return 0

    summary = None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        summary = summarize(
            findings, api_key, os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
        )

    names = ", ".join(f["name"] for f in findings)
    title = f"Competitor digest {date.today().isoformat()}: {names}"
    body = build_report(findings, baselines, summary)

    if dry_run:
        print("\n--- dry run, nothing written or posted ---\n")
        print(title)
        print()
        print(body)
        return 0

    record(findings, summary)
    if not post_issue(title, body):
        print("no GitHub token, so here is the digest instead:\n")
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
