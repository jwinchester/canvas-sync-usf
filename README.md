# canvas-sync-usf

Canvas metadata sync for **University of San Francisco** (`usfca.instructure.com`), run as a parallel to the University of Auckland `canvas-sync` tool.

Key differences from the Auckland sync:

- **Auth:** Canvas API token (Bearer), not a stored browser session.
- **No bulk file downloads.** Files listing is captured as metadata only; bulk-download may be added later if needed.
- **No external-site scraping** (Auckland's runsheet scrapers don't have a USF equivalent).

## What gets synced per course

Mirror root: `~/canvas-mirror/usf/`.

```
~/canvas-mirror/usf/<course-id>_<slug>/
  course.json                   raw course payload
  STATE.md                      human-readable summary + last-sync timestamp
  tabs.json                     course tabs
  announcements/                one <id>_<slug>.json + .md per announcement
  announcements.json            aggregated listing for convenience
  assignments/                  one <id>_<slug>.json + .md per assignment
  assignments.json              aggregated
  modules/                      one <id>_<slug>.json per module (items included)
  modules.json                  aggregated
  pages/                        one <slug>.md per page (body rendered from HTML)
  pages_index.json              page URL → file + last-updated index
  files.json                    file listing (metadata only)
  lti_external_skipped.json     tabs + module items pointing at external tools
```

## Setup

### 1. Token

Store a Canvas API token (Account → Settings → "New Access Token") at:

```
~/.config/credentials/usfca_canvas_token.txt
```

One line, the raw token. `chmod 600` is a good idea.

To override the token path:

```
export USF_CANVAS_TOKEN_PATH=/some/other/path
```

### 2. Dependencies

- Python 3.10+
- [Playwright](https://playwright.dev/python/) (`pip install playwright` — the `request` context is used, so the browser binaries aren't strictly required, but installing them is harmless: `python -m playwright install`).
- [`markdownify`](https://pypi.org/project/markdownify/) is optional; if absent, page/announcement/assignment bodies are stripped of HTML tags as a fallback.

A minimal setup:

```
cd ~/jon-claude-grand-ham/projects/canvas-sync-usf
python3 -m venv .venv
.venv/bin/pip install playwright markdownify
```

The wrapper `run_canvas_sync_usf.sh` will pick up `.venv/bin/python` automatically if present.

## Usage

```
# Full sync of every active course:
./run_canvas_sync_usf.sh

# Or invoke the script directly (and pick your own log path):
python3 canvas_sync_usf.py --log ~/canvas-mirror/logs/usf-manual.log

# Re-sync a single course (debug / targeted re-run):
python3 canvas_sync_usf.py --course-id 1234567
```

Progress goes to stdout and (if `--log` is passed) to a tee'd logfile. A machine-readable summary JSON also lands in `~/canvas-mirror/logs/usf-summary-<timestamp>.json` after each run.

## Rate limiting

Every API call sleeps `150 ms` afterwards (the "rate-limit floor"). If Canvas starts reporting `x-rate-limit-remaining < 100`, that's bumped to a 2 s sleep until the window replenishes.

## What it does NOT do (yet)

- **No bulk file download.** The Auckland sync has `--files-bulk` that walks `/courses/:id/files` and downloads every non-locked file. The USF sync intentionally does not — same lesson learned from Auckland: get metadata stable first, pull files only when we know we want them.
- **No submissions pull.** Auckland's `sync_submissions()` fetches feedback + grade history; USF version omits that for now to keep scope tight.
- **No external-site scraping.** Auckland has per-course GitHub-Pages runsheets (BIOSCI 738); USF doesn't.

Add those back when we have a clear reason to.

## Relationship to `canvas-sync` (Auckland)

Parallel module, not unified. The auth shape, the scope (bulk vs metadata), and the external-site scraping make a one-scraper-two-modes design more confusing than two sibling scrapers. Shared patterns (slugify/safe_filename/html_to_md/pagination/rate-throttle) are re-implemented verbatim rather than extracted into a shared library — low line count, and keeps the two repos independently deployable.
