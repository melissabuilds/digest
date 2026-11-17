# Competitor digest

A weekly agent that watches competitor release notes and press pages, works out
what changed since last week, and files the result as a GitHub issue. Runs on
GitHub Actions, which is free for this amount of work. No dependencies beyond
the Python standard library, and no credentials of your own anywhere.

## How it works

1. Fetches every page listed in `sources.json`.
2. Pulls out the headings and, where a `link_pattern` is set, the links that match it.
3. Diffs that list against `state.json`, which holds what it saw last week.
4. For sources with `follow_new_links`, opens each new page and lists its headings,
   so the report names the actual features rather than just linking to them.
5. Opens an issue with whatever is new. If nothing changed, it stays quiet.
6. Writes `digest.json` and commits it along with `state.json`, so the git history
   doubles as a record of when each competitor shipped.

The first run for any source saves a baseline and reports no changes, which is
expected. Real diffs start the week after.

## Why an issue instead of an email

GitHub emails you about issues in your own repositories, and pushes to the GitHub
mobile app, so you still get the digest in front of you. The difference is the
credential. Sending email would mean storing a Gmail app password, a long-lived
secret that can send mail as you. The issue route uses the token Actions mints
for each run, which is scoped to this one repository and expires when the run
ends. Nothing of yours is stored.

## Setup

1. Create a repo and push these files. `digest.yml` goes in `.github/workflows/`.
2. Go to the Actions tab and run **Competitor digest** manually once. That saves
   the baseline and confirms each source fetches cleanly.
3. Leave it alone. It runs every Monday morning.

That is the whole setup. There are no required secrets.

Watch the run log on that first run. It prints how many entries it found per
source. A source showing zero needs its `link_pattern` adjusted, or is rendered
in the browser and cannot be read by a plain fetch.

## The optional summary

Without `ANTHROPIC_API_KEY`, the issue contains the raw diff: new feature names
and links, grouped by competitor. That is free and usually enough.

With the key set as a repo secret, the new items are also passed to Claude for a
short ranked summary at the top, tagged by type, with developer and admin noise
filtered out. A month of this costs well under a cent, since it only runs when
something changed. Get a key at [console.anthropic.com](https://console.anthropic.com)
and add credits. Set `ANTHROPIC_MODEL` to use something other than
`claude-haiku-5-5`.

If the key is missing or the call fails, the digest still goes out without the
summary.

## Putting it on a web page

`digest.json` holds the last 52 runs, newest first:

```json
{
  "updated": "2026-11-16",
  "runs": [
    {
      "date": "2026-11-16",
      "summary": "...or null",
      "findings": [
        {
          "name": "Braze",
          "url": "https://www.braze.com/docs/releases/2026/",
          "new": ["https://www.braze.com/docs/releases/2026/11_12_26"],
          "details": {
            "https://www.braze.com/docs/releases/2026/11_12_26": [
              "Segment reporting",
              "Account objects"
            ]
          }
        }
      ]
    }
  ]
}
```

If the repo is public, any front end can read it straight from
`https://raw.githubusercontent.com/USER/REPO/main/digest.json` with no backend
and no auth. A private repo keeps it off the open web, and you read the issues
instead.

## Adding a competitor

Add an entry to `sources.json`:

```json
{
  "name": "Mews",
  "url": "https://example.com/release-notes",
  "link_pattern": "/releases/",
  "follow_new_links": true
}
```

- `link_pattern` is a regular expression matched against each link on the page.
  Leave it out to track headings only.
- `follow_new_links` opens each new matching link and lists its headings. Only
  worth setting when the page is an index of dated releases.

Some pages are rendered in the browser and return almost nothing to a plain
fetch. Klaviyo is one of these. For those, subscribe to the competitor's email
updates and filter them into a label instead.

## Running it locally

```bash
python3 digest.py --dry-run
```

Prints the report it would file and leaves `state.json` and `digest.json`
untouched.
