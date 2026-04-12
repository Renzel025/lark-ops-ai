#!/usr/bin/env python3
"""
slack_huddle_invite_all.py — Slack Huddle → Start Huddle → invite @channel (Playwright)

Automation flow (what the script does):
  1) Open Slack channel (persistent profile)
  2) Click headset → Huddle Preview (pop-out or new tab)
  3) Click **Start Huddle** in that UI
  4) Scroll; click **invite someone** → modal → **@channel** → **Send Invite**

SPEC — setup before running (order matters; full text in env.example):
  A) In the SAME venv you use to run this script:
       python -m pip install -r scripts/requirements-huddle.txt
     You must have playwright-stealth; script exits early if missing (unless SLACK_ALLOW_NO_STEALTH=1).
  B) Browser: ``playwright install chromium`` OR set CHROME_PATH to system Chrome (``which google-chrome``
     or ``which google-chrome-stable`` — either path is fine; match UA to that binary).
  C) Log in once: ``python scripts/slack_open_login_browser.py`` with the same SESSION_DIR (+ CHROME_PATH if used).
  D) Match SLACK_CHROME_USER_AGENT to your real Chrome major version if CHROME_PATH is set.
  E) On VNC: do NOT set SLACK_HEADLESS / HEADLESS; set DISPLAY (e.g. :1).

Required env: SESSION_DIR, SLACK_CHANNEL_URL

Common optional: CHROME_PATH, SLACK_CHROME_USER_AGENT, DISPLAY, HARD_TIMEOUT_MS, SCREENSHOT_DIR

Tuning (only if needed):
  SLACK_HEADLESS=1 — server without display (huddle UI often worse than headed/VNC)
  SLACK_CHROME_DISABLE_GPU=1 — force GPU off even when headed (can cause white windows on VNC; default keeps GPU on headed)
  SLACK_CHROME_OMIT_DISABLE_SETUID_SANDBOX=1 — omit --disable-setuid-sandbox (non-root experiments; default keeps it for root/Docker)
  SLACK_HUDDLE_EVAL_CLICK=1 — use JS .click() instead of Playwright click (worse user-activation; debug only)
  SLACK_HUDDLE_CLOSE_BLANK_POPUP=1 — legacy: close stuck about:blank pop-out (default off; can kill Preview too early)
  SLACK_PLAYWRIGHT_STEALTH_DISABLE / SLACK_ALLOW_NO_STEALTH — skip stealth (not recommended)

Run:
  export SESSION_DIR=/path/to/slack_profile
  export SLACK_CHANNEL_URL='https://app.slack.com/client/T.../C...'
  python scripts/slack_huddle_invite_all.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from slack_chrome_shared import default_slack_chrome_user_agent_string as _default_slack_user_agent
from slack_chrome_shared import env_truthy as _env_truthy
from slack_chrome_shared import slack_chrome_launch_args as _launch_args

# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------

HARD_TIMEOUT_MS = int(os.getenv("HARD_TIMEOUT_MS", "180000"))
CHROME_PATH = (os.getenv("CHROME_PATH") or "").strip() or None
SESSION_DIR = (os.getenv("SESSION_DIR") or "").strip()
SLACK_CHANNEL_URL = (os.getenv("SLACK_CHANNEL_URL") or "").strip()
# Optional: comma-separated substrings (lowercase) to match invite CTA, e.g. non-English Slack UI.
_SLACK_INVITE_MATCH_EXTRA = [
    x.strip().lower()
    for x in (os.getenv("SLACK_INVITE_MATCH_EXTRA") or "").split(",")
    if x.strip()
]

def _slack_user_agent_for_launch() -> Optional[str]:
    """HTTP User-Agent for persistent context; None = leave Playwright default."""
    if _env_truthy("SLACK_CHROME_USER_AGENT_DISABLE"):
        return None
    ua = (os.getenv("SLACK_CHROME_USER_AGENT") or "").strip()
    return ua if ua else _default_slack_user_agent()


def _js_invite_match_snippet() -> str:
    """Shared JS: `match(el)` + `gatherCandidates()` for huddle invite CTA (not workspace invite)."""
    ex = json.dumps(_SLACK_INVITE_MATCH_EXTRA)
    return f"""
  const extra = {ex};
  function match(el) {{
    const t = (el.innerText || el.textContent || "").trim().toLowerCase();
    const a = (el.getAttribute("aria-label") || "").toLowerCase();
    const qa = (el.getAttribute("data-qa") || "").toLowerCase();
    const combined = t + " " + a + " " + qa;
    if (t.length > 180) return false;
    if (combined.includes("invite someone")) return true;
    if (combined.includes("invite people")) return true;
    if (t.includes("invite") && (t.includes("someone") || t.includes("people"))) return true;
    if (a.includes("invite") && (a.includes("someone") || a.includes("people") || a.includes("huddle"))) return true;
    if (combined.includes("邀请")) return true;
    for (const s of extra) {{ if (s && combined.includes(s)) return true; }}
    return false;
  }}
  function gatherCandidates() {{
    const raw = Array.from(document.querySelectorAll(
      'button, a[role="button"], [role="button"], ' +
      'button.c-link--button, a.c-link--button, ' +
      '[data-qa*="invite"], [data-qa*="huddle_invite"]'
    ));
    return raw.filter((el) => {{
      const t = (el.innerText || el.textContent || "").trim();
      return t.length > 0 && t.length < 200;
    }});
  }}"""


def _js_invite_button_visible() -> str:
    inner = _js_invite_match_snippet()
    return f"""() => {{{inner}
  return gatherCandidates().some(match);
}}"""


def _js_invite_button_click() -> str:
    inner = _js_invite_match_snippet()
    return f"""() => {{{inner}
  let el = gatherCandidates().find(match);
  if (!el) return false;
  const clickable = el.closest("button, a[role='button'], [role='button']") || el;
  clickable.scrollIntoView({{ block: "center", inline: "center" }});
  clickable.click();
  return true;
}}"""


def _enforce_playwright_stealth_installed() -> None:
    """
    Slack Huddle preview often never leaves about:blank if automation is obvious.
    Your terminal showed: playwright-stealth not installed — install it in THIS Python venv.
    """
    if _env_truthy("SLACK_PLAYWRIGHT_STEALTH_DISABLE"):
        return
    if _env_truthy("SLACK_ALLOW_NO_STEALTH"):
        print(
            "WARN: SLACK_ALLOW_NO_STEALTH=1 — running without playwright-stealth "
            "(Huddle may stay white).",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        import playwright_stealth  # noqa: F401
    except ImportError:
        print(
            "\n"
            + "=" * 72
            + "\n"
            "FATAL: playwright-stealth is NOT installed in this Python environment.\n"
            "Slack Huddle pop-out commonly stays about:blank without anti-detection hooks.\n\n"
            "  python3 -m pip install 'playwright-stealth>=2.0.3'\n"
            "  # or from repo root:\n"
            "  python3 -m pip install -r p0_logic/requirements.txt\n"
            "  python3 -m pip install -r scripts/requirements-huddle.txt\n\n"
            "Then re-run. To force-run without stealth (not recommended):\n"
            "  SLACK_ALLOW_NO_STEALTH=1  or  SLACK_PLAYWRIGHT_STEALTH_DISABLE=1\n"
            + "=" * 72
            + "\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)


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


async def _click_huddle_button_playwright(page: Page) -> None:
    """
    Real Playwright clicks count as user activation in Chromium: window.open + getUserMedia
    in the huddle pop-out often fail or stay about:blank when using evaluate(() => btn.click()).
    """
    primary = page.locator('button:has(svg[data-qa="headphones"])').first
    if await primary.count() > 0:
        await primary.click(timeout=20000)
        return
    qa = page.locator(
        '[data-qa="huddle_channel_header_button_on_start_button"]'
    ).first
    if await qa.count() > 0:
        await qa.click(timeout=20000)
        return
    aria = page.locator(
        'button[aria-label*="Huddle"], button[aria-label*="huddle"], '
        'button[aria-label*="Start huddle"], button[aria-label*="Join huddle"]'
    ).first
    if await aria.count() > 0:
        await aria.click(timeout=20000)
        return
    raise RuntimeError("Cannot find/click headset (Playwright click).")


async def click_huddle_open_popup(
    page: Page, context: BrowserContext
) -> Optional[Page]:
    """
    Click the channel huddle control and return the **new window** if Slack uses window.open,
    else discover a new tab. Uses expect_popup so the huddle window is tied to the click.
    """
    await wait_for_huddle_control(page, 60000)
    prev = list(context.pages)
    click_js = """() => {
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

    async def _do_click() -> None:
        if _env_truthy("SLACK_HUDDLE_EVAL_CLICK"):
            ok = await page.evaluate(click_js)
            if not ok:
                raise RuntimeError("Cannot find/click headset logo.")
        else:
            await _click_huddle_button_playwright(page)

    try:
        # Short wait: many workspaces open huddle in a new tab (no window.open) — fail fast.
        async with page.expect_popup(timeout=25000) as popup_info:
            await _do_click()
        return await popup_info.value
    except Exception as e:
        print(
            f"expect_popup: {e!r} (will try new-tab / same-window discovery)",
            flush=True,
        )
    await sleep_ms(350)
    return await _discover_new_page(context, prev, timeout_sec=45)


async def _maybe_apply_stealth_async(context: BrowserContext) -> None:
    """
    Reduce automation fingerprints (navigator.webdriver, etc.). Optional: set
    SLACK_PLAYWRIGHT_STEALTH=0 to skip. Requires: pip install playwright-stealth
    """
    if _env_truthy("SLACK_PLAYWRIGHT_STEALTH_DISABLE"):
        return
    try:
        from playwright_stealth import Stealth
    except ImportError:
        print(
            "WARN: playwright-stealth not installed; "
            "pip install playwright-stealth for anti-detection hooks.",
            file=sys.stderr,
        )
        return
    try:
        await Stealth().apply_stealth_async(context)
        print("playwright-stealth: applied to persistent context.", flush=True)
    except Exception as e:
        print(f"WARN: playwright-stealth apply failed: {e}", file=sys.stderr)


async def _grant_slack_media_permissions(context: BrowserContext) -> None:
    """Huddle pre-join UI needs camera/mic; without grants + fake devices the popup can stay blank."""
    for origin in ("https://app.slack.com", "https://slack.com"):
        try:
            await context.grant_permissions(
                ["camera", "microphone", "notifications"],
                origin=origin,
            )
        except Exception as e:
            print(f"WARN: grant_permissions ({origin}): {e}", file=sys.stderr)


async def _discover_new_page(
    context: BrowserContext, before: list[Page], timeout_sec: float = 25
) -> Optional[Page]:
    before_set = set(before)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for pg in context.pages:
            if pg not in before_set:
                return pg
        await sleep_ms(200)
    return None


async def _wait_huddle_popup_ready(popup: Page) -> None:
    try:
        await popup.bring_to_front()
    except Exception:
        pass
    try:
        await popup.wait_for_load_state("domcontentloaded", timeout=90000)
    except Exception:
        pass
    print(f"Huddle popup URL (initial): {popup.url}", flush=True)
    # Slack often opens about:blank first; URL may lag while JS paints. Wait for URL or real DOM text.
    try:
        await popup.wait_for_function(
            """() => {
  const u = location.href || "";
  if (u.includes("slack.com") || u.includes("slack-edge") || u.startsWith("blob:")) return true;
  const t = (document.body && document.body.innerText) ? document.body.innerText.trim() : "";
  const low = t.toLowerCase();
  if (t.length > 80) return true;
  if (low.includes("huddle") || low.includes("camera") || low.includes("start huddle")) return true;
  return false;
}""",
            timeout=120000,
        )
    except Exception:
        pass
    print(f"Huddle popup URL (after wait): {popup.url}", flush=True)


async def _maybe_close_stuck_blank_huddle_popup(popup: Page, main: Page) -> None:
    """
    Optional (SLACK_HUDDLE_CLOSE_BLANK_POPUP=1): If the extra window stays about:blank with
    almost no text, close it — Slack may show pre-join on the main tab instead.

    **Default is OFF:** closing a white about:blank window too early often destroys the real
    Huddle Preview before Slack finishes navigating (common on slow/VNC/root setups).
    """
    if not _env_truthy("SLACK_HUDDLE_CLOSE_BLANK_POPUP"):
        return
    try:
        u = (popup.url or "").strip()
        inner = await popup.evaluate(
            "() => (document.body && document.body.innerText || '').trim()"
        )
        if u != "about:blank" and u:
            return
        if len(inner) > 80:
            return
        print(
            "WARN: Huddle popup stayed on about:blank with no UI; closing it and "
            "focusing the main Slack tab (pre-join may be a modal there).",
            file=sys.stderr,
            flush=True,
        )
        await popup.close()
        try:
            await main.bring_to_front()
        except Exception:
            pass
    except Exception:
        pass


async def _main_channel_shows_active_huddle(page: Page) -> bool:
    """
    True when Slack already shows an active huddle on the main channel (e.g. joined LIVE).
    In that case a second Chrome window may stay about:blank forever — do not wait for
    Start Huddle there.
    """
    try:
        return bool(
            await page.evaluate(
                """() => {
  const t = ((document.body && document.body.innerText) || "").toLowerCase();
  if (t.includes("joined the huddle")) return true;
  if (t.includes("only one here") && t.includes("huddle")) return true;
  if (t.includes("you're in the huddle") || t.includes("you’re in the huddle")) return true;
  if (t.includes("in this huddle") && t.includes("live")) return true;
  return false;
}"""
            )
        )
    except Exception:
        return False


async def _close_stuck_blank_secondary_windows(
    context: BrowserContext, main: Page
) -> None:
    """Close extra about:blank windows with almost no DOM text (leftover pop-out)."""
    for pg in list(context.pages):
        if pg == main:
            continue
        try:
            u = (pg.url or "").strip().lower()
            if u != "about:blank":
                continue
            inner = await pg.evaluate(
                "() => (document.body && document.body.innerText || '').trim()"
            )
            if len(inner) < 120:
                await pg.close()
                print(
                    "Closed stuck about:blank secondary window (main Slack keeps focus).",
                    flush=True,
                )
        except Exception:
            pass


async def wait_and_click_start_huddle_prejoin(
    context: BrowserContext, main_channel_page: Page
) -> None:
    """
    Slack opens **Huddle Preview** (often a separate window). You must click **Start Huddle**
    there before the channel shows the huddle strip / **invite someone** on the main tab.

    If the main tab already shows an active huddle while a pop-out stays blank, we skip
    waiting for Start Huddle and close the blank window.
    """
    if await _main_channel_shows_active_huddle(main_channel_page):
        print(
            "Main channel already in an active huddle — skipping Start Huddle wait.",
            flush=True,
        )
        await _close_stuck_blank_secondary_windows(context, main_channel_page)
        try:
            await main_channel_page.bring_to_front()
        except Exception:
            pass
        await sleep_ms(2000)
        return

    deadline = time.time() + 120
    name_rx = re.compile(r"start huddle", re.I)
    while time.time() < deadline:
        if await _main_channel_shows_active_huddle(main_channel_page):
            print(
                "Main channel entered active huddle — skipping remaining Start Huddle search.",
                flush=True,
            )
            await _close_stuck_blank_secondary_windows(context, main_channel_page)
            try:
                await main_channel_page.bring_to_front()
            except Exception:
                pass
            await sleep_ms(2000)
            return
        for pg in list(context.pages):
            try:
                loc = pg.get_by_role("button", name=name_rx)
                n = await loc.count()
                if n > 0:
                    first = loc.first
                    await first.wait_for(state="visible", timeout=8000)
                    await pg.bring_to_front()
                    await first.click(timeout=15000)
                    print(
                        'Clicked "Start Huddle" (Huddle Preview pop-out).',
                        flush=True,
                    )
                    await sleep_ms(1500)
                    try:
                        await main_channel_page.bring_to_front()
                    except Exception:
                        pass
                    await sleep_ms(4500)
                    return
                clicked = await pg.evaluate(
                    """() => {
  const btns = Array.from(document.querySelectorAll("button, [role='button']"));
  const b = btns.find((b) => {
    const t = (b.innerText || "").trim().toLowerCase();
    return t.includes("start huddle") && !t.includes("cancel");
  });
  if (b && !b.disabled) { b.click(); return true; }
  return false;
}"""
                )
                if clicked:
                    print(
                        'Clicked "Start Huddle" (pre-join, DOM fallback).',
                        flush=True,
                    )
                    await sleep_ms(1500)
                    try:
                        await main_channel_page.bring_to_front()
                    except Exception:
                        pass
                    await sleep_ms(4500)
                    return
            except Exception:
                continue
            await sleep_ms(400)
        await sleep_ms(400)
    raise TimeoutError(
        'Did not find a clickable "Start Huddle" button after opening Huddle Preview. '
        "Complete mic/camera prompts if blocking the button, or run with SLACK_HEADLESS unset in VNC."
    )


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
    vis = _js_invite_button_visible()
    while (time.time() - start) * 1000 < timeout_ms:
        await clear_obstructions(page)
        await scroll_channel_to_bottom(page, 2)
        found = await page.evaluate(vis)
        if found:
            return
        await sleep_ms(350)
    raise TimeoutError(
        'Timeout waiting for huddle "invite" control (invite someone / invite people / '
        "localized text). If Slack UI is not English, set SLACK_INVITE_MATCH_EXTRA. "
        "If headless, try SLACK_HEADLESS=0 in VNC — Slack may limit huddle UI in headless."
    )


async def click_invite_someone_robust(page: Page) -> None:
    click_js = _js_invite_button_click()
    for _attempt in range(8):
        await clear_obstructions(page)
        await scroll_channel_to_bottom(page, 2)
        ok = await page.evaluate(click_js)
        if ok:
            return
        await sleep_ms(300)
    raise RuntimeError(
        'Cannot click huddle invite control after multiple attempts. '
        "Set SLACK_INVITE_MATCH_EXTRA if your Slack language is not English."
    )


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


async def _pick_page_for_slack(context: BrowserContext) -> Page:
    """
    Prefer a tab that already shows Slack (multi-tab restore). Otherwise use the first
    tab — ``goto`` will load the channel even if it starts as ``about:blank``.

    Do **not** close all ``about:blank`` tabs: that can leave **zero** tabs, and then
    ``context.new_page()`` fails on some Linux/VNC setups (Target.createTarget).
    """
    pages = list(context.pages)
    for pg in pages:
        u = (pg.url or "").lower()
        if "slack.com" in u and "about:blank" not in u:
            try:
                await pg.bring_to_front()
            except Exception:
                pass
            return pg
    if pages:
        page = pages[0]
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return page
    try:
        return await context.new_page()
    except Exception as e:
        raise RuntimeError(
            "No tab exists and BrowserContext.new_page() failed. "
            "Common on VNC if the browser has no open window — try DISPLAY=:0 or :1. "
            f"Underlying: {e}"
        ) from e


async def _route_block_heavy(route) -> None:
    """Block heavy asset types except on Slack origins.

    Do **not** block third-party **fonts** (e.g. fonts.gstatic.com): Slack loads webfonts from
    non-slack.com hosts; blocking them causes broken glyph rendering (e.g. repeated placeholder
    text) and blank/white UI on some setups.
    """
    url = route.request.url or ""
    if "slack.com" in url or "slack-edge.com" in url or "slack-imgs.com" in url:
        await route.continue_()
        return
    t = route.request.resource_type
    # Only trim images/media off-domain; never abort fonts (Slack uses Google Fonts CDNs, etc.).
    if t in ("image", "media"):
        await route.abort()
    else:
        await route.continue_()


async def run_flow() -> None:
    Path(SESSION_DIR).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        try:
            launch_kw: dict[str, Any] = {
                "user_data_dir": SESSION_DIR,
                "headless": SLACK_HEADLESS,
                "viewport": {"width": 1366, "height": 768},
                "args": _launch_args(),
                # Selenium parity: ChromeOptions(excludeSwitches=["enable-automation"]) → do not pass
                # Playwright's default --enable-automation (same bar / automation flag).
                "ignore_default_args": ["--enable-automation"],
                # Pair with --no-sandbox in _launch_args (Linux/root/VNC); avoids inconsistent sandbox flags.
                "chromium_sandbox": False,
            }
            if CHROME_PATH:
                launch_kw["executable_path"] = CHROME_PATH
            _ua = _slack_user_agent_for_launch()
            if _ua:
                launch_kw["user_agent"] = _ua

            context = await p.chromium.launch_persistent_context(**launch_kw)
            await _maybe_apply_stealth_async(context)
            await _grant_slack_media_permissions(context)
        except Exception as e:
            print(f"ERROR: Failed to launch browser: {e}", file=sys.stderr)
            dump_launch_error("browser_launch_failed", e)
            raise RuntimeError("browser_launch_failed") from e

        page: Optional[Page] = None
        try:
            page = await _pick_page_for_slack(context)
            assert page is not None
            _LAST_PAGE[0] = page
            await page.route("**/*", _route_block_heavy)

            print(f"Opening channel: {SLACK_CHANNEL_URL}")
            await page.goto(
                SLACK_CHANNEL_URL,
                wait_until="domcontentloaded",
                timeout=120000,
            )
            print(f"After goto URL: {page.url}", flush=True)
            await _require_slack_logged_in(page)
            await wait_for_slack_loaded(page)
            await clear_obstructions(page)

            print("Clicking headset (huddle) — waiting for pop-out or new tab...")
            popup = await click_huddle_open_popup(page, context)
            if popup:
                await _wait_huddle_popup_ready(popup)
                await _maybe_close_stuck_blank_huddle_popup(popup, page)
            print(
                "Huddle Preview: waiting for Start Huddle (pop-out), then main channel for invite...",
                flush=True,
            )
            await wait_and_click_start_huddle_prejoin(context, page)
            await clear_obstructions(page)
            await sleep_ms(2000)

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
                "✅ DONE: headset -> Start Huddle -> invite someone -> @channel -> Send Invite",
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
    except RuntimeError as e:
        if e.args and e.args[0] == "browser_launch_failed":
            sys.exit(2)
        raise


def _warn_display_if_headed_linux() -> None:
    """Headed Chrome on Linux needs DISPLAY; VNC SSH sessions often forget it."""
    if sys.platform != "linux":
        return
    if SLACK_HEADLESS:
        return
    if (os.getenv("DISPLAY") or "").strip():
        return
    print(
        "WARN: DISPLAY is unset — headed Chrome will usually fail on VNC/SSH.\n"
        "  In the same terminal where you run this script, set the display your VNC uses, e.g.:\n"
        "    export DISPLAY=:1\n"
        "  (Run `echo $DISPLAY` in a terminal *inside* the VNC desktop that works, use that value.)\n",
        file=sys.stderr,
        flush=True,
    )


def main() -> None:
    # Validate env and deps here — not inside asyncio (sys.exit in async tasks causes noisy tracebacks).
    if not SESSION_DIR:
        print("ERROR: SESSION_DIR env is required.", file=sys.stderr)
        sys.exit(2)
    if not SLACK_CHANNEL_URL:
        print("ERROR: SLACK_CHANNEL_URL env is required.", file=sys.stderr)
        sys.exit(2)
    _enforce_playwright_stealth_installed()
    _warn_display_if_headed_linux()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
