"""
Canvas sync for USF (University of San Francisco).

Parallel to the UoA canvas-sync, but using a Canvas API token (Bearer auth)
instead of a stored-session cookie. Metadata-only by default — mirrors
announcements, assignments, modules, pages (as Markdown), files listing
(metadata only — NO bulk download yet), tabs, and a log of LTI external
tools that aren't traversable via Canvas API.

Mirror root: ~/canvas-mirror/usf/. Each course gets its own subdir:

    <course-id>_<slug>/
      course.json                   # raw course payload
      STATE.md                      # human-readable per-course summary
      announcements/                # one .json + .md per announcement
      assignments/                  # one .json + .md per assignment
      modules/                      # one .json per module (incl. items)
      pages/                        # one .md per page
      files.json                    # file listing (metadata only)
      lti_external_skipped.json     # tabs / module items we can't traverse

Auth: reads bearer token from the path in USF_TOKEN_PATH (see below).
Base URL: https://usfca.instructure.com/api/v1/
Rate-limit floor: 150 ms between requests.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

try:
    from markdownify import markdownify as _md
except ImportError:
    _md = None


# ---------- config ----------

BASE = Path(__file__).parent
MIRROR_ROOT = Path.home() / "canvas-mirror" / "usf"
LOGS_DIR = Path.home() / "canvas-mirror" / "logs"
CANVAS_HOST = "https://usfca.instructure.com"
API_BASE = f"{CANVAS_HOST}/api/v1"

USF_TOKEN_PATH = Path.home() / ".config" / "credentials" / "usfca_canvas_token.txt"

USER_AGENT = "usf-canvas-sync/1.0 (personal study sync; contact jwin74@gmail.com)"

RATE_FLOOR_S = 0.15  # 150 ms


# ---------- small helpers (ported from UoA sync) ----------

def _log(msg, logfh=None):
    print(msg, flush=True)
    if logfh:
        logfh.write(msg + "\n")
        logfh.flush()


def slugify(s):
    if not s:
        return "item"
    s = re.sub(r"[^\w\s-]", "", str(s))
    s = re.sub(r"[\s_]+", "-", s).strip("-").lower()
    return s[:60] or "item"


def safe_filename(name):
    if not name:
        return "file"
    name = re.sub(r"[\x00-\x1f]", "", str(name))
    name = re.sub(r"[/\\]", "_", name).strip().strip(".")
    if not name:
        return "file"
    if len(name) > 200:
        m = re.search(r"\.[^.\s]{1,10}$", name)
        if m:
            name = name[: 200 - len(m.group(0))] + m.group(0)
        else:
            name = name[:200]
    return name


def html_to_md(html):
    if not html:
        return ""
    if _md:
        try:
            return _md(html, heading_style="ATX", bullets="-").strip()
        except Exception:
            pass
    return re.sub(r"<[^>]+>", "", html).strip()


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


def build_url(path, params=None):
    if path.startswith("http"):
        url = path
    else:
        url = f"{API_BASE}/{path.lstrip('/')}"
    if params:
        pairs = []
        for k, v in params.items():
            if isinstance(v, list):
                for item in v:
                    pairs.append((k, item))
            elif v is not None:
                pairs.append((k, v))
        if pairs:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(pairs)}"
    return url


def parse_next_link(link_header):
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


def rate_throttle(resp):
    try:
        rl = resp.headers.get("x-rate-limit-remaining")
        if rl is not None and float(rl) < 100:
            time.sleep(2.0)
    except Exception:
        pass
    time.sleep(RATE_FLOOR_S)


def api_get(request, path, params=None, logfh=None):
    url = build_url(path, params)
    resp = request.get(url)
    if resp.status >= 400:
        _log(f"  [warn] HTTP {resp.status} GET {path}", logfh)
        return None
    rate_throttle(resp)
    try:
        return resp.json()
    except Exception:
        return None


def api_list(request, path, params=None, logfh=None):
    params = {**(params or {})}
    params.setdefault("per_page", 100)
    url = build_url(path, params)
    items = []
    while url:
        resp = request.get(url)
        if resp.status >= 400:
            _log(f"  [warn] HTTP {resp.status} GET {url}", logfh)
            break
        try:
            data = resp.json()
        except Exception:
            break
        if isinstance(data, list):
            items.extend(data)
        else:
            return data
        url = parse_next_link(resp.headers.get("link") or resp.headers.get("Link") or "")
        rate_throttle(resp)
    return items


# ---------- course naming ----------

def course_dir(course):
    slug = slugify(course.get("course_code") or course.get("name") or str(course["id"]))
    return MIRROR_ROOT / f"{course['id']}_{slug}"


def course_label(course):
    return course.get("course_code") or course.get("name") or f"course {course.get('id')}"


# ---------- per-category syncs ----------

def sync_tabs(request, course, cdir, logfh):
    cid = course["id"]
    tabs = api_list(request, f"courses/{cid}/tabs", logfh=logfh) or []
    save_json(cdir / "tabs.json", tabs)
    visible = [t.get("label") for t in tabs if not t.get("hidden")]
    _log(f"  tabs: {len(visible)} visible — "
         f"{', '.join(visible[:6])}{'…' if len(visible) > 6 else ''}", logfh)
    return tabs


def sync_announcements(request, course, cdir, logfh):
    cid = course["id"]
    out_dir = cdir / "announcements"
    out_dir.mkdir(exist_ok=True)

    items = api_list(request, f"courses/{cid}/discussion_topics",
                     params={"only_announcements": "true"}, logfh=logfh)
    _log(f"  announcements: {len(items)}", logfh)

    # Write a per-announcement .json + .md, plus an aggregated announcements.json
    for a in items:
        aid = a.get("id")
        title = a.get("title") or f"announcement-{aid}"
        stem = f"{aid}_{slugify(title)}"
        save_json(out_dir / f"{stem}.json", a)
        body_md = html_to_md(a.get("message") or "")
        header = (
            f"# {title}\n\n"
            f"_Posted: {a.get('posted_at')}_ — "
            f"_Updated: {a.get('updated_at')}_\n\n"
            f"[Open in Canvas]({a.get('html_url')})\n\n"
            "---\n\n"
        )
        (out_dir / f"{stem}.md").write_text(header + body_md + "\n", encoding="utf-8")

    save_json(cdir / "announcements.json", items)
    return len(items)


def sync_assignments(request, course, cdir, logfh):
    cid = course["id"]
    out_dir = cdir / "assignments"
    out_dir.mkdir(exist_ok=True)

    items = api_list(request, f"courses/{cid}/assignments", logfh=logfh)
    _log(f"  assignments: {len(items)}", logfh)

    for a in items:
        aid = a.get("id")
        name = a.get("name") or f"assignment-{aid}"
        stem = f"{aid}_{slugify(name)}"
        save_json(out_dir / f"{stem}.json", a)
        desc_md = html_to_md(a.get("description") or "")
        header = [
            f"# {name}",
            "",
            f"- **Due:** {a.get('due_at') or '—'}",
            f"- **Points:** {a.get('points_possible')}",
            f"- **Submission types:** {', '.join(a.get('submission_types') or []) or '—'}",
            f"- **Canvas:** {a.get('html_url')}",
            "",
            "---",
            "",
        ]
        (out_dir / f"{stem}.md").write_text("\n".join(header) + desc_md + "\n",
                                            encoding="utf-8")

    save_json(cdir / "assignments.json", items)
    return len(items)


def sync_modules(request, course, cdir, logfh):
    cid = course["id"]
    out_dir = cdir / "modules"
    out_dir.mkdir(exist_ok=True)

    items = api_list(request, f"courses/{cid}/modules",
                     params={"include[]": ["items", "content_details"]},
                     logfh=logfh)
    _log(f"  modules: {len(items)}", logfh)

    for m in items:
        mid = m.get("id")
        name = m.get("name") or f"module-{mid}"
        stem = f"{mid}_{slugify(name)}"
        save_json(out_dir / f"{stem}.json", m)

    # aggregated for convenience
    save_json(cdir / "modules.json", items)
    return len(items)


def sync_pages(request, course, cdir, logfh):
    cid = course["id"]
    pages_dir = cdir / "pages"
    pages_dir.mkdir(exist_ok=True)

    listing = api_list(request, f"courses/{cid}/pages", logfh=logfh)
    _log(f"  pages: {len(listing)}", logfh)

    index = []
    for p in listing:
        purl = p.get("url")
        if not purl:
            continue
        full = api_get(request, f"courses/{cid}/pages/{purl}", logfh=logfh)
        if not full:
            continue
        title = full.get("title") or purl
        body_md = html_to_md(full.get("body") or "")
        fname = f"{slugify(purl)}.md"
        header = (
            f"# {title}\n\n"
            f"_Updated: {full.get('updated_at')}_ — "
            f"[Open in Canvas]({full.get('html_url')})\n\n"
            "---\n\n"
        )
        (pages_dir / fname).write_text(header + body_md + "\n", encoding="utf-8")
        index.append({
            "url": purl,
            "title": title,
            "updated_at": full.get("updated_at"),
            "file": fname,
            "html_url": full.get("html_url"),
        })

    save_json(cdir / "pages_index.json", index)
    return len(index)


def sync_files_listing(request, course, cdir, logfh):
    """Metadata only — we are NOT bulk-downloading files for USF."""
    cid = course["id"]
    files = api_list(request, f"courses/{cid}/files", logfh=logfh)
    save_json(cdir / "files.json", files)
    total_bytes = sum((f.get("size") or 0) for f in files)
    _log(f"  files listing: {len(files)} "
         f"({total_bytes / (1024 * 1024):.1f} MB if downloaded — NOT downloading)", logfh)
    return len(files), total_bytes


def sync_lti_external_skipped(tabs, modules, cdir, logfh):
    skipped = []
    seen = set()

    for t in tabs or []:
        if t.get("hidden") or t.get("type") != "external":
            continue
        key = ("tab", t.get("id"))
        if key in seen:
            continue
        seen.add(key)
        skipped.append({
            "source": "tab",
            "label": t.get("label"),
            "type": "external_tool",
            "html_url": t.get("html_url"),
            "full_url": t.get("full_url"),
        })

    for m in modules or []:
        for it in (m.get("items") or []):
            t = it.get("type")
            if t not in ("ExternalUrl", "ExternalTool"):
                continue
            key = (t, it.get("content_id") or it.get("external_url"))
            if key in seen:
                continue
            seen.add(key)
            skipped.append({
                "source": "module",
                "module": m.get("name"),
                "type": t,
                "title": it.get("title"),
                "external_url": it.get("external_url"),
                "html_url": it.get("html_url"),
            })

    save_json(cdir / "lti_external_skipped.json", skipped)
    _log(f"  lti external noted: {len(skipped)}", logfh)
    return len(skipped)


# ---------- per-course orchestration ----------

def write_state_md(course, cdir, counts):
    lines = [
        f"# {course_label(course)}",
        "",
        f"- **Course id:** {course['id']}",
        f"- **Name:** {course.get('name')}",
        f"- **Term:** {(course.get('term') or {}).get('name') or '—'}",
        f"- **Canvas:** {CANVAS_HOST}/courses/{course['id']}",
        f"- **Last sync:** {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Contents",
        "",
        f"- Announcements: {counts['announcements']}",
        f"- Assignments:   {counts['assignments']}",
        f"- Modules:       {counts['modules']}",
        f"- Pages:         {counts['pages']}",
        f"- Files (metadata only; not downloaded): {counts['files']} "
        f"({counts['files_bytes'] / (1024 * 1024):.1f} MB if fetched)",
        f"- LTI externals skipped: {counts['lti']}",
        "",
    ]
    (cdir / "STATE.md").write_text("\n".join(lines), encoding="utf-8")


def process_course(request, course, agg, logfh):
    _log(f"\n[course] {course_label(course)} (id={course['id']})", logfh)
    cdir = course_dir(course)
    cdir.mkdir(parents=True, exist_ok=True)
    save_json(cdir / "course.json", course)

    counts = {}
    tabs = sync_tabs(request, course, cdir, logfh)
    counts["announcements"] = sync_announcements(request, course, cdir, logfh)
    counts["assignments"]   = sync_assignments(request, course, cdir, logfh)
    counts["modules"]       = sync_modules(request, course, cdir, logfh)
    counts["pages"]         = sync_pages(request, course, cdir, logfh)
    n_files, files_bytes    = sync_files_listing(request, course, cdir, logfh)
    counts["files"]         = n_files
    counts["files_bytes"]   = files_bytes
    modules_data = load_json(cdir / "modules.json", [])
    counts["lti"]           = sync_lti_external_skipped(tabs, modules_data, cdir, logfh)

    write_state_md(course, cdir, counts)

    # aggregate for global report
    for k in ("announcements", "assignments", "modules", "pages", "files", "lti"):
        agg[k] += counts[k]
    agg["files_bytes"] += counts["files_bytes"]
    agg["courses_ok"] += 1


# ---------- main ----------

def _read_token():
    path = os.environ.get("USF_CANVAS_TOKEN_PATH") or str(USF_TOKEN_PATH)
    p = Path(path).expanduser()
    if not p.exists():
        print(f"[fail] token file not found: {p}", file=sys.stderr)
        sys.exit(2)
    tok = p.read_text(encoding="utf-8").strip()
    if not tok:
        print(f"[fail] token file is empty: {p}", file=sys.stderr)
        sys.exit(2)
    return tok


def main():
    parser = argparse.ArgumentParser(description="Sync USF Canvas metadata to local mirror (no bulk file download)")
    parser.add_argument("--log", help="Write progress log to this file in addition to stdout.")
    parser.add_argument("--course-id", type=int, default=None,
                        help="Only sync this course id (debug / single-course re-run)")
    opts = parser.parse_args()

    MIRROR_ROOT.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logfh = open(opts.log, "a", encoding="utf-8") if opts.log else None
    if logfh:
        _log(f"=== canvas_sync_usf.py run started {datetime.datetime.now().isoformat()} ===", logfh)

    token = _read_token()

    agg = {
        "courses_ok": 0, "courses_fail": 0,
        "announcements": 0, "assignments": 0, "modules": 0,
        "pages": 0, "files": 0, "files_bytes": 0, "lti": 0,
        "auth_errors": [],
    }

    with sync_playwright() as p:
        request = p.request.new_context(
            extra_http_headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )

        # Identity check
        me = api_get(request, "users/self/profile", logfh=logfh)
        if not me or "id" not in me:
            _log("[fail] token rejected or empty profile response.", logfh)
            agg["auth_errors"].append("users/self/profile returned no id")
            request.dispose()
            if logfh:
                logfh.close()
            sys.exit(3)
        _log(f"[auth] {me.get('name')} <{me.get('primary_email') or me.get('login_id')}>"
             f"  (id={me.get('id')})", logfh)

        courses = api_list(request, "courses", params={
            "enrollment_state": "active",
            "include[]": ["term"],
            "state[]": ["available"],
        }, logfh=logfh)
        courses = [c for c in courses if c.get("id") and not c.get("access_restricted_by_date")]
        save_json(MIRROR_ROOT / "courses.json", courses)

        if opts.course_id:
            courses = [c for c in courses if c["id"] == opts.course_id]

        _log(f"[courses] {len(courses)} active", logfh)
        for c in courses:
            _log(f"  - {course_label(c)} (id={c['id']})", logfh)

        for course in courses:
            try:
                process_course(request, course, agg, logfh)
            except Exception as e:
                _log(f"  [err] {course_label(course)}: {e}", logfh)
                agg["courses_fail"] += 1

        request.dispose()

    # final summary
    _log("\n" + "=" * 60, logfh)
    _log("USF CANVAS SYNC — SUMMARY", logfh)
    _log("=" * 60, logfh)
    _log(f"  Courses: {agg['courses_ok']} ok / {agg['courses_fail']} failed", logfh)
    _log(f"  Announcements: {agg['announcements']}", logfh)
    _log(f"  Assignments:   {agg['assignments']}", logfh)
    _log(f"  Modules:       {agg['modules']}", logfh)
    _log(f"  Pages:         {agg['pages']}", logfh)
    _log(f"  File records:  {agg['files']}  ({agg['files_bytes'] / (1024 * 1024):.1f} MB — not downloaded)", logfh)
    _log(f"  LTI externals: {agg['lti']}", logfh)
    if agg["auth_errors"]:
        _log(f"  Auth errors: {len(agg['auth_errors'])}", logfh)
        for e in agg["auth_errors"]:
            _log(f"    {e}", logfh)

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    save_json(LOGS_DIR / f"usf-summary-{ts}.json", agg)

    if logfh:
        _log(f"=== canvas_sync_usf.py run ended {datetime.datetime.now().isoformat()} ===", logfh)
        logfh.close()


if __name__ == "__main__":
    main()
