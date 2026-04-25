"""USF Canvas SSO login -> storage_state.json.

Runs on a machine with a display (pad). Opens a real browser at the USF
Canvas dashboard URL, waits for you to complete the SSO + Duo MFA dance
manually, detects the post-login dashboard, and dumps the Playwright
storage_state to ~/.config/credentials/usfca_state.json.

Why interactive instead of scripted: USF SSO routes through Microsoft
365 / Duo Push, which doesn't automate cleanly. After you log in once,
the storage state lasts long enough that joppa-side scripts (Panopto
downloader, etc.) can drive an authenticated browser with no further
input until the cookies expire.

Usage (on pad):

    cd ~/jon-claude-grand-ham/projects/canvas-sync-usf
    .venv/bin/python usf_login.py
    # log in interactively in the browser window that opens
    # script saves usfca_state.json and exits

Then on joppa:

    scp pad:.config/credentials/usfca_state.json \
        /home/joppa/.config/credentials/usfca_state.json
    chmod 600 /home/joppa/.config/credentials/usfca_state.json

The state file is .stignore'd so Syncthing won't sync it through jcgh.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CANVAS_URL = "https://usfca.instructure.com/"
STATE_FILE = Path.home() / ".config" / "credentials" / "usfca_state.json"
SCREENSHOT = Path(__file__).parent / "usf-dashboard.png"

# Selectors / URL fragments that indicate we landed on the Canvas dashboard
DASHBOARD_SELECTORS = [
    "#dashboard",
    "[aria-label='Global Navigation']",
    ".ic-app-header",
    "div.ic-Dashboard-header__title",
]


def _is_dashboard_url(url: str) -> bool:
    return "usfca.instructure.com" in url and "/login" not in url.lower()


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"[info] target: {CANVAS_URL}")
    print(f"[info] state file destination: {STATE_FILE}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()

        print("[step] navigating to USF Canvas...")
        page.goto(CANVAS_URL, wait_until="domcontentloaded", timeout=30000)
        print(f"[info] landed at: {page.url}")

        print()
        print("[ACTION REQUIRED]")
        print("  Complete the USF SSO login (Microsoft 365 / Duo Push) in the")
        print("  browser window. Script will detect the dashboard and save state.")
        print(f"  Waiting up to 5 minutes...")
        print()

        success = False
        try:
            page.wait_for_url(_is_dashboard_url, timeout=300000)
            success = True
        except PWTimeout:
            print("[warn] URL wait timed out; checking DOM as fallback...")
            for sel in DASHBOARD_SELECTORS:
                try:
                    page.wait_for_selector(sel, timeout=5000)
                    if _is_dashboard_url(page.url):
                        success = True
                        break
                except PWTimeout:
                    continue

        try:
            title = page.title()
        except Exception:
            title = "(unavailable)"
        print()
        print("=== RESULT ===")
        print(f"final url: {page.url}")
        print(f"page title: {title}")
        print(f"login success: {success}")

        try:
            page.screenshot(path=str(SCREENSHOT), full_page=True)
            print(f"[info] screenshot saved to {SCREENSHOT.name}")
        except Exception as e:
            print(f"[warn] screenshot failed: {e}")

        if success:
            try:
                ctx.storage_state(path=str(STATE_FILE))
                STATE_FILE.chmod(0o600)
                print(f"[info] storage state saved to {STATE_FILE}")
            except Exception as e:
                print(f"[warn] storage state save failed: {e}")
        else:
            print("[fail] not saving state — login wasn't detected")

        print("[info] keeping browser open 20s for visual verification...")
        page.wait_for_timeout(20000)
        browser.close()

        sys.exit(0 if success else 2)


if __name__ == "__main__":
    main()
