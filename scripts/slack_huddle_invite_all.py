#!/usr/bin/env python3
"""
slack_huddle_invite_all.py — Playwright (Python) port of slack_huddle_invite_all.js (Puppeteer)

Flow:
  1) Open Slack channel (persistent browser profile)
  2) Click headset (huddle)
  3) Scroll to latest / bottom (virtualized list)
  4) Find & click "invite someone" (button.c-link--button)
  5) Modal: @channel row
  6) Wait Send Invite enabled → click

Requires: pip install playwright
  Then either: playwright install chromium
  Or set CHROME_PATH to system Chromium/Chrome.

Env (see env.example):
  SESSION_DIR, SLACK_CHANNEL_URL (required)
  SLACK_HEADLESS=1 or HEADLESS=1 (server; default off for local debugging)
  CHROME_PATH, SCREENSHOT_DIR, HARD_TIMEOUT_MS (optional)

Run:
  export SESSION_DIR=/path/to/slack_profile
  export SLACK_CHANNEL_URL='https://app.slack.com/client/T.../C...'
  python scripts/slack_huddle_invite_all.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Page, async_playwright

# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------

HARD_TIMEOUT_MS = int(os.getenv("HARD_TIMEOUT_MS", "180000"))
CHROME_PATH = (os.getenv("CHROME_PATH") or "").strip() or None
SESSION_DIR = (os.getenv("SESSION_DIR") or "").strip()
SLACK_CHANNEL_URL = (os.getenv("SLACK_CHANNEL_URL") or "").strip()


def _env_truthy(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# Server / bot integration sets SLACK_HEADLESS=1; local debugging leaves default off.
SLACK_HEADLESS = _env_truthy("SLACK_HEADLESS") or _env_truthy("HEADLESS")
SCREENSHOT_DIR = Path(
    os.getenv("SCREENSHOT_DIR") or Path(__file__).resolve().parent / "screenshots"
)

# Filled after page exists — used for debug snapshot on asyncio hard-timeout
_LAST_PAGE: list[Optional[Page]] = [None]


def _ts_local() -> str:
    t = time.localtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}_{t.tm_hour:02d}-{t.tm_min:02d}-{t.tm_sec:02d}"


def _ensure_screenshot_dir() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


async def snap_on_error(page: Optional[Page], name: str = "failure_state") -> None:
    """Screenshot + URL + HTML for scp debugging."""
    if not page:
        return
    try:
        _ensure_screenshot_dir()
        ts = _ts_local()
        base = SCREENSHOT_DIR / f"{ts}_{name}"
        png = base.with_suffix(".png")
        urlf = base.with_suffix(".url.txt")
        htmlf = base.with_suffix(".html")
        try:
            await page.screenshot(path=str(png), full_page=True)
            print(f"[ERROR] screenshot saved: {png}", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] screenshot failed: {e}", file=sys.stderr)
        try:
            urlf.write_text(page.url + "\n", encoding="utf-8")
            print(f"[ERROR] url saved: {urlf}", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] url dump failed: {e}", file=sys.stderr)
        try:
            htmlf.write_text(await page.content(), encoding="utf-8")
            print(f"[ERROR] html saved: {htmlf}", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] html dump failed: {e}", file=sys.stderr)
    except Exception as outer:
        print(f"[ERROR] snap_on_error outer: {outer}", file=sys.stderr)


def dump_launch_error(name: str, err: BaseException) -> None:
    import traceback

    try:
        _ensure_screenshot_dir()
        ts = _ts_local()
        file = SCREENSHOT_DIR / f"{ts}_{name}.launch_error.txt"
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        msg = (
            "===== BROWSER LAUNCH FAILED =====\n"
            f"Time: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
            f"CHROME_PATH: {CHROME_PATH}\n"
            f"SESSION_DIR: {SESSION_DIR}\n"
            f"SLACK_CHANNEL_URL: {SLACK_CHANNEL_URL}\n\n"
            f"Error:\n{tb}\n"
        )
        file.write_text(msg, encoding="utf-8")
        print(f"[ERROR] launch error report saved: {file}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] dumpLaunchError failed: {e}", file=sys.stderr)


async def sleep_ms(ms: float) -> None:
    await asyncio.sleep(ms / 1000.0)


async def press_esc_a_few_times(page: Page, times: int = 3) -> None:
    for _ in range(times):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await sleep_ms(120)


async def close_right_flexpane(page: Page) -> None:
    try:
        for _ in range(4):
            did = await page.evaluate(
                """() => {
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) return false;
    const s = window.getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
    return true;
  };
  const panes = [
    document.querySelector('[data-qa="flexpane"]'),
    document.querySelector('[data-qa="rhs_container"]'),
    document.querySelector("div.p-flexpane"),
    document.querySelector('aside[aria-label="Thread"]'),
  ].filter(Boolean);
  const pane = panes.find(isVisible);
  if (!pane) return false;
  const closeSelectors = [
    '[data-qa="close_flexpane"]',
    'button[aria-label="Close"]',
    'button[aria-label="Close panel"]',
    'button[aria-label="Close details"]',
    'button[aria-label="Close thread"]',
    'button.c-icon_button[aria-label="Close"]',
    'button[aria-label="Close"][type="button"]',
  ];
  for (const sel of closeSelectors) {
    const btn = pane.querySelector(sel);
    if (btn && isVisible(btn)) { btn.click(); return true; }
  }
  const maybeBtns = Array.from(pane.querySelectorAll("button"));
  const xBtn = maybeBtns.find((b) => {
    if (!isVisible(b)) return false;
    const a = (b.getAttribute("aria-label") || "").toLowerCase();
    const t = (b.innerText || "").trim().toLowerCase();
    return a.includes("close") || t === "×" || t === "x";
  });
  if (xBtn) { xBtn.click(); return true; }
  return false;
}"""
            )
            await press_esc_a_few_times(page, 1)
            await sleep_ms(180)
            if did:
                await sleep_ms(250)
            still_visible = await page.evaluate(
                """() => {
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) return false;
    const s = window.getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
    return true;
  };
  const pane =
    document.querySelector('[data-qa="flexpane"]') ||
    document.querySelector('[data-qa="rhs_container"]') ||
    document.querySelector("div.p-flexpane") ||
    document.querySelector('aside[aria-label="Thread"]');
  return isVisible(pane);
}"""
            )
            if not still_visible:
                return
    except Exception:
        pass


async def clear_obstructions(page: Page) -> None:
    await press_esc_a_few_times(page, 2)
    await close_right_flexpane(page)
    await press_esc_a_few_times(page, 1)


async def _require_slack_logged_in(page: Page) -> None:
    """Fail fast if persistent profile has no session (Slack shows workspace sign-in)."""
    u = (page.url or "").lower()
    if "workspace-signin" in u:
        raise RuntimeError(
            "Slack is NOT logged in in this Chromium profile (got workspace-signin). "
            f"Log in once using SESSION_DIR={SESSION_DIR!r}: "
            "unset SLACK_HEADLESS, set DISPLAY if headless server, run "
            "`python3 scripts/slack_open_login_browser.py` (or VNC + same script). "
            "Then re-run this automation."
        )


async def wait_for_slack_loaded(page: Page, timeout_ms: int = 120000) -> None:
    ready_sel = ",".join(
        [
            '[data-qa="channel_name"]',
            '[data-qa="message_input"]',
            '[role="textbox"][contenteditable="true"]',
            '[aria-label*="Search"]',
            '[data-qa="slack_sidebar"]',
            '[data-qa="channel_view"]',
            '[data-qa="message-pane"]',
            "div.p-workspace",
            "div.p-client_container",
        ]
    )
    start = time.time()
    for attempt in range(1, 3):
        try:
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            while (time.time() - start) * 1000 < timeout_ms:
                try:
                    await page.wait_for_selector(ready_sel, timeout=5000)
                    return
                except Exception as e:
                    msg = str(e)
                    if "detached" in msg or "Execution context was destroyed" in msg:
                        await sleep_ms(350)
                        continue
                    await sleep_ms(350)
            raise TimeoutError(f"wait_for_slack_loaded timeout after {timeout_ms}ms")
        except Exception as e:
            msg = str(e)
            if attempt < 2 and (
                "detached" in msg or "Execution context was destroyed" in msg
            ):
                print(
                    "wait_for_slack_loaded: frame detach; retrying once...",
                    file=sys.stderr,
                )
                await sleep_ms(800)
                continue
            print(f"ERROR: wait_for_slack_loaded timeout. URL: {page.url}", file=sys.stderr)
            await snap_on_error(page, "slack_load_fail")
            raise


async def wait_for_huddle_control(page: Page, timeout_ms: int = 60000) -> None:
    await page.wait_for_function(
        """() => {
  const svgBtn = document.querySelector('svg[data-qa="headphones"]')?.closest("button");
  if (svgBtn) return true;
  const qaBtn = document.querySelector('button[data-qa="huddle_channel_header_button_on_start_button"]');
  if (qaBtn) return true;
  const ariaBtn =
    document.querySelector('button[aria-label*="Huddle"]') ||
    document.querySelector('button[aria-label*="Start huddle"]') ||
    document.querySelector('button[aria-label*="Join huddle"]') ||
    document.querySelector('button[aria-label*="huddle"]');
  return !!ariaBtn;
}""",
        timeout=timeout_ms,
    )


async def click_huddle_logo(page: Page) -> bool:
    await wait_for_huddle_control(page, 60000)
    ok = await page.evaluate(
        """() => {
  const svg = document.querySelector('svg[data-qa="headphones"]');
  const btn1 = svg?.closest("button");
  if (btn1) { btn1.click(); return true; }
  const btn2 = document.querySelector('button[data-qa="huddle_channel_header_button_on_start_button"]');
  if (btn2) { btn2.click(); return true; }
  const btn3 =
    document.querySelector('button[aria-label*="Huddle"]') ||
    document.querySelector('button[aria-label*="Start huddle"]') ||
    document.querySelector('button[aria-label*="Join huddle"]') ||
    document.querySelector('button[aria-label*="huddle"]');
  if (btn3) { btn3.click(); return true; }
  return false;
}"""
    )
    if not ok:
        return False
    await sleep_ms(350)
    return True


async def click_jump_to_latest_if_exists(page: Page) -> None:
    await page.evaluate(
        """() => {
  const sels = [
    'button[data-qa="slack_kit_list_jump_to_present"]',
    'button[aria-label*="Latest messages"]',
    'button[aria-label*="Jump to present"]',
    'button[aria-label*="New messages"]',
    'button[aria-label*="Jump to newest"]',
  ];
  for (const sel of sels) {
    const b = document.querySelector(sel);
    if (b) { b.click(); return; }
  }
  const btns = Array.from(document.querySelectorAll("button"));
  const pill = btns.find((b) => (b.innerText || "").trim().toLowerCase().includes("latest messages"));
  if (pill) pill.click();
}"""
    )


async def get_message_scroll_container(page: Page) -> Any:
    selectors = [
        "div.c-virtual_list__scroll_container",
        'div[role="list"][aria-label*="channel"]',
        'div[aria-label*="channel"][role="list"]',
        '[data-qa="message_list"]',
    ]
    for sel in selectors:
        h = await page.query_selector(sel)
        if not h:
            continue
        ok = await h.evaluate(
            """(el) => {
  const sh = el.scrollHeight || 0;
  const ch = el.clientHeight || 0;
  return sh > ch + 50;
}"""
        )
        if ok:
            return h
    return None


async def scroll_channel_to_bottom(page: Page, rounds: int = 6) -> None:
    await clear_obstructions(page)
    await click_jump_to_latest_if_exists(page)
    await sleep_ms(180)
    scroller = await get_message_scroll_container(page)
    for _ in range(rounds):
        await clear_obstructions(page)
        if scroller:
            await scroller.evaluate(
                """(el) => { el.scrollTop = el.scrollHeight; }"""
            )
        await page.mouse.wheel(0, 5000)
        await sleep_ms(120)


async def wait_invite_someone_visible(page: Page, timeout_ms: int = 120000) -> None:
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        await clear_obstructions(page)
        await scroll_channel_to_bottom(page, 2)
        found = await page.evaluate(
            """() => {
  const btns = Array.from(document.querySelectorAll("button.c-link--button"));
  return btns.some((b) => (b.innerText || "").trim().toLowerCase().includes("invite someone"));
}"""
        )
        if found:
            return
        await sleep_ms(350)
    raise TimeoutError(
        'Timeout waiting for "invite someone" to appear (likely not rendered in DOM).'
    )


async def click_invite_someone_robust(page: Page) -> None:
    for _attempt in range(8):
        await clear_obstructions(page)
        await scroll_channel_to_bottom(page, 2)
        ok = await page.evaluate(
            """() => {
  const btns = Array.from(document.querySelectorAll("button.c-link--button"));
  const btn = btns.find((b) => (b.innerText || "").trim().toLowerCase().includes("invite someone")) || null;
  if (!btn) return false;
  btn.scrollIntoView({ block: "center", inline: "center" });
  btn.click();
  return true;
}"""
        )
        if ok:
            return
        await sleep_ms(300)
    raise RuntimeError('Cannot click "invite someone" after multiple attempts.')


async def wait_invite_modal(page: Page, timeout_ms: int = 45000) -> None:
    await page.wait_for_selector("div.ReactModal__Content", timeout=timeout_ms)


async def click_channel_row(page: Page) -> None:
    await page.wait_for_selector("div.ReactModal__Content", timeout=45000)
    await page.evaluate(
        """() => {
  const tabs = Array.from(document.querySelectorAll('[role="tab"],button[role="tab"]'));
  const suggested = tabs.find((t) => (t.innerText || "").toLowerCase().includes("suggested"));
  if (suggested) suggested.click();
}"""
    )
    await sleep_ms(250)
    await page.wait_for_selector("div.p-huddle_invite_channel_list_entity", timeout=45000)
    clicked = await page.evaluate(
        """() => {
  const rows = Array.from(document.querySelectorAll("div.p-huddle_invite_channel_list_entity"));
  const row = rows.find((r) => (r.innerText || "").toLowerCase().includes("@channel"));
  if (!row) return false;
  row.scrollIntoView({ block: "center", inline: "center" });
  row.click();
  return true;
}"""
    )
    if not clicked:
        raise RuntimeError('Cannot find/click "@channel" row.')
    await sleep_ms(350)


async def wait_send_invite_enabled(page: Page, timeout_ms: int = 45000) -> None:
    await page.wait_for_selector('button[data-qa="send_invite_button"]', timeout=timeout_ms)
    await page.wait_for_function(
        """() => {
  const btn = document.querySelector('button[data-qa="send_invite_button"]');
  if (!btn) return false;
  const ariaDisabled = btn.getAttribute("aria-disabled") === "true";
  return !btn.disabled && !ariaDisabled;
}""",
        timeout=timeout_ms,
    )


async def click_send_invite(page: Page) -> None:
    await wait_send_invite_enabled(page, 45000)
    ok = await page.evaluate(
        """() => {
  const btn = document.querySelector('button[data-qa="send_invite_button"]');
  if (!btn) return false;
  if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return false;
  btn.scrollIntoView({ block: "center", inline: "center" });
  btn.click();
  return true;
}"""
    )
    if not ok:
        raise RuntimeError('Cannot click "Send Invite".')
    await sleep_ms(500)


def _launch_args() -> list[str]:
    return [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--window-size=1366,768",
        "--disable-features=UseOzonePlatform",
        "--disable-notifications",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--password-store=basic",
        "--use-mock-keychain",
    ]


async def _route_block_heavy(route) -> None:
    t = route.request.resource_type
    if t in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


async def run_flow() -> None:
    if not SESSION_DIR:
        print("ERROR: SESSION_DIR env is required.", file=sys.stderr)
        sys.exit(2)
    if not SLACK_CHANNEL_URL:
        print("ERROR: SLACK_CHANNEL_URL env is required.", file=sys.stderr)
        sys.exit(2)

    Path(SESSION_DIR).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        try:
            launch_kw: dict[str, Any] = {
                "user_data_dir": SESSION_DIR,
                "headless": SLACK_HEADLESS,
                "viewport": {"width": 1366, "height": 768},
                "args": _launch_args(),
            }
            if CHROME_PATH:
                launch_kw["executable_path"] = CHROME_PATH

            context = await p.chromium.launch_persistent_context(**launch_kw)
        except Exception as e:
            print(f"ERROR: Failed to launch browser: {e}", file=sys.stderr)
            dump_launch_error("browser_launch_failed", e)
            sys.exit(2)

        page: Optional[Page] = None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            assert page is not None
            _LAST_PAGE[0] = page
            await page.route("**/*", _route_block_heavy)

            print(f"Opening channel: {SLACK_CHANNEL_URL}")
            await page.goto(SLACK_CHANNEL_URL, wait_until="domcontentloaded")
            await _require_slack_logged_in(page)
            await wait_for_slack_loaded(page)
            await clear_obstructions(page)

            print("Clicking headset logo (only)...")
            h_ok = await click_huddle_logo(page)
            if not h_ok:
                raise RuntimeError("Cannot find/click headset logo.")
            await clear_obstructions(page)

            print('Waiting for "invite someone"... (scroll-safe)')
            await wait_invite_someone_visible(page)

            print('Clicking "invite someone"... (robust DOM click)')
            await click_invite_someone_robust(page)

            print("Waiting invite modal...")
            await wait_invite_modal(page)

            print('Clicking "@channel"...')
            await click_channel_row(page)

            print('Clicking "Send Invite"...')
            await click_send_invite(page)

            print(
                "✅ DONE: headset -> invite someone -> @channel -> Send Invite (clicked)",
                flush=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            if page:
                await snap_on_error(page, "failure_state")
            raise
        finally:
            try:
                await context.close()
            except Exception:
                pass


async def main_async() -> None:
    try:
        await asyncio.wait_for(run_flow(), timeout=HARD_TIMEOUT_MS / 1000.0)
    except asyncio.TimeoutError:
        print(f"HARD TIMEOUT ({HARD_TIMEOUT_MS}ms).", file=sys.stderr)
        p = _LAST_PAGE[0]
        if p:
            await snap_on_error(p, "hard_timeout")
        else:
            _ensure_screenshot_dir()
            ts = _ts_local()
            f = SCREENSHOT_DIR / f"{ts}_hard_timeout_no_page.txt"
            f.write_text("Hard timeout before page was created.\n", encoding="utf-8")
            print(f"[ERROR] hard timeout note: {f}", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
