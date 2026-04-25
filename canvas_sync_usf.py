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
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

try:
    from markdownify import markdownify as _md
except ImportError:
    _md = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "canvas-common"))
import auth as canvas_auth  # noqa: E402
import attachments as canvas_attachments  # noqa: E402


# ---------- config ----------

BASE = Path(__file__).parent
MIRROR_ROOT = Path.home() / "jon-claude-grand-ham" / "canvas-mirror" / "usf"
LOGS_DIR = Path.home() / "jon-claude-grand-ham" / "canvas-mirror" / "logs"
CANVAS_HOST = "https://usfca.instructure.com"
API_BASE = f"{CANVAS_HOST}/api/v1"

USF_TOKEN_PATH = Path.home() / ".config" / "credentials" / "usfca_canvas_token.txt"

USER_AGENT = "usf-canvas-sync/1.0 (personal study sync; contact jwin74@gmail.com)"

RATE_FLOOR_S = 0.15  # 150 ms
LARGE_FILE_THRESHOLD = 500 * 1024 * 1024  # 500 MB


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
    err = canvas_auth.detect_failure_in_response(resp, scope="usf", expect_json=True)
    if err:
        raise err
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
        err = canvas_auth.detect_failure_in_response(resp, scope="usf", expect_json=True)
        if err:
            raise err
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


def download_stream(url, dest, expected_size=None, logfh=None):
    """Stream-download a file with atomic rename. USF file URLs from the API
    carry a verifier so no auth header is needed. Returns (status, bytes)
    where status is 'downloaded' | 'skipped_exists' | 'failed'."""
    if dest.exists() and expected_size:
        try:
            if dest.stat().st_size == expected_size:
                return "skipped_exists", dest.stat().st_size
        except Exception:
            pass

    tmp = dest.with_name(dest.name + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        try:
            tmp.unlink()
        except Exception:
            pass

    headers = {"User-Agent": USER_AGENT}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=180) as resp:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f, length=131072)
        tmp.rename(dest)
        return "downloaded", dest.stat().st_size
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        _log(f"    [dl-fail] {dest.name}: {e}", logfh)
        return "failed", 0


def sync_files_listing(request, course, cdir, logfh, files_bulk=False, dl_stats=None):
    """Always emit files.json (metadata). When files_bulk=True, also walk the
    folder hierarchy and download every non-locked file under <course>/files/.
    Dedupes against on-disk size. dl_stats is mutated in-place when bulk."""
    cid = course["id"]
    files = api_list(request, f"courses/{cid}/files", logfh=logfh)
    save_json(cdir / "files.json", files)
    total_bytes = sum((f.get("size") or 0) for f in files)

    if not files_bulk:
        _log(f"  files listing: {len(files)} "
             f"({total_bytes / (1024 * 1024):.1f} MB if downloaded — NOT downloading)", logfh)
        return len(files), total_bytes

    folders = api_list(request, f"courses/{cid}/folders", logfh=logfh) or []
    folder_paths = {}
    for f in folders:
        fid = f.get("id")
        full = f.get("full_name", "") or ""
        if full == "course files":
            rel = ""
        elif full.startswith("course files/"):
            rel = full[len("course files/"):]
        else:
            rel = full
        folder_paths[fid] = rel

    modules = load_json(cdir / "modules.json", [])
    file_ids = {f.get("id") for f in files if f.get("id")}
    extras = []
    for m in modules:
        for it in (m.get("items") or []):
            if it.get("type") == "File":
                fid = it.get("content_id")
                if fid and fid not in file_ids:
                    extra = api_get(request, f"files/{fid}", logfh=logfh)
                    if extra:
                        extras.append(extra)
                        file_ids.add(fid)
    if extras:
        _log(f"  +{len(extras)} module-only file(s) discovered", logfh)
        files = files + extras

    files_dir = cdir / "files"
    files_dir.mkdir(exist_ok=True)
    c = {"downloaded": 0, "skipped": 0, "failed": 0, "locked": 0,
         "total": len(files), "bytes": 0}
    cname = course_label(course)
    large_here = []

    for i, fobj in enumerate(files, 1):
        fid = fobj.get("id")
        display = fobj.get("display_name") or fobj.get("filename") or f"file_{fid}"
        size = fobj.get("size") or 0
        url = fobj.get("url")
        locked = fobj.get("locked_for_user") or fobj.get("locked")

        if locked:
            c["locked"] += 1
            _log(f"  [{i}/{len(files)}] [locked] {display}", logfh)
            continue
        if not url:
            c["failed"] += 1
            _log(f"  [{i}/{len(files)}] [no-url] {display}", logfh)
            continue

        folder_rel = folder_paths.get(fobj.get("folder_id"), "")
        dest = files_dir
        for part in folder_rel.split("/"):
            if part:
                dest = dest / safe_filename(part)
        dest = dest / safe_filename(display)

        size_mb = size / 1024 / 1024
        if size >= LARGE_FILE_THRESHOLD:
            large_here.append({"course": cname, "name": display, "size": size, "path": str(dest)})
            _log(f"  [{i}/{len(files)}] [LARGE {size_mb:.0f} MB] {display}", logfh)

        status, written = download_stream(url, dest, expected_size=size, logfh=logfh)
        if status == "downloaded":
            c["downloaded"] += 1
            c["bytes"] += written
            _log(f"  [{i}/{len(files)}] [+ {size_mb:.1f} MB] {display}", logfh)
        elif status == "skipped_exists":
            c["skipped"] += 1
            c["bytes"] += written
            _log(f"  [{i}/{len(files)}] [= {size_mb:.1f} MB] {display}", logfh)
        else:
            c["failed"] += 1

        time.sleep(RATE_FLOOR_S)

    if dl_stats is not None:
        dl_stats["courses"][cname] = c
        for k in ("downloaded", "skipped", "failed", "locked", "bytes"):
            dl_stats[k] += c[k]
        dl_stats["large_files"].extend(large_here)

    _log(f"  files bulk: {c['downloaded']} dl / {c['skipped']} skip / "
         f"{c['failed']} fail / {c['locked']} locked / {c['total']} total", logfh)
    return len(files), total_bytes


def sync_submissions(request, course, cdir, sub_stats, logfh):
    """Pull self-submissions with comments + history. Download my submitted
    files and any instructor feedback attachments. Write feedback.md per
    assignment that has anything worth keeping. Ported from Auckland scraper."""
    cid = course["id"]
    cname = course_label(course)
    subs_root = cdir / "submissions"
    subs_root.mkdir(exist_ok=True)

    subs = api_list(request, f"courses/{cid}/students/submissions", params={
        "student_ids[]": ["self"],
        "include[]": [
            "submission_comments",
            "rubric_assessment",
            "submission_history",
            "assignment",
        ],
    }, logfh=logfh)

    save_json(cdir / "submissions.json", subs)

    meaningful = []
    for s in subs:
        if (s.get("attachments")
            or s.get("submission_comments")
            or s.get("rubric_assessment")
            or s.get("body")
            or s.get("submitted_at")
            or s.get("score") is not None
            or s.get("grade")):
            meaningful.append(s)

    if not meaningful:
        _log("  submissions: none with content", logfh)
        return

    s_stats = {"assignments": 0, "my_files": 0, "feedback_files": 0,
               "downloaded": 0, "skipped": 0, "failed": 0}

    for s in meaningful:
        asg = s.get("assignment") or {}
        aid = s.get("assignment_id") or asg.get("id")
        aname = asg.get("name") or f"assignment-{aid}"
        sdir = subs_root / f"{aid}_{slugify(aname)}"
        sdir.mkdir(parents=True, exist_ok=True)
        save_json(sdir / "submission.json", s)
        s_stats["assignments"] += 1

        my_dir = sdir / "my"
        fb_dir = sdir / "feedback"

        my_attachments_index = []
        for att in (s.get("attachments") or []):
            att_id = att.get("id")
            fname = canvas_attachments.safe_filename(
                att.get("display_name") or att.get("filename") or f"att_{att_id}"
            )
            stored = f"{att_id}_{fname}"
            dest = my_dir / stored
            url = att.get("url")
            if not url:
                continue
            status, _ = canvas_attachments.download_url_to_path(
                url, dest, expected_size=att.get("size"),
                user_agent=USER_AGENT,
                logger=lambda m: _log(m, logfh),
            )
            if status == "downloaded":
                s_stats["downloaded"] += 1
            elif status == "skipped_exists":
                s_stats["skipped"] += 1
            else:
                s_stats["failed"] += 1
            s_stats["my_files"] += 1
            my_attachments_index.append({
                "stored": stored, "display": att.get("display_name"),
                "size": att.get("size"),
            })
            time.sleep(0.1)

        feedback_comments_index = []
        for co in (s.get("submission_comments") or []):
            author = co.get("author_name") or (co.get("author") or {}).get("display_name") or "instructor"
            co_entry = {
                "author": author,
                "created_at": co.get("created_at"),
                "comment": co.get("comment"),
                "attachments": [],
            }
            for att in (co.get("attachments") or []):
                att_id = att.get("id")
                fname = canvas_attachments.safe_filename(
                    att.get("display_name") or att.get("filename") or f"att_{att_id}"
                )
                stored = f"{att_id}_{fname}"
                dest = fb_dir / stored
                url = att.get("url")
                if not url:
                    continue
                status, _ = canvas_attachments.download_url_to_path(
                    url, dest, expected_size=att.get("size"),
                    user_agent=USER_AGENT,
                    logger=lambda m: _log(m, logfh),
                )
                if status == "downloaded":
                    s_stats["downloaded"] += 1
                elif status == "skipped_exists":
                    s_stats["skipped"] += 1
                else:
                    s_stats["failed"] += 1
                s_stats["feedback_files"] += 1
                co_entry["attachments"].append({
                    "stored": stored, "display": att.get("display_name"),
                    "size": att.get("size"),
                })
                time.sleep(0.1)
            feedback_comments_index.append(co_entry)

        (sdir / "feedback.md").write_text(
            _render_feedback_md(cname, asg, s, my_attachments_index, feedback_comments_index),
            encoding="utf-8",
        )

    _log(f"  submissions: {s_stats['assignments']} assignment(s), "
         f"{s_stats['my_files']} my-file(s), {s_stats['feedback_files']} feedback-file(s) — "
         f"{s_stats['downloaded']} dl / {s_stats['skipped']} skip / {s_stats['failed']} fail", logfh)

    for k in s_stats:
        sub_stats[k] += s_stats[k]


def _render_feedback_md(cname, asg, sub, my_atts, comments):
    lines = [f"# {asg.get('name') or 'Submission'}", ""]
    lines.append(f"- **Course:** {cname}")
    lines.append(f"- **Assignment:** {asg.get('html_url') or ''}")
    lines.append(f"- **Points possible:** {asg.get('points_possible')}")
    lines.append(f"- **Due:** {asg.get('due_at') or '—'}")
    lines.append("")

    lines.append("## My submission")
    lines.append("")
    lines.append(f"- Submitted at: {sub.get('submitted_at') or '—'}")
    lines.append(f"- State: `{sub.get('workflow_state')}`, attempt {sub.get('attempt')}")
    lines.append(f"- Late: {sub.get('late')}   Missing: {sub.get('missing')}   Excused: {sub.get('excused')}")
    if my_atts:
        lines.append("")
        lines.append("Files:")
        for a in my_atts:
            size_mb = (a.get("size") or 0) / 1024 / 1024
            lines.append(f"- `my/{a['stored']}` — {a.get('display') or a['stored']} ({size_mb:.2f} MB)")
    body = sub.get("body")
    if body:
        lines.append("")
        lines.append("### Text body")
        lines.append("")
        lines.append(html_to_md(body))
    lines.append("")

    lines.append("## Grade")
    lines.append("")
    lines.append(f"- Score: **{sub.get('score')}** / {asg.get('points_possible')}")
    lines.append(f"- Grade string: `{sub.get('grade')}`")
    lines.append(f"- Graded at: {sub.get('graded_at') or '—'}")
    lines.append("")

    rubric = asg.get("rubric") or []
    ra = sub.get("rubric_assessment") or {}
    if rubric and ra:
        lines.append("## Rubric")
        lines.append("")
        for crit in rubric:
            ckey = crit.get("id")
            got = ra.get(ckey, {}) if isinstance(ra, dict) else {}
            pts = got.get("points")
            max_pts = crit.get("points")
            lines.append(f"### {crit.get('description') or ckey} — "
                         f"{pts if pts is not None else '—'} / {max_pts}")
            if got.get("comments"):
                lines.append("")
                for cline in str(got["comments"]).splitlines():
                    lines.append(f"> {cline}")
            lines.append("")

    if comments:
        lines.append("## Feedback comments")
        lines.append("")
        for co in comments:
            lines.append(f"### {co['author']} — {co.get('created_at') or ''}")
            lines.append("")
            text = co.get("comment")
            if text:
                for t in str(text).splitlines():
                    lines.append(t)
                lines.append("")
            for att in co.get("attachments") or []:
                size_mb = (att.get("size") or 0) / 1024 / 1024
                label = att.get("display") or att["stored"]
                lines.append(f"- 📎 [`feedback/{att['stored']}`](feedback/{att['stored']}) — "
                             f"{label} ({size_mb:.2f} MB)")
            lines.append("")
    return "\n".join(lines)


def fetch_assignment_attachments_for_course(request, course, cdir, aa_stats, logfh):
    assignments = load_json(cdir / "assignments.json", [])
    if not assignments:
        return
    files_meta = load_json(cdir / "files.json", [])
    known_ids: set[int] = {f.get("id") for f in files_meta if f.get("id")}
    descs = (a.get("description") for a in assignments)
    s = canvas_attachments.fetch_assignment_attachments(
        request, CANVAS_HOST, course["id"], descs,
        dest_dir=cdir / "files" / "_assignment_attachments",
        known_file_ids=known_ids,
        user_agent=USER_AGENT,
        logger=lambda m: _log(m, logfh),
    )
    for k in aa_stats:
        aa_stats[k] += s[k]


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


def process_course(request, course, agg, logfh, files_bulk=False,
                   dl_stats=None, sub_stats=None, aa_stats=None):
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
    n_files, files_bytes    = sync_files_listing(request, course, cdir, logfh,
                                                  files_bulk=files_bulk,
                                                  dl_stats=dl_stats)
    counts["files"]         = n_files
    counts["files_bytes"]   = files_bytes
    modules_data = load_json(cdir / "modules.json", [])
    counts["lti"]           = sync_lti_external_skipped(tabs, modules_data, cdir, logfh)

    if sub_stats is not None:
        sync_submissions(request, course, cdir, sub_stats, logfh)
    if aa_stats is not None:
        fetch_assignment_attachments_for_course(request, course, cdir, aa_stats, logfh)

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
    parser = argparse.ArgumentParser(description="Sync USF Canvas content to local mirror")
    parser.add_argument("--log", help="Write progress log to this file in addition to stdout.")
    parser.add_argument("--course-id", type=int, default=None,
                        help="Only sync this course id (debug / single-course re-run)")
    parser.add_argument("--files-bulk", action="store_true",
                        help="Also download every non-locked course file under <course>/files/. "
                             "Resumes via on-disk size dedupe.")
    opts = parser.parse_args()

    MIRROR_ROOT.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logfh = open(opts.log, "a", encoding="utf-8") if opts.log else None
    if logfh:
        _log(f"=== canvas_sync_usf.py run started {datetime.datetime.now().isoformat()} "
             f"(files_bulk={opts.files_bulk}) ===", logfh)

    token = _read_token()

    agg = {
        "courses_ok": 0, "courses_fail": 0,
        "announcements": 0, "assignments": 0, "modules": 0,
        "pages": 0, "files": 0, "files_bytes": 0, "lti": 0,
        "auth_errors": [],
    }
    dl_stats = {"downloaded": 0, "skipped": 0, "failed": 0, "locked": 0,
                "bytes": 0, "courses": {}, "large_files": []}
    sub_stats = {"assignments": 0, "my_files": 0, "feedback_files": 0,
                 "downloaded": 0, "skipped": 0, "failed": 0}
    aa_stats = {"discovered": 0, "new": 0, "downloaded": 0,
                "skipped": 0, "failed": 0, "bytes": 0}

    with sync_playwright() as p:
        request = p.request.new_context(
            extra_http_headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )

        try:
            me = canvas_auth.preflight(request, CANVAS_HOST, scope="usf")
        except canvas_auth.AuthError as e:
            _log(f"[auth-fail] {e}", logfh)
            agg["auth_errors"].append(str(e))
            request.dispose()
            if logfh:
                logfh.close()
            sys.exit(3)
        _log(f"[auth] {me.name} <{me.email}>  (id={me.user_id})", logfh)

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
                process_course(request, course, agg, logfh,
                               files_bulk=opts.files_bulk, dl_stats=dl_stats,
                               sub_stats=sub_stats, aa_stats=aa_stats)
            except canvas_auth.AuthError as e:
                _log(f"  [auth-fail mid-run] {course_label(course)}: {e}", logfh)
                agg["auth_errors"].append(str(e))
                break
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
    if opts.files_bulk:
        gb = dl_stats["bytes"] / (1024 ** 3)
        _log(f"  Files (bulk):  {dl_stats['downloaded']} dl / {dl_stats['skipped']} skip / "
             f"{dl_stats['failed']} fail / {dl_stats['locked']} locked  ({gb:.2f} GB on disk)", logfh)
    else:
        _log(f"  File records:  {agg['files']}  ({agg['files_bytes'] / (1024 * 1024):.1f} MB — not downloaded)", logfh)
    _log(f"  LTI externals: {agg['lti']}", logfh)
    if sub_stats["assignments"]:
        _log(f"  Submissions:   {sub_stats['assignments']} assignment(s) — "
             f"{sub_stats['my_files']} my-file(s), {sub_stats['feedback_files']} feedback-file(s) "
             f"({sub_stats['downloaded']} dl / {sub_stats['skipped']} skip / {sub_stats['failed']} fail)", logfh)
    if aa_stats["discovered"]:
        _log(f"  Asg-attachments: {aa_stats['discovered']} ref(s), {aa_stats['new']} new — "
             f"{aa_stats['downloaded']} dl / {aa_stats['skipped']} skip / {aa_stats['failed']} fail "
             f"({aa_stats['bytes'] / (1024 * 1024):.1f} MB)", logfh)
    if agg["auth_errors"]:
        _log(f"  Auth errors: {len(agg['auth_errors'])}", logfh)
        for e in agg["auth_errors"]:
            _log(f"    {e}", logfh)

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    save_json(LOGS_DIR / f"usf-summary-{ts}.json", {
        **agg,
        "downloads": dl_stats if opts.files_bulk else None,
        "submissions": sub_stats,
        "assignment_attachments": aa_stats,
    })

    if logfh:
        _log(f"=== canvas_sync_usf.py run ended {datetime.datetime.now().isoformat()} ===", logfh)
        logfh.close()


if __name__ == "__main__":
    main()
