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
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
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


# Matches 2026-10-13 on its own and inside timestamps like 2026-10-13T00:00:00Z.
DATE_RE = re.compile(r"(?<!\d)(20\d\d-\d\d-\d\d)(?!\d)")
TITLE_KEY_RE = re.compile(
    r'"(?:title|name|heading|headline)"\s*:\s*(?:\[\s*0\s*,\s*)?"((?:[^"\\]|\\.){3,200})"'
)


def extract_dated(markup: str) -> list[tuple[str, str]]:
    """Read a "what's new" list of dated updates, whichever way the page ships it.

    Tries two readers and keeps whichever finds more: visible headings followed
    by a date, and data embedded in the page for the browser to draw later.
    """
    from_html = extract_dated_headings(markup)
    from_data = extract_dated_data(markup)
    return from_data if len(from_data) > len(from_html) else from_html


TITLE_KEYS = ("title", "name", "heading", "headline")


def _unwrap(value):
    """Astro ships data as [0, value] and [1, [items]]. Turn that back into plain JSON."""
    if isinstance(value, list):
        if len(value) == 2 and value[0] == 0:
            return _unwrap(value[1])
        if len(value) == 2 and value[0] == 1 and isinstance(value[1], list):
            return [_unwrap(v) for v in value[1]]
        return [_unwrap(v) for v in value]
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    return value


def _embedded_json(markup: str) -> list:
    """Every JSON document embedded in the page: data scripts and Astro props."""
    # application/ld+json is search-engine metadata about the site itself
    # (organisation, regional sites, the page), never a changelog. Skip it.
    blobs = re.findall(
        r"<script\b[^>]*(?:type=\"application/json\"|id=\"__NEXT_DATA__\")[^>]*>([\s\S]*?)</script>",
        markup, flags=re.I,
    )
    blobs += [html_mod.unescape(p) for p in re.findall(r'\bprops="([^"]*)"', markup)]
    docs = []
    for blob in blobs:
        try:
            docs.append(_unwrap(json.loads(blob)))
        except (json.JSONDecodeError, ValueError):
            continue
    return docs


def extract_dated_data(markup: str) -> list[tuple[str, str]]:
    """Find updates in data embedded in the page. A record counts when one object
    holds both a title and a date, so titles are never paired with a neighbour's date."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def walk(node):
        if isinstance(node, dict):
            if any(k.startswith("@") for k in node):
                return  # schema.org metadata, wherever it turns up
            title = next((node[k] for k in TITLE_KEYS if isinstance(node.get(k), str)), None)
            dated = sorted(node, key=lambda k: ("date" not in k.lower() and "publish" not in k.lower()))
            day = next(
                (m.group(1) for k in dated if isinstance(node[k], str) for m in [DATE_RE.search(node[k])] if m),
                None,
            )
            if title and day:
                title = re.sub(r"\s+", " ", title).strip()
                if 3 <= len(title) <= 200 and title.lower() not in NOISE and (day, title) not in seen:
                    seen.add((day, title))
                    pairs.append((day, title))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for doc in _embedded_json(markup):
        walk(doc)
    return pairs


def describe(markup: str) -> str:
    """One line on what a fetched page contains, for the log."""
    levels = {f"h{n}": len(re.findall(rf"<h{n}\b", markup, flags=re.I)) for n in range(1, 5)}
    dates = len(DATE_RE.findall(markup))
    hints = [h for h in ("astro-island", "__NEXT_DATA__", "application/json", "application/ld+json") if h in markup]
    return f"{len(markup)} chars, headings {levels}, {dates} dates, embedded data: {hints or 'none'}"


def extract_dated_headings(markup: str) -> list[tuple[str, str]]:
    """Read a page where each update is a heading followed by a date.

    Returns (date, title) pairs, e.g. ("2026-10-13", "The new Composer is live").
    """
    stripped = re.sub(rf"<({BLOCK_TAGS})\b[\s\S]*?</\1>", " ", markup, flags=re.I)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    headings = list(re.finditer(r"<h([2-4])\b[^>]*>(.*?)</h\1>", stripped, flags=re.S | re.I))
    for i, m in enumerate(headings):
        title = re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", m.group(2)))).strip()
        if not (3 <= len(title) <= 200) or title.lower() in NOISE:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(stripped)
        found = DATE_RE.search(TAG_RE.sub(" ", stripped[m.end():end]))
        if found and (found.group(1), title) not in seen:
            seen.add((found.group(1), title))
            pairs.append((found.group(1), title))
    return pairs


def extract_zendesk(raw: str, limit: int = 2) -> tuple[list[str], dict[str, dict[str, list[str]]]]:
    """Read a Zendesk help center articles feed (the JSON API).

    Release notes there are one article per quarter, updated as features ship.
    Returns entries like "Release Notes - Q3 2026: AI Grow" for diffing, plus
    the features of each article grouped by section, for the report.
    """
    data = json.loads(raw)
    articles = sorted(
        data.get("articles", []),
        key=lambda a: a.get("updated_at") or "",
        reverse=True,
    )[:limit]
    entries: list[str] = []
    details: dict[str, dict[str, list[str]]] = {}
    for article in articles:
        title = article.get("title", "").strip()
        grouped = sections("<body>" + (article.get("body") or "") + "</body>")
        if not grouped:
            continue
        details[article.get("html_url", title)] = grouped
        for features in grouped.values():
            entries.extend(f"{title}: {feature}" for feature in features)
    return entries, details


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def sections(markup: str) -> dict[str, list[str]]:
    """Group a page's headings into {section: [features]}.

    Works out the structure from the page rather than assuming it: the heading
    level used most often holds the features (ties go to the smaller heading),
    and the nearest larger level holds the sections. A larger level used only
    once, as the page's very first heading, is the page title, not a section.
    Headings smaller than the features are notes inside them and are skipped.
    """
    stripped = re.sub(rf"<({BLOCK_TAGS})\b[\s\S]*?</\1>", " ", markup, flags=re.I)
    heads: list[tuple[int, str]] = []
    for level, inner in HEADING_RE.findall(stripped):
        text = re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", inner))).strip()
        if 3 <= len(text) <= 200 and text.lower() not in NOISE:
            heads.append((int(level), text))
    if not heads:
        return {}

    counts: dict[int, int] = {}
    for lvl, _ in heads:
        counts[lvl] = counts.get(lvl, 0) + 1
    feature_level = max(counts, key=lambda lvl: (counts[lvl], lvl))

    section_level = None
    for lvl in sorted((l for l in counts if l < feature_level), reverse=True):
        is_title = counts[lvl] == 1 and heads[0][0] == lvl
        if not is_title:
            section_level = lvl
            break

    grouped: dict[str, list[str]] = {}
    current = ""
    for lvl, text in heads:
        if lvl == section_level:
            current = text
        elif lvl == feature_level:
            grouped.setdefault(current, []).append(text)
    return {k: v for k, v in grouped.items() if v}


def follow(urls: list[str], limit: int = 3) -> dict[str, dict[str, list[str]]]:
    """Open each new page and group its headings, so the report names the features."""
    details: dict[str, dict[str, list[str]]] = {}
    for url in urls[:limit]:
        if not url.startswith("http"):
            continue
        try:
            markup = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not open {url}: {exc}")
            continue
        grouped = sections(markup)
        if grouped:
            details[url] = grouped
        else:
            print(f"  no features found on {url}: {describe(markup)}")
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
    print(f"filling release details for: {', '.join(sorted(followable)) or 'no sources have follow_new_links'}")
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
                print(f"  {run['date']} {f['name']}: already has details")
                continue
            found = follow(missing, limit=len(missing))
            if found:
                f.setdefault("details", {}).update(found)
                filled += len(found)
                print(f"  {run['date']} {f['name']}: filled {len(found)} of {len(missing)}")
            else:
                print(f"  {run['date']} {f['name']}: nothing found")
    if filled:
        DIGEST_FILE.write_text(
            json.dumps(digest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(f"backfill done, {filled} release pages filled in")
    return 0


def backfill_dated(sources: list[dict]) -> None:
    """File every dated update from "what's new" pages into its month in digest.json.

    For each month the page covers, the competitor's items are replaced with what
    the page lists, so running this twice gives the same result.
    """
    digest = load_json(DIGEST_FILE, {"updated": None, "runs": []})

    # One-off cleanup: an earlier run read Klaviyo's site metadata as updates
    # (regional site names and a page title). Remove those items.
    bad = re.compile(r"^(Klaviyo( [A-Z]{2})?|Composer GA announcement - Oct 2026) \(20\d\d-\d\d-\d\d\)$")
    for run in digest["runs"]:
        for f in run["findings"]:
            if f["name"] == "Klaviyo":
                f["new"] = [i for i in f["new"] if not bad.match(i)]
        run["findings"] = [f for f in run["findings"] if f.get("new") or f.get("details")]

    for source in sources:
        if not source.get("dated"):
            continue
        name, url = source["name"], source["url"]
        print(f"backfilling {name} from {url}")
        try:
            markup = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  fetch failed: {exc}")
            continue
        print(f"  page: {describe(markup)}")
        heads, data = extract_dated_headings(markup), extract_dated_data(markup)
        print(f"  found {len(heads)} from headings, {len(data)} from embedded data")
        print(f"  headings sample: {heads[:4]}")
        print(f"  embedded sample: {data[:4]}")
        pairs = data if len(data) > len(heads) else heads
        if len(pairs) < 20:
            # Too few to be the real list. Print clues about where the list lives.
            h3s = [re.sub(r"\s+", " ", TAG_RE.sub("", h)).strip() for h in re.findall(r"<h3\b[^>]*>(.*?)</h3>", markup, flags=re.S | re.I)]
            print(f"  first h3 headings: {h3s[:6]}")
            srcs = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', markup, flags=re.I)
            apis = sorted(set(re.findall(r'https?://[^"\'\s<>]*(?:api|graphql|contentful|sanity|cms|json)[^"\'\s<>]*', markup)))
            print(f"  scripts: {srcs[:8]}")
            print(f"  data-looking urls: {apis[:8]}")
            for m in list(DATE_RE.finditer(markup))[:3]:
                snippet = re.sub(r"\s+", " ", markup[max(0, m.start() - 200): m.end() + 80])
                print(f"  around a date: {snippet}")
            print("  too few updates to trust, leaving this source's months unchanged")
            continue
        this_year = str(date.today().year)
        by_month: dict[str, list[str]] = {}
        for day, title in sorted(pairs):
            if not day.startswith(this_year):
                continue
            by_month.setdefault(day[:7], []).append(f"{title} ({day})")
        for month, items in by_month.items():
            run = next((r for r in digest["runs"] if r["date"].startswith(month)), None)
            if run is None:
                run = {"date": f"{month}-28", "summary": None, "findings": []}
                digest["runs"].append(run)
            run["findings"] = [f for f in run["findings"] if f["name"] != name]
            run["findings"].append({"name": name, "url": url, "new": items})
            print(f"  {month}: {len(items)} updates")
    digest["runs"].sort(key=lambda r: r["date"], reverse=True)
    DIGEST_FILE.write_text(
        json.dumps(digest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    sources = load_json(SOURCES_FILE, [])
    if not sources:
        print(f"No sources found in {SOURCES_FILE}", file=sys.stderr)
        return 1

    if "--backfill-details" in sys.argv:
        backfill_dated(sources)
        return backfill_details(sources)

    state = load_json(STATE_FILE, {})
    findings: list[dict] = []
    baselines: list[str] = []

    for source in sources:
        name, url = source["name"], source["url"]
        # Several sources can report under one competitor name. "key" keeps
        # their memories separate in state.json.
        key = source.get("key", name)
        print(f"checking {key}")
        try:
            markup = fetch(url)
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the rest
            print(f"  fetch failed: {exc}", file=sys.stderr)
            continue

        zendesk_details: dict = {}
        if source.get("type") == "zendesk":
            try:
                entries, zendesk_details = extract_zendesk(markup)
            except (json.JSONDecodeError, AttributeError) as exc:
                print(f"  not a help center feed: {exc}", file=sys.stderr)
                continue
        elif source.get("dated"):
            entries = [f"{title} ({day})" for day, title in extract_dated(markup)]
        else:
            entries = extract(markup, url, source.get("link_pattern"))
            if not entries:
                # No headings or links worth tracking. Fall back to the page text.
                entries = [s.strip() for s in clean_text(markup).split(". ") if len(s) > 40]
        if not entries:
            print("  nothing extracted, skipping", file=sys.stderr)
            continue

        previous = state.get(key, {}).get("entries", [])
        if not previous:
            baselines.append(key)
            print(f"  baseline: {len(entries)} entries")
        else:
            seen = set(previous)
            new = [e for e in entries if e not in seen]
            if new:
                finding = {"name": name, "url": source.get("link", url), "new": new}
                if source.get("follow_new_links"):
                    finding["details"] = follow([e for e in new if e.startswith("http")])
                elif zendesk_details:
                    # Report only the features that are new, grouped as on the page.
                    fresh = set(new)
                    trimmed = {}
                    for article_url, grouped in zendesk_details.items():
                        for section, feats in grouped.items():
                            keep = [f for f in feats if any(e.endswith(": " + f) and e in fresh for e in new)]
                            if keep:
                                trimmed.setdefault(article_url, {})[section] = keep
                    finding["details"] = trimmed
                    # Same shape as Braze: the article is the new item,
                    # its new features sit underneath it.
                    finding["new"] = list(trimmed) or new
                findings.append(finding)
                print(f"  {len(new)} new")
            else:
                print("  no change")

        state[key] = {"entries": entries, "checked": date.today().isoformat()}

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
