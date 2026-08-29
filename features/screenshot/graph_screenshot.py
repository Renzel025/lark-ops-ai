"""
Optional: when a P0 session starts, capture a dashboard URL (e.g. **Grafana**) with Playwright and post
the PNG to a Lark group.

A text line with the **capture date/time** (when ``page.screenshot`` completed) is posted before the
image — see ``P0_GRAPH_SCREENSHOT_CAPTION``, ``{captured_at}``, and ``P0_GRAPH_SCREENSHOT_TIMEZONE``.

**Clean “panels-only” grabs (default):** ``P0_GRAPH_SCREENSHOT_KIOSK=1`` appends Grafana **kiosk** mode to
the URL (hides left navigation). ``P0_GRAPH_SCREENSHOT_CLIP_SELECTOR`` (or the built-in fallback chain)
picks the **dashboard body** — but on wide **multi-panel** boards (e.g. Core Metrics) that chain often
matches an **inner** ``.scrollbar-view`` (one panel’s scroller), so you only get a slice of the UI.

**Two Lark-style images (top / bottom of *what’s on screen* — like your refs):** set
``P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY=1`` + ``P0_GRAPH_SCREENSHOT_SPLIT_VERTICAL_HALVES=1`` and leave
``P0_GRAPH_SCREENSHOT_FULL_DOCUMENT=0``. That captures the **viewport at scroll top**, then scrolls the main dashboard and takes **N** full-viewport shots (``N`` = ``P0_GRAPH_SCREENSHOT_VIEWPORT_SCROLL_COUNT``, default **2** when split-halves is on — use **3** or **4** for less clutter per image). If the page does not scroll, falls back to Pillow halving one viewport. ``P0_GRAPH_SCREENSHOT_FULL_DOCUMENT=1`` is **full document** height (``full_page=True``) — often
an enormous, half-empty strip when Grafana’s layout is tall; use only when you really want entire scroll.

For **multi-panel Grafana** dashboards: wide viewport (e.g. **1920×1080**), ``GOTO_WAIT_UNTIL=load``, and
raise ``P0_GRAPH_SCREENSHOT_WAIT_MS`` (e.g. **12000–20000**). Enable
``P0_GRAPH_SCREENSHOT_PANEL_READY_TIMEOUT_MS`` (e.g. **25000–35000**) so React mounts before panels;
``P0_GRAPH_SCREENSHOT_BAND_PANEL_READY_RATIO`` (default **0.88**) and ``BAND_MAX_BLANK_PANELS=0``
block capture while panels are still black/loading.

**Two images (upper / lower half):** ``P0_GRAPH_SCREENSHOT_SPLIT_VERTICAL_HALVES=1`` uses **two**
``full_page`` screenshots with vertical **clips** (no Pillow required). Grafana nests several
``.scrollbar-view`` nodes; we pick the one with the largest scroll overflow for scrolling. Virtualized
tables have ``scrollHeight >> clientHeight``: bisecting the full clip **yields a black upper PNG**. In
that case we **scroll** that target to the top and bottom and capture **two viewport-sized** clips. If
the clip box is still absurdly tall vs what’s visible, we skip geometric bisection and fall back to one
full-page capture (Pillow split if installed). If clips cannot be computed, falls back to Pillow split,
then a single full-page PNG.

If Lark shows **solid gray / blank** PNGs, the first CSS match was often a **narrow** scroll strip
(not the dashboard); the bot now skips those and tries the next selector (e.g. ``main``).
**Solid black** panels on Linux headless are almost always the bundled *headless shell* failing to
composite uPlot/canvas/WebGL. Primary fix: launch the full Chromium build in NEW headless mode via
``channel="chromium"`` (``get_p0_graph_screenshot_chromium_channel`` — on by default on Linux; needs
``playwright install chromium``). SwiftShader software-GL flags are also enabled by default on Linux
(see ``get_p0_graph_screenshot_swiftshader``); set ``P0_GRAPH_SCREENSHOT_SWIFTSHADER=0`` to force off.
Install **Pillow** so uniformly-dark captures can trigger an automatic viewport-only retry.

Logged-in runs should use a **fixed browser zoom** in the persistent Playwright profile (100 % is
simplest): e.g. 50 % zoom changes how much fits in the viewport and alters scroll/virtualized metrics.

Without ``P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR``, each run uses a **fresh** Chromium — fine for
anonymous/public dashboards only. For logged-in Grafana, point that env at a **persistent profile**
where you completed login once (headed), similar to Slack ``SESSION_DIR``.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Playwright: imported inside ``_capture_png_payloads`` only, so this module loads even when
# ``playwright`` is not installed (optional feature / lighter test imports).

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (see p0_logic/requirements.txt backports.zoneinfo)
    from backports.zoneinfo import ZoneInfo

log = logging.getLogger("lark-ops-ai")

_INTERVAL_LOCK = threading.Lock()
_interval_timer: Optional[threading.Timer] = None
_interval_source_label: str = ""
_capture_busy_lock = threading.Lock()
_capture_busy = False
_capture_ctx = threading.local()
_ON_DEMAND_PENDING: List[Dict[str, Any]] = []
_ON_DEMAND_PENDING_LOCK = threading.Lock()
_on_demand_timed_out = False
_on_demand_timed_out_lock = threading.Lock()

# Reuse Chromium between on-demand captures (avoids ~20–40s cold launch each time).
_BROWSER_POOL_LOCK = threading.Lock()
_BROWSER_POOL: Dict[str, Any] = {}


def _busy_lock_timeout_sec() -> float:
    from p0_logic import config as _config

    return float(_config.get_p0_graph_screenshot_busy_lock_timeout_sec())


def _read_capture_busy() -> bool:
    """Bounded read of the busy flag. Never blocks forever; on contention assume busy."""
    got = _capture_busy_lock.acquire(timeout=_busy_lock_timeout_sec())
    try:
        return bool(_capture_busy)
    finally:
        if got:
            _capture_busy_lock.release()


def _mark_capture_busy() -> bool:
    """
    Bounded acquire + claim of the capture slot. Returns True iff we now own an *idle* slot
    (caller must later call ``_clear_capture_busy``). Returns False if a capture is already
    running OR the lock is contended past the timeout — either way the caller skips/queues
    instead of blocking. The lock is held ONLY for the check-then-set, never across capture.
    """
    global _capture_busy
    got = _capture_busy_lock.acquire(timeout=_busy_lock_timeout_sec())
    if not got:
        log.warning(
            "p0 graph screenshot: busy-lock contended >%.1fs — treating slot as busy",
            _busy_lock_timeout_sec(),
        )
        return False
    try:
        if _capture_busy:
            return False
        _capture_busy = True
        return True
    finally:
        _capture_busy_lock.release()


def _clear_capture_busy() -> None:
    """Bounded release of the busy flag. Clears the flag even if the lock is contended
    (bool assignment is GIL-atomic) so a wedged lock can never leave the slot stuck busy."""
    global _capture_busy
    got = _capture_busy_lock.acquire(timeout=_busy_lock_timeout_sec())
    try:
        _capture_busy = False
    finally:
        if got:
            _capture_busy_lock.release()
        else:
            log.warning(
                "p0 graph screenshot: busy-lock contended on release — cleared flag anyway"
            )


def _force_kill_capture_chromium() -> None:
    """
    Watchdog escape hatch: kill the Grafana Chromium so any stuck sync Playwright call raises
    (sync API can't be signal-interrupted), then drop the warm pool so the next capture cold
    starts clean. Best-effort, never raises. Safe because captures are serialised by
    ``_capture_busy`` — this only ever targets the wedged capture's own browser.
    """
    from p0_logic import config as _config

    ud = ""
    try:
        ud = (_config.get_p0_graph_screenshot_playwright_user_data_dir() or "").strip()
    except Exception:
        ud = ""
    if ud:
        try:
            _kill_stale_grafana_chromium(ud)
        except Exception as e:
            log.warning("p0 graph screenshot watchdog: kill chromium failed: %s", e)
    else:
        log.warning(
            "p0 graph screenshot watchdog: no persistent user-data-dir — cannot pkill by profile"
        )
    try:
        _browser_pool_close()
    except Exception as e:
        log.warning("p0 graph screenshot watchdog: pool close failed: %s", e)


def _react_failed_explicit(token: str, trigger_message_id: str) -> None:
    """
    Post the FAILED emoji reaction using an EXPLICIT trigger message id (not the thread-local
    capture ctx). The watchdog runs on a separate Timer thread where the thread-local ctx is
    unset, so ``_react_to_trigger_message`` would no-op there — this path always works.
    """
    from p0_logic import config as _config
    from p0_logic import lark_client as _lark

    if not _config.get_p0_graph_screenshot_react_enabled():
        return
    et = (_config.get_p0_graph_screenshot_react_failed_emoji() or "").strip()
    mid = (trigger_message_id or "").strip()
    tok = (token or "").strip()
    if not et or not mid or not tok:
        return
    try:
        st, _ = _lark.add_message_reaction(mid, tok, et)
        if st == 200:
            log.info("p0 graph screenshot: FAILED reaction on trigger msg tail=%s", mid[-12:])
    except Exception as e:
        log.warning("p0 graph screenshot: failed-reaction post error: %s", e)


def _capture_ctx_set(
    *,
    on_demand: bool = False,
    force_full: bool = False,
    trigger_message_id: str = "",
) -> None:
    _capture_ctx.on_demand = on_demand
    _capture_ctx.force_full = force_full
    _capture_ctx.error = ""
    _capture_ctx.trigger_message_id = (trigger_message_id or "").strip()


def _capture_ctx_clear() -> None:
    _capture_ctx.on_demand = False
    _capture_ctx.force_full = False
    _capture_ctx.error = ""
    _capture_ctx.trigger_message_id = ""


def _get_trigger_message_id() -> str:
    return str(getattr(_capture_ctx, "trigger_message_id", "") or "").strip()


def _react_to_trigger_message(token: str, emoji_type: str) -> None:
    """Add Lark emoji reaction to the on-demand request message (OTE-AI style)."""
    from p0_logic import config as _config
    from p0_logic import lark_client as _lark

    if not _config.get_p0_graph_screenshot_react_enabled():
        return
    if not _is_on_demand_capture():
        return
    et = (emoji_type or "").strip()
    if not et:
        return
    mid = _get_trigger_message_id()
    tok = (token or "").strip()
    if not mid or not tok:
        return
    st, _ = _lark.add_message_reaction(mid, tok, et)
    if st == 200:
        log.info("p0 graph screenshot: reaction %s on trigger msg tail=%s", et, mid[-12:])


def _set_capture_error(msg: str) -> None:
    _capture_ctx.error = (msg or "").strip()[:500]


def _get_capture_error() -> str:
    return str(getattr(_capture_ctx, "error", "") or "").strip()


def _capture_ctx_force_full() -> bool:
    return bool(getattr(_capture_ctx, "force_full", False))


def _is_on_demand_capture() -> bool:
    return bool(getattr(_capture_ctx, "on_demand", False))


def _effective_fast_capture() -> bool:
    from p0_logic import config as _config

    if _capture_ctx_force_full():
        return False
    if _is_on_demand_capture():
        return _config.get_p0_graph_screenshot_on_demand_fast()
    return _config.get_p0_graph_screenshot_fast_capture()


def _priority_screenshot_eligible(priority: str) -> bool:
    """P0 always qualifies; P1 only when P0_GRAPH_SCREENSHOT_ON_P1 is on."""
    from p0_logic import config as _config

    pri = (priority or "").strip().upper()
    if pri == "P0":
        return True
    if pri == "P1":
        return _config.get_p0_graph_screenshot_on_p1_enabled()
    return False


def _has_screenshot_eligible_session() -> bool:
    from features.session.session import P0_SESSIONS

    for sess in P0_SESSIONS.values():
        if _priority_screenshot_eligible(str(sess.get("priority") or "")):
            return True
    return False


def _stop_graph_screenshot_interval() -> None:
    global _interval_timer
    with _INTERVAL_LOCK:
        if _interval_timer is not None:
            _interval_timer.cancel()
            _interval_timer = None


def _schedule_graph_screenshot_interval_tick(delay_sec: float) -> None:
    global _interval_timer
    with _INTERVAL_LOCK:
        if _interval_timer is not None:
            _interval_timer.cancel()
        t = threading.Timer(delay_sec, _graph_screenshot_interval_tick)
        t.daemon = True
        _interval_timer = t
        t.start()


def _graph_screenshot_interval_tick() -> None:
    global _interval_timer
    with _INTERVAL_LOCK:
        _interval_timer = None
    try:
        from p0_logic import config as _config
        from p0_logic import lark_client as _lark

        if not _config.p0_graph_screenshot_enabled():
            return
        mins = _config.get_p0_graph_screenshot_interval_min()
        if mins <= 0:
            return
        if not _has_screenshot_eligible_session():
            log.info("p0 graph screenshot interval: no screenshot-eligible session — stopped")
            return
        chat_id = _config.get_p0_graph_screenshot_target_chat_id()
        if not _config.get_p0_graph_screenshot_url() or not chat_id:
            return
        tok = _lark.get_tenant_token_primary()
        if not tok:
            log.warning("p0 graph screenshot interval: no tenant token")
            return
        label = _interval_source_label
        log.info(
            "p0 graph screenshot interval: repeat capture (%s min, P0 still active)",
            mins,
        )
        # NOTE: do NOT hold _capture_busy_lock across this call. The body re-acquires the
        # (non-reentrant) busy-lock itself via _mark_capture_busy(); wrapping it here caused a
        # self-deadlock that wedged the lock forever (killed the interval AND on-demand). The
        # body now claims the slot with a bounded acquire and skips cleanly if a capture is
        # already running.
        _capture_and_post_ranges_thread_body(tok, chat_id, label)
    except Exception as e:
        log.warning("p0 graph screenshot interval tick failed: %s", e, exc_info=True)
    finally:
        from p0_logic import config as _config

        if (
            _config.p0_graph_screenshot_enabled()
            and _config.get_p0_graph_screenshot_interval_min() > 0
            and _has_screenshot_eligible_session()
        ):
            delay = float(_config.get_p0_graph_screenshot_interval_min()) * 60.0
            _schedule_graph_screenshot_interval_tick(delay)


def start_p0_graph_screenshot_interval(source_chat_label: str) -> None:
    """Schedule repeat captures every ``P0_GRAPH_SCREENSHOT_INTERVAL_MIN`` while P0 is active."""
    from p0_logic import config as _config

    mins = _config.get_p0_graph_screenshot_interval_min()
    if mins <= 0 or not _config.p0_graph_screenshot_enabled():
        return
    global _interval_source_label
    _interval_source_label = (source_chat_label or "").strip()
    delay = float(mins) * 60.0
    _schedule_graph_screenshot_interval_tick(delay)
    log.info(
        "p0 graph screenshot interval: next repeat in %s min while P0 active (label=%r)",
        mins,
        _interval_source_label[:48] if _interval_source_label else "",
    )


def on_p0_session_ended_for_graph_screenshot() -> None:
    """Call when a P0/P1 session ends; stops the repeat timer if no screenshot-eligible session remains."""
    if _has_screenshot_eligible_session():
        return
    _stop_graph_screenshot_interval()
    log.info("p0 graph screenshot interval: stopped (no screenshot-eligible session)")


def _apply_kiosk_to_grafana_url(url: str, enable: bool, *, hide_time_picker: bool = True) -> str:
    """Append a bare ``kiosk`` param (+ optional hide dashboard chrome) when missing."""
    u = (url or "").strip()
    if not u or not enable:
        return u
    low = u.lower()
    parsed = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
    if hide_time_picker and not any(k.lower() == "_dash.hidetimepicker" for k, _ in q):
        q.append(("_dash.hideTimePicker", "true"))
    if not any(k.lower() == "_dash.hidevariables" for k, _ in q):
        q.append(("_dash.hideVariables", "true"))
    new_query = urlencode(q)
    # Grafana 11 dropped ``kiosk=tv`` (it left the nav dock/sidebar visible). The current form is a
    # bare valueless ``kiosk`` param — append ``&kiosk`` (no ``=``) so the left nav is actually hidden.
    if "kiosk" not in low:
        new_query = (new_query + "&kiosk") if new_query else "kiosk"
    return urlunparse(parsed._replace(query=new_query))


def _prepare_grafana_capture_url(url: str, *, kiosk: bool) -> str:
    """
    URL used for Playwright capture: drop ``refresh=`` so panels do not auto-reload during waits
    (``refresh=1m`` on a heavy board looks like a hang on the KPI row).
    """
    u = (url or "").strip()
    if not u:
        return u
    parsed = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
    had_refresh = any(k.lower() == "refresh" for k, _ in q)
    q = [(k, v) for k, v in q if k.lower() != "refresh"]
    if had_refresh:
        log.info(
            "p0 graph screenshot: removed refresh= from capture URL (avoids reload during panel wait)"
        )
    u = urlunparse(parsed._replace(query=urlencode(q)))
    from p0_logic import config as _config

    hide_tp = not _config.get_p0_graph_screenshot_include_time_bar()
    if not hide_tp:
        log.info("p0 graph screenshot: INCLUDE_TIME_BAR=1 — keep Grafana time range / refresh visible")
    return _apply_kiosk_to_grafana_url(u, kiosk, hide_time_picker=hide_tp)


def _preset_grafana_nav_local_storage(page) -> None:
    """Grafana 11 dock prefs — apply before navigation so the sidebar starts collapsed/undocked."""
    try:
        page.evaluate(
            """() => {
              try {
                localStorage.setItem('grafana.navigation.docked', 'false');
                localStorage.setItem('grafana.navigation.dock', '');
                localStorage.setItem('grafana.navigation.dockState', 'collapsed');
                localStorage.setItem('grafana.navigation.dockStateBeforeLogin', 'collapsed');
              } catch (e) {}
            }"""
        )
    except Exception:
        pass


def _inject_grafana_capture_styles(page) -> None:
    """Persistent CSS — hide left dock / nav (Grafana 8–11)."""
    try:
        page.evaluate(
            """() => {
              let st = document.getElementById('p0-grafana-capture-css');
              if (!st) {
                st = document.createElement('style');
                st.id = 'p0-grafana-capture-css';
                document.head.appendChild(st);
              }
              st.textContent = `
                .sidemenu,
                .navbar,
                [data-testid="NavBar"],
                [data-testid="navbar"],
                [data-testid="nav-menu"],
                [data-testid="NavMenu"],
                [data-testid="DockedNavigation"],
                nav[aria-label="Main"],
                nav[aria-label="Navigation"],
                aside[aria-label="Navigation"],
                .page-sidebar,
                [class*="NavMenu"],
                [class*="navMenu"],
                [class*="DockMenu"],
                [class*="dockedNav"],
                [class*="DockedNav"],
                [class*="SideNav"],
                [class*="sideNav"],
                #dockMenuContent,
                .dashboard-solo .navbar,
                .sidemenu-custom,
                [data-testid="mega-menu"],
                [data-testid="navigation-menu"],
                [class*="NavToolbar"] nav,
                [class*="NavDock"],
                [class*="navDock"],
                [class*="DockMenuMain"],
                [class*="dock-menu"],
                [class*="page-content"] nav,
                #reactRoot nav,
                #reactRoot aside {
                  display: none !important;
                  visibility: hidden !important;
                  width: 0 !important;
                  min-width: 0 !important;
                  max-width: 0 !important;
                  opacity: 0 !important;
                  pointer-events: none !important;
                  position: absolute !important;
                  left: -9999px !important;
                }
                .main-view,
                main,
                [data-testid="dashboard-scene"],
                .dashboard-container,
                .page-container,
                [class*="DashboardCanvas"],
                [class*="page-body"],
                [class*="page-content"],
                [data-testid="page-content"] {
                  margin-left: 0 !important;
                  padding-left: 0 !important;
                  left: 0 !important;
                  width: 100% !important;
                  max-width: 100% !important;
                  transform: none !important;
                }
              `;
            }"""
        )
    except Exception as e:
        log.debug("p0 graph screenshot: inject capture CSS failed: %s", e)


def _grafana_sidebar_likely_open(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                  const picks = [
                    '.sidemenu',
                    '[data-testid="NavBar"]',
                    '[data-testid="navbar"]',
                    '[data-testid="nav-menu"]',
                    '[data-testid="NavMenu"]',
                    'nav[aria-label="Main"]',
                    'nav[aria-label="Navigation"]',
                    '.navbar',
                    '[class*="NavMenu"]',
                    '[class*="DockMenu"]',
                    '[class*="SideNav"]',
                  ];
                  for (const sel of picks) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width >= 80 && r.height >= 200) return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


def _click_grafana_nav_toggle(page) -> bool:
    """Click Grafana 10/11 dock toggle — Grafana 11 uses ``Undock menu`` on the dock header."""
    for name in (
        "Undock menu",
        "Close menu",
        "Close navigation menu",
        "Collapse menu",
        "Toggle menu",
    ):
        try:
            btn = page.get_by_role("button", name=name, exact=True)
            if btn.count() > 0 and btn.first.is_visible(timeout=600):
                btn.first.click(timeout=2500)
                log.info("p0 graph screenshot: clicked Grafana button %r", name)
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    try:
        clicked = page.evaluate(
            """() => {
              const labels = [
                'Undock menu',
                'Dock menu',
                'Close menu',
                'Close navigation menu',
                'Collapse menu',
                'Toggle menu',
                'Open menu',
              ];
              for (const label of labels) {
                const btn = document.querySelector(`button[aria-label="${label}"]`);
                if (btn && btn.offsetParent !== null) {
                  btn.click();
                  return label;
                }
              }
              const testIds = [
                '[data-testid="nav-menu-collapse"]',
                '[data-testid="nav-menu-button"]',
                '[data-testid="navbar-toggle"]',
                '[data-testid="nav-burger"]',
              ];
              for (const sel of testIds) {
                const btn = document.querySelector(sel);
                if (btn && btn.offsetParent !== null) {
                  btn.click();
                  return sel;
                }
              }
              const chrome = document.querySelector(
                '[data-testid="AppChrome"], [class*="AppChrome"], header'
              );
              if (chrome) {
                for (const btn of chrome.querySelectorAll('button')) {
                  const r = btn.getBoundingClientRect();
                  if (r.width >= 16 && r.height >= 16 && r.left < 140 && r.top < 96) {
                    btn.click();
                    return 'app-chrome-toggle';
                  }
                }
              }
              return '';
            }"""
        )
        if clicked:
            log.info("p0 graph screenshot: clicked Grafana nav toggle (%s)", clicked)
            page.wait_for_timeout(500)
            return True
    except Exception as e:
        log.debug("p0 graph screenshot: nav toggle JS click failed: %s", e)
    for sel in (
        '[aria-label="Undock menu"]',
        '[aria-label="Dock menu"]',
        '[data-testid="nav-menu-button"]',
        '[data-testid="nav-menu-collapse"]',
        '[aria-label="Close menu"]',
        '[aria-label="Close navigation menu"]',
        '[aria-label="Collapse menu"]',
        '[aria-label="Toggle menu"]',
        'button[aria-label*="Undock" i]',
        'button[aria-label*="menu" i]',
        'button[aria-label*="Close" i]',
        ".navbar-toggle-button",
        '[data-testid="nav-burger"]',
        ".sidemenu__top .sidemenu__logo",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=800):
                loc.first.click(timeout=2500)
                log.info("p0 graph screenshot: clicked Grafana nav toggle selector=%s", sel)
                page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    return False


def _collapse_grafana_sidebar(page) -> None:
    """Hide Grafana left nav / dock so captures look like kiosk / full-width dashboard."""
    _preset_grafana_nav_local_storage(page)
    _inject_grafana_capture_styles(page)
    _click_grafana_nav_toggle(page)
    # Grafana 11: Undock button lives on the dock header — try nav-scoped clicks too.
    for sel in (
        'nav button[aria-label="Undock menu"]',
        'aside button[aria-label="Undock menu"]',
        '[role="navigation"] button[aria-label="Undock menu"]',
        '[aria-label="Undock menu"]',
        '[aria-label="Dock menu"]',
        '[data-testid="nav-menu-collapse"]',
        '[data-testid="nav-menu-button"]',
        '[aria-label="Close menu"]',
        '[aria-label="Close navigation menu"]',
        '[aria-label="Collapse menu"]',
        '[aria-label="Toggle menu"]',
        'button[aria-label*="Undock" i]',
        'button[aria-label*="menu" i]',
        'button[aria-label*="Close" i]',
        ".navbar-toggle-button",
        '[data-testid="nav-burger"]',
        ".sidemenu__top .sidemenu__logo",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=500):
                loc.first.click(timeout=2500)
                page.wait_for_timeout(350)
        except Exception:
            continue
    _inject_grafana_capture_styles(page)
    _hide_grafana_left_dock_strip(page)
    try:
        page.evaluate(
            """() => {
              document.body.classList.add('dashboard-solo');
              const main = document.querySelector(
                'main, .dashboard-container, [data-testid="dashboard-scene"], .main-view, .page-container'
              );
              if (main) {
                main.style.marginLeft = '0';
                main.style.paddingLeft = '0';
                main.style.maxWidth = '100%';
                main.style.width = '100%';
              }
            }"""
        )
    except Exception as e:
        log.debug("p0 graph screenshot: sidebar layout JS failed: %s", e)


def _ensure_grafana_sidebar_collapsed(page, *, max_attempts: int = 4) -> bool:
    """Keep clicking dock toggle + CSS hide until left nav is gone (or max attempts)."""
    for attempt in range(max(1, max_attempts)):
        if not _grafana_sidebar_likely_open(page):
            return True
        log.info(
            "p0 graph screenshot: Grafana sidebar still open — collapse attempt %s/%s",
            attempt + 1,
            max_attempts,
        )
        _collapse_grafana_sidebar(page)
        page.wait_for_timeout(700)
    still_open = _grafana_sidebar_likely_open(page)
    if still_open:
        log.warning(
            "p0 graph screenshot: sidebar still visible after %s attempts — "
            "capture CSS hide will still run",
            max_attempts,
        )
    return not still_open


def _hide_grafana_left_dock_strip(page) -> None:
    """Hide collapsed dock rail / leftover left chrome (narrow full-height strip)."""
    try:
        page.evaluate(
            """() => {
              const vh = window.innerHeight || 1080;
              const navWords = /Home|Dashboards|Bookmarks|Starred|Alerting|Grafana/i;
              const picks = [
                'nav',
                'aside',
                '[role="navigation"]',
                '[data-testid*="nav" i]',
                '[class*="dock" i]',
                '[class*="Dock" i]',
                '[class*="SideNav"]',
                '[class*="NavMenu"]',
                '[class*="NavDock"]',
              ];
              for (const sel of picks) {
                document.querySelectorAll(sel).forEach((el) => {
                  const r = el.getBoundingClientRect();
                  const txt = (el.innerText || '').slice(0, 400);
                  if (r.left > 32) return;
                  if (r.width < 16) return;
                  if (r.width > 360 && !navWords.test(txt)) return;
                  if (r.height < vh * 0.25 && !navWords.test(txt)) return;
                  if (r.width <= 360 || navWords.test(txt)) {
                    el.style.setProperty('display', 'none', 'important');
                    el.style.setProperty('width', '0', 'important');
                    el.style.setProperty('min-width', '0', 'important');
                    el.style.setProperty('position', 'absolute', 'important');
                    el.style.setProperty('left', '-9999px', 'important');
                  }
                });
              }
              document.querySelectorAll('body *').forEach((el) => {
                const r = el.getBoundingClientRect();
                if (r.left > 24) return;
                if (r.width < 120 || r.width > 360) return;
                if (r.height < vh * 0.45) return;
                const txt = (el.innerText || '').slice(0, 400);
                if (!navWords.test(txt)) return;
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('width', '0', 'important');
              });
            }"""
        )
    except Exception as e:
        log.debug("p0 graph screenshot: hide left dock strip failed: %s", e)


def _effective_capture_viewport(base_w: int, base_h: int, zoom_pct: int) -> Tuple[int, int]:
    """
    ``zoom_pct`` < 100 means fit more dashboard — expand Playwright viewport instead of CSS zoom
    (CSS zoom + panel clip produced ultra-wide, ultra-short PNGs in Lark).
    """
    pct = int(zoom_pct or 100)
    if pct <= 0 or pct >= 100:
        return base_w, base_h
    factor = 100.0 / pct
    # Cap high enough that 5% vs 30% vs 50% are not all identical (old 3840 cap broke zoom).
    w = min(7680, max(320, int(round(base_w * factor))))
    h = min(4320, max(240, int(round(base_h * factor))))
    return w, h


def _clear_capture_zoom_styles(page) -> None:
    try:
        page.evaluate(
            """() => {
              document.documentElement.style.zoom = '';
              document.body.style.zoom = '';
              document.querySelectorAll(
                '[data-testid="dashboard-scene"], .dashboard-container, '
                + '[data-testid="page-content"], main .page-container, main .page-body, main'
              ).forEach((el) => {
                el.style.zoom = '';
                el.style.transform = '';
                el.style.width = '';
                el.style.maxWidth = '';
              });
            }"""
        )
    except Exception:
        pass


def _apply_browser_page_zoom(page, percent: int) -> None:
    """
    Real **browser zoom** (like Ctrl +/- in Chromium): CDP ``Emulation.setPageScaleFactor``,
    then CSS ``zoom`` on ``html``/``body``. Viewport stays 1920×1080 — not viewport resize.
    """
    pct = int(percent or 100)
    if pct <= 0 or pct >= 100:
        _clear_capture_zoom_styles(page)
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setPageScaleFactor", {"pageScaleFactor": 1.0})
        except Exception:
            pass
        return
    scale = round(pct / 100.0, 4)
    _clear_capture_zoom_styles(page)
    applied = False
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setPageScaleFactor", {"pageScaleFactor": scale})
        log.info(
            "p0 graph screenshot: browser zoom CDP pageScaleFactor=%s (%s%%)",
            scale,
            pct,
        )
        applied = True
    except Exception as e:
        log.debug("p0 graph screenshot: CDP browser zoom failed: %s", e)
    page.evaluate(
        """(z) => {
          document.documentElement.style.zoom = z + '%';
          document.body.style.zoom = z + '%';
        }""",
        pct,
    )
    verify = page.evaluate(
        """() => ({
          htmlZoom: document.documentElement.style.zoom || getComputedStyle(document.documentElement).zoom,
          bodyZoom: document.body.style.zoom || getComputedStyle(document.body).zoom,
        })"""
    )
    log.info(
        "p0 graph screenshot: browser zoom=%s%% (CDP=%s) verify=%s",
        pct,
        applied,
        verify,
    )
    page.wait_for_timeout(400)


def _apply_dashboard_scene_zoom(page, percent: int) -> None:
    """
    Top+bottom Lark framing: keep viewport at 1920×1080 but scale the dashboard scene (like browser
    zoom 50 %) so each of the two scroll shots matches manual refs — expanded viewport fits the whole
    board in one PNG and breaks the top/bottom split.
    """
    pct = int(percent or 100)
    if pct <= 0 or pct >= 100:
        _clear_capture_zoom_styles(page)
        return
    scale = pct / 100.0
    inv_pct = 100.0 / pct
    try:
        page.evaluate(
            """({ scale, invPct, pct }) => {
              document.documentElement.style.zoom = '';
              document.body.style.zoom = '';
              let hit = 0;
              document.querySelectorAll(
                '[data-testid="dashboard-scene"], .dashboard-container, '
                + '[data-testid="page-content"], main .page-body, main'
              ).forEach((el) => {
                el.style.transform = 'scale(' + scale + ')';
                el.style.transformOrigin = 'top left';
                el.style.width = invPct + '%';
                el.style.maxWidth = invPct + '%';
                hit++;
              });
              if (!hit) {
                document.documentElement.style.zoom = pct + '%';
                document.body.style.zoom = pct + '%';
              }
            }""",
            {"scale": scale, "invPct": inv_pct, "pct": pct},
        )
        verify = page.evaluate(
            """() => {
              const main = document.querySelector('main');
              const tr = main ? getComputedStyle(main).transform : '';
              const hz = document.documentElement.style.zoom || '';
              const bz = document.body.style.zoom || '';
              return {
                mainTransform: tr && tr !== 'none' ? tr : '',
                htmlZoom: hz,
                bodyZoom: bz,
              };
            }"""
        )
        if verify and not (verify.get("mainTransform") or verify.get("htmlZoom")):
            page.evaluate(
                "(z) => { document.documentElement.style.zoom = z + '%'; document.body.style.zoom = z + '%'; }",
                pct,
            )
            log.info(
                "p0 graph screenshot: scene transform not detected — fallback html zoom=%s%%",
                pct,
            )
        log.info(
            "p0 graph screenshot: top+bottom scene zoom=%s%% verify=%s",
            pct,
            verify,
        )
    except Exception as e:
        log.warning("p0 graph screenshot: scene zoom failed: %s", e)


def _grafana_viewport_clip_excluding_nav(page) -> Optional[Dict[str, int]]:
    """Full viewport minus any left nav/dock strip — never panel-union (avoids panoramic crops)."""
    try:
        raw = page.evaluate(
            """() => {
              const vw = window.innerWidth || 1920;
              const vh = window.innerHeight || 1080;
              let left = 0;
              const navWords = /Home|Dashboards|Bookmarks|Starred|Alerting|Grafana/i;
              document.querySelectorAll(
                '.sidemenu, [data-testid="NavBar"], [data-testid="navbar"], '
                + '[data-testid="nav-menu"], [data-testid="NavMenu"], '
                + 'nav[aria-label="Main"], nav[aria-label="Navigation"], aside, '
                + '[class*="NavMenu"], [class*="DockMenu"], [class*="SideNav"], '
                + '[class*="dockedNav"], [class*="DockedNav"], [role="navigation"]'
              ).forEach((el) => {
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return;
                const r = el.getBoundingClientRect();
                if (r.left > 32) return;
                if (r.width < 16 || r.height < vh * 0.2) return;
                left = Math.max(left, Math.ceil(r.right));
              });
              document.querySelectorAll('body *').forEach((el) => {
                const r = el.getBoundingClientRect();
                if (r.left > 24) return;
                if (r.width < 100 || r.width > 360) return;
                if (r.height < vh * 0.4) return;
                const txt = (el.innerText || '').slice(0, 400);
                if (!navWords.test(txt)) return;
                left = Math.max(left, Math.ceil(r.right));
              });
              left = Math.min(left, Math.floor(vw * 0.28));
              return {
                x: Math.max(0, left),
                y: 0,
                width: Math.max(480, vw - Math.max(0, left)),
                height: vh,
              };
            }"""
        )
    except Exception as e:
        log.debug("p0 graph screenshot: viewport clip failed: %s", e)
        return None
    if not raw or not isinstance(raw, dict):
        return None
    try:
        w = int(raw.get("width") or 0)
        h = int(raw.get("height") or 0)
        if w < 320 or h < 200:
            return None
        return {
            "x": int(raw.get("x") or 0),
            "y": int(raw.get("y") or 0),
            "width": w,
            "height": h,
        }
    except (TypeError, ValueError):
        return None


def _grafana_main_viewport_clip(page) -> Optional[Dict[str, int]]:
    """Backward-compatible alias — always viewport-based, not panel bounding box."""
    return _grafana_viewport_clip_excluding_nav(page)


def _normalize_screenshot_png(png_bytes: bytes, *, max_width: int = 1920) -> bytes:
    """Downscale wide captures for Lark (keeps aspect ratio)."""
    if not png_bytes or max_width <= 0:
        return png_bytes
    try:
        from PIL import Image
    except ImportError:
        return png_bytes
    try:
        im = Image.open(BytesIO(png_bytes))
        im.load()
        w, h = im.size
        if w <= max_width:
            return png_bytes
        nh = max(1, int(round(h * (max_width / float(w)))))
        im = im.resize((max_width, nh), Image.Resampling.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.debug("p0 graph screenshot: normalize screenshot failed: %s", e)
        return png_bytes


def _dashboard_viewport_screenshot(page) -> bytes:
    _ensure_grafana_sidebar_collapsed(page)
    _inject_grafana_capture_styles(page)
    _hide_grafana_left_dock_strip(page)
    page.wait_for_timeout(300)
    clip = _grafana_viewport_clip_excluding_nav(page)
    left = int((clip or {}).get("x") or 0)
    if clip and left >= 20:
        log.info(
            "p0 graph screenshot: crop left nav strip x=%s y=%s w=%s h=%s",
            clip.get("x"),
            clip.get("y"),
            clip.get("width"),
            clip.get("height"),
        )
        raw = page.screenshot(full_page=False, type="png", clip=clip)
        return _normalize_screenshot_png(raw)
    if left > 0:
        log.warning(
            "p0 graph screenshot: left nav chrome ~%spx but clip failed — full viewport",
            left,
        )
    raw = page.screenshot(full_page=False, type="png")
    return _normalize_screenshot_png(raw)


def _apply_page_zoom_percent(page, percent: int) -> None:
    """Non top+bottom captures: zoom via expanded Playwright viewport."""
    _clear_capture_zoom_styles(page)
    pct = int(percent or 100)
    if pct > 0 and pct < 100:
        log.info(
            "p0 graph screenshot: zoom=%s%% via expanded viewport (no CSS zoom)",
            pct,
        )


def _uses_top_and_bottom_framing() -> bool:
    from p0_logic import config as _config

    return _config.get_p0_graph_screenshot_viewport_only() and _config.get_p0_graph_screenshot_top_and_bottom()


def _highlight_band_capture_mode() -> bool:
    """Two (or three) viewport PNGs from dashboard row bands — per-band waits, not one global chart wait."""
    return _uses_top_and_bottom_framing()


def _band_scroll_settle_ms() -> int:
    return 200 if _effective_fast_capture() else 550


def _band_post_capture_settle_ms() -> int:
    """Short sleep after band panels look ready, before ``screenshot``."""
    from p0_logic import config as _config

    wait_ms = _config.get_p0_graph_screenshot_wait_ms()
    if _effective_fast_capture():
        return min(wait_ms, 800)
    if _highlight_band_capture_mode():
        if _effective_fast_capture():
            return min(wait_ms, 2000)
        return min(wait_ms, 12000)
    return min(wait_ms, 8000)


def _post_nav_settle_ms() -> int:
    """Sleep after panel/grid waits, before kiosk/zoom and band capture."""
    from p0_logic import config as _config

    wait_ms = _config.get_p0_graph_screenshot_wait_ms()
    if _effective_fast_capture():
        return min(wait_ms, 1200)
    if not _highlight_band_capture_mode():
        return wait_ms
    return min(wait_ms, 5000)


def _prepare_grafana_dashboard_for_capture(page, dashboard_url: str) -> None:
    """Kiosk re-goto, undock nav, apply zoom — before screenshots."""
    from p0_logic import config as _config

    kiosk_on = _config.get_p0_graph_screenshot_append_kiosk()
    u = _prepare_grafana_capture_url((dashboard_url or "").strip(), kiosk=kiosk_on)
    if u and kiosk_on:
        cur = (page.url or "").strip().lower()
        need_goto = "kiosk" not in cur or _grafana_sidebar_likely_open(page)
        if need_goto:
            try:
                _preset_grafana_nav_local_storage(page)
                log.info("p0 graph screenshot: force kiosk re-open (Grafana 11 dock hide)")
                page.goto(u, wait_until="load", timeout=90_000)
                page.wait_for_timeout(1200)
            except Exception as e:
                log.warning("p0 graph screenshot: kiosk re-goto failed: %s", e)
        else:
            log.info("p0 graph screenshot: already on kiosk URL — skip re-goto")
    top_bottom = _uses_top_and_bottom_framing()
    try:
        base_w = _config.get_p0_graph_screenshot_viewport_width()
        base_h = _config.get_p0_graph_screenshot_viewport_height()
        zoom_pct = _config.get_p0_graph_screenshot_zoom_percent()
        page.set_viewport_size({"width": base_w, "height": base_h})
        log.info(
            "p0 graph screenshot: viewport=%sx%s browser_zoom=%s%% top+bottom=%s",
            base_w,
            base_h,
            zoom_pct,
            top_bottom,
        )
    except Exception as e:
        log.debug("p0 graph screenshot: set_viewport_size: %s", e)
    _ensure_grafana_sidebar_collapsed(page)
    _apply_browser_page_zoom(page, _config.get_p0_graph_screenshot_zoom_percent())
    _ensure_grafana_sidebar_collapsed(page)
    _inject_grafana_capture_styles(page)
    _hide_grafana_left_dock_strip(page)
    page.wait_for_timeout(1000)


def _measure_clip_rect(page, selector: str) -> Optional[Dict[str, int]]:
    """
    Rectangle in **document / layout** pixels for ``page.screenshot(full_page=True, clip=…)``.
    """
    try:
        raw = page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (!el) return null;
              const r = el.getBoundingClientRect();
              const sx = window.scrollX || 0;
              const sy = window.scrollY || 0;
              const x = Math.max(0, Math.floor(sx + r.left));
              const y = Math.max(0, Math.floor(sy + r.top));
              const rW = Math.ceil(r.width);
              const rH = Math.ceil(r.height);
              let w = Math.max(rW, Math.ceil(el.scrollWidth || 0));
              let h = Math.max(rH, Math.ceil(el.scrollHeight || 0));
              if (h < 120) h = rH;
              if (w < 80 || h < 80) return null;
              return { x, y, width: w, height: h };
            }""",
            selector,
        )
    except Exception as e:
        log.debug("p0 graph screenshot: clip measure failed for %r: %s", selector, e)
        return None
    if not raw or not isinstance(raw, dict):
        return None
    try:
        return {
            "x": int(raw["x"]),
            "y": int(raw["y"]),
            "width": int(raw["width"]),
            "height": int(raw["height"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _visible_clip_substantial(
    vis: Dict[str, int], viewport_w: int, viewport_h: int
) -> bool:
    """
    Grafana's first ``querySelector('main .scrollbar-view')`` often hits a **narrow** scroller (e.g.
    ~300–400px wide on the right) — not the dashboard canvas — producing **blank gray** PNGs.
    Require a minimum visible footprint relative to the Playwright viewport.
    """
    w = int(vis.get("width") or 0)
    h = int(vis.get("height") or 0)
    vw = max(int(viewport_w or 0), 320)
    vh = max(int(viewport_h or 0), 240)
    min_w = max(480, vw // 3)
    min_h = max(260, vh // 5)
    if w < min_w or h < min_h:
        return False
    return True


def _pick_dashboard_clip(
    page,
    selectors: List[str],
    viewport_w: int,
    viewport_h: int,
) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    for sel in selectors:
        clip = _measure_clip_rect(page, sel)
        vis = _measure_visible_clip_rect(page, sel)
        if not clip or not vis:
            continue
        if not _visible_clip_substantial(vis, viewport_w, viewport_h):
            log.info(
                "p0 graph screenshot: clip selector %r visible box=%s too small vs viewport %sx%s — trying next",
                sel,
                vis,
                viewport_w,
                viewport_h,
            )
            continue
        log.info(
            "p0 graph screenshot: using clip selector %r box=%s visible=%s",
            sel,
            clip,
            vis,
        )
        return clip, sel
    log.info(
        "p0 graph screenshot: no clip selector with substantial visible area — full viewport/page capture"
    )
    return None, None


def _measure_visible_clip_rect(page, selector: str) -> Optional[Dict[str, int]]:
    """
    Visible client box for an element (no ``scrollHeight`` inflation).
    Use for screenshots after scrolling **inside** a virtualized Grafana ``.scrollbar-view``.
    """
    try:
        raw = page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (!el) return null;
              const r = el.getBoundingClientRect();
              const sx = window.scrollX || 0;
              const sy = window.scrollY || 0;
              const x = Math.max(0, Math.floor(sx + r.left));
              const y = Math.max(0, Math.floor(sy + r.top));
              const w = Math.max(Math.ceil(r.width), 80);
              const h = Math.max(Math.ceil(el.clientHeight || r.height), 80);
              if (w < 80 || h < 80) return null;
              return { x, y, width: w, height: h };
            }""",
            selector,
        )
    except Exception as e:
        log.debug("p0 graph screenshot: visible clip measure failed for %r: %s", selector, e)
        return None
    if not raw or not isinstance(raw, dict):
        return None
    try:
        return {
            "x": int(raw["x"]),
            "y": int(raw["y"]),
            "width": int(raw["width"]),
            "height": int(raw["height"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _scrollbar_virtualized_metrics(
    page, selector: str
) -> Optional[Tuple[int, int]]:
    """``(scrollHeight, clientHeight)`` for element, or ``None``."""
    try:
        raw = page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (!el) return null;
              return [Math.ceil(el.scrollHeight || 0), Math.ceil(el.clientHeight || 0)];
            }""",
            selector,
        )
    except Exception:
        return None
    if (
        not raw
        or not isinstance(raw, (list, tuple))
        or len(raw) != 2
    ):
        return None
    try:
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def _mark_best_scroll_target_under_root(page, root_selector: str) -> str:
    """
    Grafana nests several ``.scrollbar-view`` / scroll regions. The first match from config is often
    an **outer** wrapper with ``scrollHeight ≈ clientHeight`` while a **child** holds the virtualized
    table (huge ``scrollHeight``). Mark the descendant with the largest ``scrollHeight - clientHeight``
    and return a stable selector; otherwise return ``root_selector``.
    """
    try:
        placed = page.evaluate(
            """(rootSel) => {
              const root = document.querySelector(rootSel);
              if (!root) return false;
              document.querySelectorAll('[data-p0-capture-scroll]').forEach((e) =>
                e.removeAttribute('data-p0-capture-scroll')
              );
              const nodes = [root];
              root.querySelectorAll('.scrollbar-view, .scrollbar__view').forEach((n) =>
                nodes.push(n)
              );
              let best = null;
              let bestDelta = -1;
              for (const e of nodes) {
                const ch = Math.ceil(e.clientHeight || 0);
                const sh = Math.ceil(e.scrollHeight || 0);
                const d = sh - ch;
                if (ch >= 100 && d > bestDelta) {
                  bestDelta = d;
                  best = e;
                }
              }
              if (best && bestDelta >= 40) {
                best.setAttribute('data-p0-capture-scroll', '1');
                return true;
              }
              return false;
            }""",
            root_selector,
        )
    except Exception as e:
        log.debug("p0 graph screenshot: scroll-target mark failed: %s", e)
        return root_selector
    if placed:
        log.info(
            "p0 graph screenshot: nested scroll — using descendant with largest overflow (marked data-p0-capture-scroll)"
        )
        return "[data-p0-capture-scroll='1']"
    return root_selector


def _clear_p0_dash_page_scroll_marks(page) -> None:
    try:
        page.evaluate(
            """() => {
              document.querySelectorAll('[data-p0-dash-page-scroll]').forEach((e) =>
                e.removeAttribute('data-p0-dash-page-scroll')
              );
            }"""
        )
    except Exception:
        pass


def _mark_wide_dashboard_scroll_container(page) -> bool:
    """
    Tag the **widest** scrollable ``.scrollbar-view`` under ``main`` (dashboard body), not a narrow
    table scroller — used to page down for a second viewport screenshot.
    """
    try:
        return bool(
            page.evaluate(
                """() => {
              document.querySelectorAll('[data-p0-dash-page-scroll]').forEach((e) =>
                e.removeAttribute('data-p0-dash-page-scroll')
              );
              const main = document.querySelector('main') || document.body;
              if (!main) return false;
              const vw = window.innerWidth || 1280;
              const minW = Math.max(480, Math.floor(vw * 0.42));
              let best = null;
              let bestArea = -1;
              main.querySelectorAll('.scrollbar-view, .scrollbar__view').forEach((el) => {
                const ch = Math.ceil(el.clientHeight || 0);
                const sh = Math.ceil(el.scrollHeight || 0);
                if (sh <= ch + 12) return;
                const cw = Math.ceil(el.clientWidth || 0);
                if (cw < minW) return;
                const area = cw * ch;
                if (area > bestArea) {
                  bestArea = area;
                  best = el;
                }
              });
              if (best) {
                best.setAttribute('data-p0-dash-page-scroll', '1');
                return true;
              }
              const m = document.querySelector('main');
              if (m) {
                const mch = Math.ceil(m.clientHeight || 0);
                const msh = Math.ceil(m.scrollHeight || 0);
                const mcw = Math.ceil(m.clientWidth || 0);
                if (msh > mch + 12 && mcw >= minW) {
                  m.setAttribute('data-p0-dash-page-scroll', '1');
                  return true;
                }
              }
              return false;
            }"""
            )
        )
    except Exception as e:
        log.debug("p0 graph screenshot: wide dashboard scroll mark failed: %s", e)
        return False


def _scroll_pair_reset_top(page, scroll_sel: Optional[str]) -> None:
    try:
        page.evaluate(
            """() => {
              window.scrollTo(0, 0);
              const se = document.scrollingElement;
              if (se) se.scrollTop = 0;
            }"""
        )
        if scroll_sel:
            page.evaluate(
                """(sel) => {
                  const e = document.querySelector(sel);
                  if (e) e.scrollTop = 0;
                }""",
                scroll_sel,
            )
    except Exception:
        pass


def _compute_next_viewport_scroll_delta(
    page, scroll_sel: Optional[str], viewport_h: int
) -> int:
    """How many px to scroll **from current** position (element or window), capped ~0.92× viewport height."""
    try:
        raw = page.evaluate(
            """({ sel }) => {
              if (sel) {
                const e = document.querySelector(sel);
                if (!e) return 0;
                const st = Math.ceil(e.scrollTop || 0);
                const ch = Math.ceil(e.clientHeight || 0);
                const sh = Math.ceil(e.scrollHeight || 0);
                const maxS = Math.max(0, sh - ch);
                const room = maxS - st;
                if (room <= 4) return 0;
                const step = Math.max(Math.floor(ch * 0.92), 1);
                return Math.min(room, step);
              }
              const se = document.scrollingElement || document.documentElement;
              const st = Math.ceil(window.scrollY || se.scrollTop || 0);
              const ih = Math.ceil(window.innerHeight || 720);
              const sh = Math.max(
                Math.ceil(se.scrollHeight || 0),
                document.body ? Math.ceil(document.body.scrollHeight || 0) : 0
              );
              const maxS = Math.max(0, sh - ih);
              const room = maxS - st;
              if (room <= 4) return 0;
              const step = Math.max(Math.floor(ih * 0.92), 1);
              return Math.min(room, step);
            }""",
            {"sel": scroll_sel},
        )
        return max(0, int(raw))
    except Exception:
        return 0


def _apply_viewport_pair_scroll(page, scroll_sel: Optional[str], delta: int) -> None:
    if delta <= 0:
        return
    if scroll_sel:
        page.evaluate(
            """({ sel, d }) => {
              const e = document.querySelector(sel);
              if (!e) return;
              const maxTop = Math.max(0, (e.scrollHeight || 0) - (e.clientHeight || 0));
              e.scrollTop = Math.min(maxTop, (e.scrollTop || 0) + d);
            }""",
            {"sel": scroll_sel, "d": delta},
        )
    else:
        page.evaluate("(d) => window.scrollBy(0, d)", delta)


def _viewport_scroll_chain_screenshots(
    page,
    viewport_h: int,
    num_pages: int,
) -> List[bytes]:
    """
    ``num_pages`` full-viewport PNGs: scroll top first, then ~one viewport steps. Stops early if no
    more scroll room. Fixes **incremental** scroll (each step from current ``scrollTop``).
    """
    n = max(1, min(int(num_pages), 8))
    scroll_sel: Optional[str] = None
    pngs: List[bytes] = []
    try:
        _clear_p0_dash_page_scroll_marks(page)
        if _mark_wide_dashboard_scroll_container(page):
            scroll_sel = "[data-p0-dash-page-scroll='1']"
        _scroll_pair_reset_top(page, scroll_sel)
        page.wait_for_timeout(420)
        for i in range(n):
            shot = page.screenshot(full_page=False, type="png")
            if shot:
                pngs.append(shot)
            if i >= n - 1:
                break
            delta = _compute_next_viewport_scroll_delta(page, scroll_sel, viewport_h)
            if delta <= 0:
                log.info(
                    "p0 graph screenshot: viewport scroll chain stops after %s page(s) — no more overflow",
                    len(pngs),
                )
                break
            log.info(
                "p0 graph screenshot: viewport scroll chain step %s/%s delta=%s sel=%s",
                i + 1,
                n,
                delta,
                scroll_sel or "window",
            )
            _apply_viewport_pair_scroll(page, scroll_sel, delta)
            page.wait_for_timeout(680)
        if len(pngs) >= 2:
            log.info(
                "p0 graph screenshot: viewport scroll chain captured %s page(s) (requested %s)",
                len(pngs),
                n,
            )
        elif n >= 2 and len(pngs) < 2:
            log.info(
                "p0 graph screenshot: viewport scroll chain got %s page(s) — may fallback to Pillow",
                len(pngs),
            )
        return pngs
    except Exception as e:
        log.warning("p0 graph screenshot: viewport scroll chain failed: %s", e)
        return pngs
    finally:
        _scroll_pair_reset_top(page, scroll_sel)
        _clear_p0_dash_page_scroll_marks(page)


def _scroll_to_max(page, scroll_sel: Optional[str]) -> None:
    try:
        page.evaluate(
            """(sel) => {
              const capScroll = (el) => {
                if (!el) return;
                let maxTop = Math.max(0, (el.scrollHeight || 0) - (el.clientHeight || 0));
                const grid = document.querySelector(
                  '[data-testid="dashboard-layout-grid"], .react-grid-layout, .dashboard-grid'
                );
                if (grid) {
                  const er = el.getBoundingClientRect();
                  const gr = grid.getBoundingClientRect();
                  const gridBottom = (el.scrollTop || 0) + (gr.bottom - er.top);
                  const target = Math.max(0, gridBottom - (el.clientHeight || 0) + 24);
                  maxTop = Math.min(maxTop, target);
                }
                el.scrollTop = maxTop;
              };
              if (sel) {
                const e = document.querySelector(sel);
                if (e) {
                  capScroll(e);
                  return;
                }
              }
              const se = document.scrollingElement || document.documentElement;
              const sh = Math.max(
                se ? se.scrollHeight : 0,
                document.body ? document.body.scrollHeight : 0
              );
              const ih = window.innerHeight || 720;
              const grid = document.querySelector(
                '[data-testid="dashboard-layout-grid"], .react-grid-layout'
              );
              let maxY = Math.max(0, sh - ih);
              if (grid) {
                const gr = grid.getBoundingClientRect();
                const target = Math.max(0, gr.bottom + window.scrollY - ih + 24);
                maxY = Math.min(maxY, target);
              }
              window.scrollTo(0, maxY);
              if (se) se.scrollTop = maxY;
            }""",
            scroll_sel,
        )
    except Exception:
        pass


def _scroll_toolbar_and_apisix_into_view(page) -> None:
    """Pic 1: top of page + full **Apisix Error Count** panel visible (not clipped by scroll)."""
    try:
        page.evaluate(
            """() => {
              window.scrollTo(0, 0);
              const sy = window.scrollY || 0;
              const vh = window.innerHeight || 1080;
              const toolbarPad = 58;
              let apisix = null;
              document.querySelectorAll(
                '[data-panelid], .react-grid-item, [data-panel-id]'
              ).forEach((el) => {
                const t = (el.innerText || '').trim().slice(0, 120);
                if (!/apisix\\s*error/i.test(t)) return;
                const r = el.getBoundingClientRect();
                if (r.width < 120 || r.height < 40) return;
                if (!apisix || r.height > apisix.h) {
                  apisix = { top: sy + r.top, h: r.height, bottom: sy + r.bottom };
                }
              });
              if (!apisix) return;
              if (apisix.h + toolbarPad <= vh - 12) {
                const target = Math.max(0, apisix.top - toolbarPad);
                window.scrollTo(0, target);
              } else {
                window.scrollTo(0, Math.max(0, apisix.top - toolbarPad));
              }
            }"""
        )
    except Exception as e:
        log.debug("p0 graph screenshot: apisix scroll into view: %s", e)


def _measure_grafana_highlight_band_clips(page, *, include_login_panel: bool = True) -> List[Dict[str, int]]:
    """
    Two bands (``include_login_panel=False``, default VNC-style):
      Band 1 = Apisix/KPI/FPMS (stops before Login / CPMS).
      Band 2 = **CPMS/IGO/Pulsar** only (scroll starts at CPMS header — matches manual refs).
    Three bands (``include_login_panel=True``):
      Band 1 = top block; band 2 = CPMS/IGO/Pulsar; band 3 = Login panel only.
    """
    from p0_logic import config as _config

    try:
        raw = page.evaluate(
            """({ includeLogin, includeTimeBar }) => {
              const sx = window.scrollX || 0;
              const sy = window.scrollY || 0;
              const vw = window.innerWidth || 1920;
              let left = 0;
              document.querySelectorAll(
                'nav, aside, [role="navigation"], [class*="NavMenu"], [class*="DockMenu"], [class*="SideNav"]'
              ).forEach((el) => {
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return;
                const r = el.getBoundingClientRect();
                if (r.left > 32 || r.width < 16 || r.height > 120) return;
                left = Math.max(left, Math.ceil(r.right));
              });
              left = Math.min(left, Math.floor(vw * 0.28));

              const panelNodes = Array.from(document.querySelectorAll(
                '[data-panelid], .react-grid-item, [data-panel-id]'
              ));
              const panels = [];
              const loginTitleRe = /login\\s*(with\\s*)?password/i;
              const isLoginTitle = (t) => loginTitleRe.test(t || '');
              for (const el of panelNodes) {
                const r = el.getBoundingClientRect();
                const title = (el.innerText || '').trim().slice(0, 240);
                const minPanelH = isLoginTitle(title) ? 20 : 40;
                if (r.width < 48 || r.height < minPanelH) continue;
                if (r.right <= left + 8) continue;
                panels.push({
                  top: sy + r.top,
                  bottom: sy + r.bottom,
                  left: sx + Math.max(r.left, left),
                  right: sx + r.right,
                  title,
                });
              }
              if (panels.length < 2) return null;

              const findSplitY = (re, maxLen) => {
                // Prefer Grafana row-TITLE elements (short own text). Generic div/span containers
                // include every nested panel's text, blow past maxLen, and get skipped — which is
                // why detection failed and we fell back to a 52% half-cut. Among all matches, pick
                // the SHORTEST-text one (that's the title, not a wrapper).
                const sel = [
                  'a[class*="row__title"]', '[class*="row__title"]', '[class*="row-title"]',
                  '[data-testid*="row-title" i]', '[data-testid*="dashboard-row" i]',
                  '[class*="dashboard-row"]', 'h2', 'h3', 'button', 'span', 'div'
                ].join(', ');
                let bestY = null, bestLen = Infinity;
                for (const el of document.querySelectorAll(sel)) {
                  const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
                  if (!t || t.length > maxLen) continue;
                  if (!re.test(t)) continue;
                  const r = el.getBoundingClientRect();
                  if (r.height > 180 || r.width < 16) continue;
                  if (t.length < bestLen) { bestLen = t.length; bestY = sy + r.top - 4; }
                }
                return bestY;
              };

              const loginRe = /login\\s*(with\\s*)?password/i;
              const cpmsRe = /CPMS1\\.0|CPMS2\\.0|CPMS\\s*1\\.0|CPMS1\\.0\\s*\\/\\s*CPMS2\\.0/i;

              let loginSplit = findSplitY(loginRe, 120);
              let cpmsSplit = findSplitY(cpmsRe, 120);
              let cpmsFromHeader = cpmsSplit != null;

              if (cpmsSplit == null) {
                const grid = document.querySelector(
                  '[data-testid="dashboard-layout-grid"], .react-grid-layout'
                );
                if (grid) {
                  const gr = grid.getBoundingClientRect();
                  cpmsSplit = sy + gr.top + gr.height * 0.52;
                }
              }
              if (cpmsSplit == null) return null;

              if (loginSplit == null) {
                for (const p of panels) {
                  if (isLoginTitle(p.title) && p.title.length < 200) {
                    loginSplit = p.top - 4;
                    break;
                  }
                }
              }
              if (loginSplit == null) {
                for (const el of panelNodes) {
                  const t = (el.innerText || '').trim();
                  if (!isLoginTitle(t)) continue;
                  const r = el.getBoundingClientRect();
                  if (r.width < 48 || r.height < 16) continue;
                  loginSplit = sy + r.top - 4;
                  break;
                }
              }

              const union = (list) => {
                if (!list.length) return null;
                let x0 = list[0].left, y0 = list[0].top, x1 = list[0].right, y1 = list[0].bottom;
                for (const p of list.slice(1)) {
                  x0 = Math.min(x0, p.left);
                  y0 = Math.min(y0, p.top);
                  x1 = Math.max(x1, p.right);
                  y1 = Math.max(y1, p.bottom);
                }
                return { x0, y0, x1, y1 };
              };

              const isLoginPanel = (p) => isLoginTitle(p.title) && p.title.length < 240;
              const band1End = (loginSplit != null && loginSplit < cpmsSplit)
                ? loginSplit
                : cpmsSplit;

              const topPanels = panels.filter(
                (p) => !isLoginPanel(p) && p.bottom <= band1End + 4
              );
              // VNC 2nd PNG: always anchor at CPMS (not Login) so CPMS→Pulsar fits one viewport at 50% zoom.
              const band2Start = cpmsSplit;
              const midPanels = panels.filter(
                (p) => !isLoginPanel(p) && p.top >= cpmsSplit - 6
              );
              const loginPanels = panels.filter((p) => isLoginPanel(p));

              const pad = 8;
              const mk = (u, minH) => ({
                x: Math.max(left, Math.floor(u.x0 - pad)),
                y: Math.max(0, Math.floor(u.y0 - pad)),
                width: Math.max(320, Math.ceil(u.x1 - u.x0 + pad * 2)),
                height: Math.max(minH, Math.ceil(u.y1 - u.y0 + pad * 2)),
              });

              // HARD split on the CPMS header: band1 = content top → CPMS, band2 = CPMS → content
              // bottom. Robust vs per-panel unions, which go short (lazy render) or overshoot past
              // 9280 into CPMS/IGO. Full page is pre-warmed, so content extents are accurate.
              const contentPanels = panels.filter((p) => !isLoginPanel(p));
              const uAll = union(contentPanels.length ? contentPanels : panels);
              if (!uAll) return null;
              const outLeft = Math.max(left, Math.floor(uAll.x0 - pad));
              const outWidth = Math.max(320, Math.ceil(uAll.x1 - uAll.x0 + pad * 2));
              const b1y = Math.max(0, Math.floor(uAll.y0 - pad));
              const band1 = {
                x: outLeft, y: b1y, width: outWidth,
                height: Math.max(200, Math.ceil(cpmsSplit - b1y)),
              };
              const band2 = {
                x: outLeft, y: Math.max(0, Math.floor(cpmsSplit)), width: outWidth,
                height: Math.max(200, Math.ceil(uAll.y1 + pad - cpmsSplit)),
              };

              const out = {
                loginSplit,
                cpmsSplit,
                cpmsFromHeader,
                band1,
                band2,
                band3: null,
              };

              if (includeTimeBar) {
                const grid = document.querySelector(
                  '[data-testid="dashboard-layout-grid"], .react-grid-layout'
                );
                if (grid) {
                  const gr = grid.getBoundingClientRect();
                  const toolY = Math.max(0, Math.floor(sy + gr.top - 56));
                  out.band1.y = Math.min(out.band1.y, toolY);
                }
                let ax = null;
                for (const p of panels) {
                  if (!/apisix\\s*error/i.test(p.title || '')) continue;
                  if (!ax || (p.bottom - p.top) > (ax.bottom - ax.top)) ax = p;
                }
                if (ax) {
                  out.band1.y = Math.min(out.band1.y, Math.max(0, Math.floor(ax.top - 12)));
                }
                // Keep band1's BOTTOM pinned at the CPMS split (top moved up for the toolbar,
                // so grow height to still end exactly at CPMS — never cut 9280).
                out.band1.height = Math.max(200, Math.ceil(cpmsSplit - out.band1.y));
              }

              if (includeLogin && loginPanels.length) {
                const u3 = union(loginPanels);
                if (u3) out.band3 = mk(u3, 72);
              }
              return out;
            }""",
            {
                "includeLogin": bool(include_login_panel),
                "includeTimeBar": bool(_config.get_p0_graph_screenshot_include_time_bar()),
            },
        )
    except Exception as e:
        log.debug("p0 graph screenshot: highlight band measure failed: %s", e)
        return []
    if not raw or not isinstance(raw, dict):
        return []
    out: List[Dict[str, int]] = []
    try:
        keys = ("band1", "band2")
        if include_login_panel and raw.get("band3"):
            keys = ("band1", "band2", "band3")
        for key in keys:
            band = raw.get(key)
            if not band or not isinstance(band, dict):
                if key == "band3":
                    continue
                return []
            w = int(band.get("width") or 0)
            h = int(band.get("height") or 0)
            min_h = 72 if key == "band3" else 200
            if w < 200 or h < min_h:
                if key == "band3":
                    continue
                return []
            out.append({
                "x": int(band.get("x") or 0),
                "y": int(band.get("y") or 0),
                "width": w,
                "height": h,
            })
        if len(out) < 2:
            return []
        cpms_from_header = bool(raw.get("cpmsFromHeader"))
        log.info(
            "p0 graph screenshot: highlight bands login_y=%s cpms_y=%s cpms_from_header=%s parts=%s sizes=%s",
            raw.get("loginSplit"),
            raw.get("cpmsSplit"),
            cpms_from_header,
            len(out),
            [(c["width"], c["height"]) for c in out],
        )
        if not cpms_from_header:
            log.warning(
                "p0 graph screenshot: CPMS header NOT found — using 52%% geometric half-split "
                "(bands will overlap / not be section-aligned). Check the row title regex vs the dashboard."
            )
        return out
    except (TypeError, ValueError):
        return []


def _band_panel_wait_timeout_ms() -> int:
    from p0_logic import config as _config

    if _is_on_demand_capture() or _effective_fast_capture():
        return _config.get_p0_graph_screenshot_on_demand_band_max_wait_ms()
    cap = _config.get_p0_graph_screenshot_band_max_wait_ms()
    t = _config.get_p0_graph_screenshot_panel_content_ready_timeout_ms()
    if t > 0:
        return min(t, cap)
    pr = _config.get_p0_graph_screenshot_panel_ready_timeout_ms()
    if pr > 0:
        return min(max(20_000, pr + 10_000), cap)
    return min(35_000, cap)


def _band_panel_ready_ratio() -> float:
    from p0_logic import config as _config

    ratio = _config.get_p0_graph_screenshot_band_panel_ready_ratio()
    if _effective_fast_capture():
        return min(ratio, 0.72)
    return ratio


def _band_panel_ready_opts(*, bottom_zone_only: bool = False) -> Dict[str, object]:
    from p0_logic import config as _config

    return {
        "minDoneRatio": _band_panel_ready_ratio(),
        "maxBlank": _config.get_p0_graph_screenshot_band_max_blank_panels(),
        "bottomZoneOnly": bottom_zone_only,
        "bottomZoneFraction": 0.38,
    }


_BAND_PANEL_READY_JS = r"""
(args) => {
  const clip = args.clip || {};
  const opts = args.opts || {};
  const minRatio = typeof opts.minDoneRatio === 'number' ? opts.minDoneRatio : 0.88;
  const maxBlank = typeof opts.maxBlank === 'number' ? opts.maxBlank : 0;
  const bottomOnly = !!opts.bottomZoneOnly;
  const bottomFrac = typeof opts.bottomZoneFraction === 'number' ? opts.bottomZoneFraction : 0.38;
  const sy = window.scrollY || 0;
  const vh = window.innerHeight || 1080;
  const y0 = clip.y || 0;
  const y1 = (clip.y || 0) + (clip.height || vh);
  const viewBot = sy + vh;
  const bottomZoneTop = sy + Math.floor(vh * (1 - bottomFrac));
  const nodes = document.querySelectorAll(
    '[data-panelid], .react-grid-item, [data-panel-id], [data-viz-key]'
  );
  let total = 0;
  let done = 0;
  let loading = 0;
  let blank = 0;
  const panelState = (el) => {
    if (
      el.querySelector(
        '[class*="spinner"], [class*="Spinner"], [class*="loading"], '
        + '[class*="PanelLoading"], [class*="panel-loading"], '
        + '[aria-busy="true"], [role="progressbar"], '
        + '[data-testid*="panel-loader"], [data-testid*="PanelLoading"]'
      )
    ) {
      return 'loading';
    }
    const content = el.querySelector(
      '[class*="panel-content"], [class*="panel-content"]'
    ) || el;
    let canv = 0;
    let svgBig = 0;
    content.querySelectorAll('canvas').forEach((c) => {
      const cr = c.getBoundingClientRect();
      if (cr.width > 48 && cr.height > 24) canv++;
    });
    content.querySelectorAll('svg').forEach((s) => {
      const sr = s.getBoundingClientRect();
      if (sr.width > 36 && sr.height > 20) svgBig++;
    });
    if (canv > 0 || svgBig > 0) return 'chart';
    const bodyText = (content.innerText || '').trim();
    if (/no data|无数据|暂无数据|没有数据/i.test(bodyText)) return 'nodata';
    const rows = content.querySelectorAll(
      'table tbody tr, [role="rowgroup"] [role="row"]'
    ).length;
    if (rows >= 2) return 'table';
    const cr = content.getBoundingClientRect();
    if (cr.height > 44 && cr.width > 72) return 'blank';
    return 'small';
  };
  nodes.forEach((el) => {
    const r = el.getBoundingClientRect();
    const docTop = sy + r.top;
    const docBot = sy + r.bottom;
    if (docBot < y0 + 12 || docTop > y1 - 12) return;
    if (bottomOnly) {
      if (docBot < bottomZoneTop || docTop > viewBot) return;
    }
    if (r.width < 72 || r.height < 36) return;
    const st = panelState(el);
    if (st === 'small') return;
    total++;
    if (st === 'loading') loading++;
    else if (st === 'blank') blank++;
    else done++;
  });
  if (total < 1) return false;
  if (loading > 0) return false;
  if (blank > maxBlank) return false;
  const need = Math.max(1, Math.ceil(total * minRatio));
  return done >= need;
}
"""


def _evaluate_band_panels_ready(page, doc_clip: Dict[str, int], *, bottom_zone_only: bool = False) -> bool:
    try:
        return bool(
            page.evaluate(
                _BAND_PANEL_READY_JS,
                {"clip": doc_clip, "opts": _band_panel_ready_opts(bottom_zone_only=bottom_zone_only)},
            )
        )
    except Exception:
        return False


def _wait_for_charts_in_document_band(page, doc_clip: Dict[str, int], *, timeout_ms: int = 0) -> bool:
    """
    Wait until viewport panels are loaded: no spinners, no black blanks, and most panels show
    chart / table / stable ``No data``. Returns ``True`` if panels became ready, ``False`` if the
    wait timed out (caller uses this to decide whether a resulting blank frame is retryable).
    """
    if timeout_ms <= 0:
        timeout_ms = _band_panel_wait_timeout_ms()
    if timeout_ms <= 0:
        return True
    ratio = _band_panel_ready_ratio()
    log.info(
        "p0 graph screenshot: waiting for band panels (max %ss, need %.0f%% ready, blank=0)…",
        timeout_ms // 1000,
        ratio * 100,
    )
    try:
        page.wait_for_function(
            _BAND_PANEL_READY_JS,
            arg={"clip": doc_clip, "opts": _band_panel_ready_opts(bottom_zone_only=False)},
            timeout=timeout_ms,
            polling=350,
        )
        log.info(
            "p0 graph screenshot: band viewport panels ready (waited up to %sms)",
            timeout_ms,
        )
        return True
    except Exception as e:
        log.warning(
            "p0 graph screenshot: band panel wait timed out — may have blank panels: %s",
            e,
        )
        return False


def _band_stable_poll_settings() -> Tuple[int, int]:
    from p0_logic import config as _config

    if _effective_fast_capture():
        return (
            _config.get_p0_graph_screenshot_on_demand_band_stable_polls(),
            min(_config.get_p0_graph_screenshot_band_stable_poll_ms(), 700),
        )
    return (
        _config.get_p0_graph_screenshot_band_stable_polls(),
        _config.get_p0_graph_screenshot_band_stable_poll_ms(),
    )


def _wait_for_band_panels_stable(page, doc_clip: Dict[str, int], *, timeout_ms: int = 0) -> bool:
    """Require several consecutive ready checks so Grafana does not capture mid-paint. Returns
    ``True`` when the required consecutive-ready streak was reached, ``False`` on timeout."""
    polls_need, poll_ms = _band_stable_poll_settings()
    if timeout_ms <= 0:
        timeout_ms = min(18_000, max(6000, polls_need * poll_ms * 4))
    deadline = time.time() + (timeout_ms / 1000.0)
    streak = 0
    while time.time() < deadline:
        if _evaluate_band_panels_ready(page, doc_clip):
            streak += 1
            if streak >= polls_need:
                log.info("p0 graph screenshot: band panels stable (%s checks)", streak)
                return True
        else:
            streak = 0
        page.wait_for_timeout(poll_ms)
    log.warning(
        "p0 graph screenshot: band stability wait timed out (needed %s consecutive ready polls)",
        polls_need,
    )
    return False


def _wait_for_viewport_bottom_row_ready(page, doc_clip: Dict[str, int], *, timeout_ms: int) -> bool:
    """Band 2: bottom row (Pulsar) loads last — same strict rules in the lower viewport. Returns
    ``True`` if the bottom row became ready, ``False`` on timeout."""
    if timeout_ms <= 0:
        return True
    log.info(
        "p0 graph screenshot: waiting for bottom-row panels (max %ss)…",
        timeout_ms // 1000,
    )
    try:
        page.wait_for_function(
            _BAND_PANEL_READY_JS,
            arg={"clip": doc_clip, "opts": _band_panel_ready_opts(bottom_zone_only=True)},
            timeout=timeout_ms,
            polling=350,
        )
        log.info("p0 graph screenshot: bottom-row panels ready in viewport")
        return True
    except Exception as e:
        log.warning("p0 graph screenshot: bottom-row wait: %s", e)
        return False


def _scroll_viewport_to_paint_lazy_panels(page, band_y: int) -> None:
    """Step through the viewport so below-the-fold panels start Grafana queries before we wait."""
    from p0_logic import config as _config

    vh = _config.get_p0_graph_screenshot_viewport_height()
    step_ms = 320 if _effective_fast_capture() else 620
    base = max(0, int(band_y or 0))
    if _is_on_demand_capture() and _effective_fast_capture():
        offsets = [0, int(vh * 0.55), 0]
    else:
        offsets = [0, int(vh * 0.28), int(vh * 0.55), int(vh * 0.82), 0]
    try:
        for off in offsets:
            page.evaluate("(y) => window.scrollTo(0, Math.max(0, y))", base + off)
            page.wait_for_timeout(step_ms)
        page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 8))", base)
        page.wait_for_timeout(step_ms // 2)
        log.info("p0 graph screenshot: lazy-panel viewport scroll at y=%s", base)
    except Exception as e:
        log.debug("p0 graph screenshot: lazy viewport scroll: %s", e)


def _warm_band_lazy_panels(page, doc_clip: Dict[str, int]) -> None:
    """Scroll through the band so off-screen / lazy panels (e.g. Pulsar row) start queries."""
    from p0_logic import config as _config

    try:
        y = int(doc_clip.get("y") or 0)
        h = int(doc_clip.get("height") or 0)
        vh = _config.get_p0_graph_screenshot_viewport_height()
        end_y = max(y, y + h - vh)
        if _is_on_demand_capture() and _effective_fast_capture():
            return
        warm_ms = 280 if _effective_fast_capture() else 520
        for sy in (y, end_y, y):
            page.evaluate("(s) => window.scrollTo(0, Math.max(0, s))", sy)
            page.wait_for_timeout(warm_ms)
        log.info(
            "p0 graph screenshot: warmed lazy panels band y=%s..%s (scroll through)",
            y,
            y + h,
        )
    except Exception as e:
        log.debug("p0 graph screenshot: warm band scroll: %s", e)


def _screenshot_viewport_at_band_start(
    page,
    doc_clip: Dict[str, int],
    *,
    is_lower_band: bool = False,
    is_first_band: bool = False,
) -> Optional[bytes]:
    """
    VNC-style: scroll to band top, capture **one viewport** (1920×1080), not the full band height.
    Avoids huge 1800×1300+ PNGs from full_page band clips.
    """
    from p0_logic import config as _config

    try:
        y = int(doc_clip.get("y") or 0)
        h = int(doc_clip.get("height") or 0)
        vh = _config.get_p0_graph_screenshot_viewport_height()
        if is_first_band and _config.get_p0_graph_screenshot_include_time_bar():
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(400)
            _scroll_toolbar_and_apisix_into_view(page)
            y = 0
            log.info(
                "p0 graph screenshot: pic 1/2 — include time bar + full Apisix panel (scroll top)"
            )
        if is_lower_band:
            from p0_logic import config as _cfg_warm

            if _cfg_warm.get_p0_graph_screenshot_band_warm_scroll():
                log.info("p0 graph screenshot: pic 2/2 — warm-scroll lazy panels…")
                _warm_band_lazy_panels(page, doc_clip)
        settle = _band_scroll_settle_ms()
        page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 8))", y)
        page.wait_for_timeout(settle + 120)
        page.evaluate(
            """(y) => {
              window.scrollBy(0, Math.min(400, window.innerHeight * 0.35));
              window.scrollTo(0, Math.max(0, y - 8));
            }""",
            y,
        )
        page.wait_for_timeout(settle)
        vp_band = {
            "x": int(doc_clip.get("x") or 0),
            "y": y,
            "width": int(doc_clip.get("width") or 1920),
            "height": vh,
        }
        _scroll_viewport_to_paint_lazy_panels(page, y)
        base_band_ms = _band_panel_wait_timeout_ms()
        content_cap = _config.get_p0_graph_screenshot_panel_content_ready_timeout_ms()
        if content_cap <= 0:
            content_cap = 90_000
        content_cap = max(content_cap, base_band_ms)
        blank_retry = _config.get_p0_graph_screenshot_blank_retry()
        max_retries = _config.get_p0_graph_screenshot_blank_max_retries() if blank_retry else 0
        band_label = "lower band (Pulsar)" if is_lower_band else "top band (KPI)"

        def _run_readiness_gates(band_ms):
            """Run the band readiness/stability waits. Returns False if any wait timed out."""
            gate_ok = _wait_for_charts_in_document_band(page, vp_band, timeout_ms=band_ms)
            stable_ms = 8_000 if _effective_fast_capture() else 22_000
            if not _wait_for_band_panels_stable(page, vp_band, timeout_ms=stable_ms):
                gate_ok = False
            if is_lower_band:
                if _effective_fast_capture():
                    bottom_ms = min(band_ms, 12_000)
                    stable2_ms = 6_000
                else:
                    bottom_ms = min(band_ms, 45_000)
                    stable2_ms = min(stable_ms, 16_000)
                if not _wait_for_viewport_bottom_row_ready(
                    page,
                    {"x": vp_band["x"], "y": y, "width": vp_band["width"], "height": h},
                    timeout_ms=bottom_ms,
                ):
                    gate_ok = False
                if not _wait_for_band_panels_stable(page, vp_band, timeout_ms=stable2_ms):
                    gate_ok = False
                page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 8))", y)
                page.wait_for_timeout(400 if _effective_fast_capture() else 700)
            extra = _band_post_capture_settle_ms()
            if extra > 0:
                page.wait_for_timeout(extra)
            return gate_ok

        def _capture_band_png():
            """Section-aligned full-height clip (fallback: single viewport). None if all-black."""
            band_h = int(doc_clip.get("height") or 0)
            band_y = int(doc_clip.get("y") or 0)
            if _config.get_p0_graph_screenshot_band_full_height() and band_h > 200:
                # Clip to the EXACT measured band box (section-aligned), whether the band is TALLER
                # or SHORTER than the viewport. A plain viewport shot of a short band overflows into
                # the next section (CPMS header bled into pic 1, 9280 into pic 2). Lazy panels were
                # already warmed above (warm-scroll + _scroll_viewport_to_paint_lazy_panels).
                page.evaluate("(yy) => window.scrollTo(0, Math.max(0, yy - 8))", band_y)
                page.wait_for_timeout(300)
                try:
                    full = page.screenshot(
                        full_page=True,
                        type="png",
                        clip={
                            "x": int(vp_band["x"]),
                            "y": band_y,
                            "width": int(vp_band["width"]),
                            "height": band_h,
                        },
                    )
                except Exception as e_full:
                    log.warning(
                        "p0 graph screenshot: full-height band capture failed (%s) — viewport fallback",
                        e_full,
                    )
                    full = b""
                if full and not _png_bytes_uniformly_blank(full):
                    log.info("p0 graph screenshot: full-height band PNG bytes=%s h=%s", len(full), band_h)
                    return _normalize_screenshot_png(full)
                log.info("p0 graph screenshot: full-height band blank/failed — falling back to single viewport")
            raw = _dashboard_viewport_screenshot(page)
            if raw and not _png_bytes_uniformly_blank(raw):
                return _normalize_screenshot_png(raw)
            return None

        # Bounded blank-retry loop. The common case runs exactly ONE gate+capture and returns — the
        # extra wait/re-capture is only paid when the readiness gate timed out AND the panel region
        # actually came out blank, i.e. the exact failure that was posting empty dashboards.
        attempt = 0
        band_ms = base_band_ms
        while True:
            gate_ok = _run_readiness_gates(band_ms)
            png = _capture_band_png()
            # Region-blank is only consulted when the gate TIMED OUT: a gate that reports ready means
            # the charts painted, so a legitimately dark dashboard is never second-guessed.
            region_blank = (
                png is not None
                and blank_retry
                and not gate_ok
                and _png_panel_region_blank(png)
            )
            is_blank = png is None or region_blank
            if not is_blank:
                return png
            retryable = attempt < max_retries and (png is None or not gate_ok)
            if retryable:
                attempt += 1
                band_ms = min(content_cap, int(band_ms * 1.8) + 4000)
                log.warning(
                    "p0 graph screenshot: %s blank capture detected — retrying "
                    "(attempt %s/%s, band wait -> %sms, gate_ok=%s, cause=%s)",
                    band_label,
                    attempt,
                    max_retries,
                    band_ms,
                    gate_ok,
                    "all-black" if png is None else "unpainted-panel-region",
                )
                _scroll_viewport_to_paint_lazy_panels(page, y)
                continue
            if png is None:
                # Every attempt produced a uniformly-black frame — let the caller fall back.
                return None
            log.warning(
                "p0 graph screenshot: %s STILL blank after %s retr%s (gate_ok=%s) — "
                "posting likely-blank image as last resort",
                band_label,
                attempt,
                "y" if attempt == 1 else "ies",
                gate_ok,
            )
            return png
    except Exception as e:
        log.warning("p0 graph screenshot: viewport-at-band failed: %s", e)
    return None


def _screenshot_document_band(page, doc_clip: Dict[str, int]) -> Optional[bytes]:
    """
    Scroll band into view first (fixes black PNG for off-screen band 2), then capture.
    Headless Grafana often paints panels only after they enter the viewport.
    """
    try:
        y = int(doc_clip.get("y") or 0)
        h = int(doc_clip.get("height") or 0)
        page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 12))", y)
        page.wait_for_timeout(500)
        page.evaluate(
            """({ y, h }) => {
              const mid = y + Math.floor(h * 0.4);
              window.scrollTo(0, Math.max(0, mid - 100));
              window.scrollTo(0, Math.max(0, y - 12));
            }""",
            {"y": y, "h": h},
        )
        page.wait_for_timeout(900)
        if h >= 180:
            _wait_for_charts_in_document_band(page, doc_clip, timeout_ms=12000)
        else:
            page.wait_for_timeout(600)
        page.wait_for_timeout(400)
        vp_clip = page.evaluate(
            """(c) => {
              const sy = window.scrollY || 0;
              const vh = window.innerHeight || 1080;
              const yOff = Math.max(0, c.y - sy);
              const hCap = Math.min(c.height, Math.max(200, vh - yOff));
              return {
                x: Math.max(0, c.x),
                y: yOff,
                width: c.width,
                height: hCap,
              };
            }""",
            doc_clip,
        )
        if not vp_clip:
            return None
        min_h = 60 if h < 180 else 200
        if int(vp_clip.get("height") or 0) < min_h:
            return None
        raw = page.screenshot(full_page=False, type="png", clip=vp_clip)
        if raw and not _png_bytes_uniformly_blank(raw):
            return _normalize_screenshot_png(raw)
        if int(doc_clip.get("height") or 0) > int(vp_clip.get("height") or 0):
            log.info("p0 graph screenshot: band taller than viewport — full_page clip fallback")
            page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 12))", y)
            page.wait_for_timeout(600)
            raw2 = page.screenshot(full_page=True, type="png", clip=doc_clip)
            if raw2 and not _png_bytes_uniformly_blank(raw2):
                return _normalize_screenshot_png(raw2)
    except Exception as e:
        log.warning("p0 graph screenshot: document band capture failed: %s", e)
    return None


def _prewarm_full_page_lazy_panels(page) -> None:
    """
    Scroll the whole document (top→bottom→top) so Grafana renders every lazy/virtualized panel at
    full height BEFORE band clips are measured. Without this, a tall below-the-fold panel (e.g. the
    9280 Connection panel at the end of the KPI section) measures short and gets cut off in band 1.
    """
    try:
        page.evaluate(
            """async () => {
              const de = document.scrollingElement || document.documentElement;
              const step = Math.max(300, Math.floor((window.innerHeight || 1080) * 0.8));
              let total = de.scrollHeight;
              for (let y = 0; y <= total; y += step) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 130));
                total = de.scrollHeight;  // grows as lazy panels load in
              }
              window.scrollTo(0, 0);
              await new Promise(r => setTimeout(r, 250));
            }"""
        )
        page.wait_for_timeout(400)
        log.info("p0 graph screenshot: pre-warmed full page (lazy panels rendered before measure)")
    except Exception as e:
        log.debug("p0 graph screenshot: full-page pre-warm failed: %s", e)


def _screenshot_highlight_bands(page) -> List[bytes]:
    """Capture dashboard bands: 2 (VNC-style) or 3 (optional separate Login PNG)."""
    from p0_logic import config as _config

    include_login = _config.get_p0_graph_screenshot_include_login_panel()
    if _config.get_p0_graph_screenshot_band_warm_scroll():
        _prewarm_full_page_lazy_panels(page)
    clips = _measure_grafana_highlight_band_clips(page, include_login_panel=include_login)
    if len(clips) < 2:
        return []
    pngs: List[bytes] = []
    labels = (
        ("top KPI/FPMS", "CPMS/IGO/Pulsar")
        if not include_login
        else ("top KPI/FPMS", "CPMS/IGO/Pulsar", "Login With Password")
    )
    for idx, clip in enumerate(clips):
        label = labels[idx] if idx < len(labels) else f"band {idx + 1}"
        log.info(
            "p0 graph screenshot: highlight %s (%s/%s) clip x=%s y=%s w=%s h=%s",
            label,
            idx + 1,
            len(clips),
            clip.get("x"),
            clip.get("y"),
            clip.get("width"),
            clip.get("height"),
        )
        raw = _screenshot_viewport_at_band_start(
            page,
            clip,
            is_lower_band=(idx >= 1),
            is_first_band=(idx == 0),
        )
        if raw:
            pngs.append(raw)
            log.info(
                "p0 graph screenshot: highlight %s viewport PNG bytes=%s",
                label,
                len(raw),
            )
        else:
            log.warning("p0 graph screenshot: highlight %s blank or failed", label)
    return pngs


def _viewport_top_and_bottom_screenshots(
    page, viewport_h: int, dashboard_url: str = ""
) -> List[bytes]:
    """
    Two PNGs = only the highlighted dashboard bands (not full browser viewports).
    Page must already be prepared by ``_goto_and_wait`` — do not reload again here.
    """
    scroll_sel: Optional[str] = None
    pngs: List[bytes] = []
    try:
        from p0_logic import config as _config

        _scroll_pair_reset_top(page, scroll_sel)
        extra = _band_post_capture_settle_ms()
        if extra > 0:
            page.wait_for_timeout(extra)
        band_pngs = _screenshot_highlight_bands(page)
        if len(band_pngs) >= 2:
            log.info("p0 graph screenshot: captured %s highlight-band PNG(s)", len(band_pngs))
            return band_pngs
        if len(band_pngs) == 1:
            pngs.extend(band_pngs)
        log.info("p0 graph screenshot: highlight bands unavailable — fallback viewport top/bottom")
        if _mark_wide_dashboard_scroll_container(page):
            scroll_sel = "[data-p0-dash-page-scroll='1']"
        _scroll_pair_reset_top(page, scroll_sel)
        page.wait_for_timeout(400)
        top = _dashboard_viewport_screenshot(page)
        if top:
            pngs.append(top)
            log.info("p0 graph screenshot: top+bottom part 1/2 captured (scroll top fallback)")
        _scroll_to_max(page, scroll_sel)
        page.wait_for_timeout(1400)
        bottom = _dashboard_viewport_screenshot(page)
        if bottom and not _png_bytes_uniformly_blank(bottom):
            if not (top and bottom == top):
                pngs.append(bottom)
                log.info("p0 graph screenshot: top+bottom part 2/2 captured (scroll bottom fallback)")
        elif bottom:
            log.info("p0 graph screenshot: bottom capture looks blank — skipping 2nd image")
        log.info(
            "p0 graph screenshot: top+bottom capture got %s page(s) sel=%s",
            len(pngs),
            scroll_sel or "window",
        )
        return pngs
    except Exception as e:
        log.warning("p0 graph screenshot: top+bottom capture failed: %s", e)
        return pngs
    finally:
        _scroll_pair_reset_top(page, scroll_sel)
        _clear_p0_dash_page_scroll_marks(page)


def _grafana_on_dashboard_page(page) -> bool:
    """True when the dashboard grid is present (logged-in view)."""
    try:
        return bool(
            page.evaluate(
                """() => {
                  const grid = document.querySelector(
                    '.react-grid-layout, [data-testid="dashboard-layout-grid"]'
                  );
                  if (!grid) return false;
                  const r = grid.getBoundingClientRect();
                  return r.width > 200 && r.height > 120;
                }"""
            )
        )
    except Exception:
        return False


def _grafana_login_form_visible(page) -> bool:
    """
    True only when a **visible** login form is shown — not hidden inputs left in the DOM
    after a successful login (that caused false ``auto-login failed`` warnings).
    """
    try:
        return bool(
            page.evaluate(
                """() => {
                  const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    if (r.width < 40 || r.height < 12) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (parseFloat(st.opacity || '1') < 0.05) return false;
                    return true;
                  };
                  const user = document.querySelector(
                    'input[name="user"], input[name="username"], #user-input, '
                    + 'input[autocomplete="username"]'
                  );
                  const pw = document.querySelector(
                    'input[name="password"], input[type="password"]'
                  );
                  if (vis(pw)) return true;
                  if (vis(user) && pw) return true;
                  const path = (window.location.pathname || '').toLowerCase();
                  if (path.includes('/login') && !document.querySelector('.react-grid-layout')) {
                    return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


def _grafana_login_page_detected(page) -> bool:
    """
    Robustly decide "are we NOT authenticated?" AFTER a goto — regardless of cookies.

    A stale persistent-profile session makes Grafana serve ``/login`` (or a login form) even
    though cookies exist, so we must NOT infer "cookies exist => logged in". We are on the login
    page when ANY of these hold and the dashboard grid is NOT painted:
      - URL path contains ``/login``, OR
      - a password / username input exists in the DOM, OR
      - a "Log in" / "Sign in" submit button exists.
    This is deliberately looser than ``_grafana_login_form_visible`` (does not require the form to
    be laid out to a min size) so a login page mid-mount is still detected instead of silently
    skipped.
    """
    try:
        return bool(
            page.evaluate(
                """() => {
                  // Painted dashboard grid => authenticated, definitely not the login page.
                  const grid = document.querySelector(
                    '.react-grid-layout, [data-testid="dashboard-layout-grid"]'
                  );
                  if (grid) {
                    const gr = grid.getBoundingClientRect();
                    if (gr.width > 200 && gr.height > 120) return false;
                  }
                  const path = (window.location.pathname || '').toLowerCase();
                  if (path.indexOf('/login') !== -1) return true;
                  const pw = document.querySelector(
                    'input[name="password"], input[type="password"], '
                    + '[data-testid="data-testid Password input"]'
                  );
                  if (pw) return true;
                  const user = document.querySelector(
                    'input[name="user"], input[name="username"], #user-input, '
                    + 'input[autocomplete="username"], [data-testid="data-testid Username input"]'
                  );
                  if (user) return true;
                  const btns = Array.from(document.querySelectorAll('button, a[role="button"]'));
                  if (btns.some((b) => /\\b(log ?in|sign ?in)\\b/i.test((b.textContent || '')))) {
                    return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


def _grafana_local_login_fields_present(page) -> bool:
    """
    True when a **local** username/password form is present (as opposed to an SSO/OAuth-only
    login page that offers just "Sign in with <provider>" buttons). Used to detect SSO so we can
    stop guessing: username/password auto-login can't work against an OAuth flow.
    """
    try:
        return bool(
            page.evaluate(
                """() => {
                  const pw = document.querySelector(
                    'input[name="password"], input[type="password"], '
                    + '[data-testid="data-testid Password input"]'
                  );
                  const user = document.querySelector(
                    'input[name="user"], input[name="username"], #user-input, '
                    + 'input[autocomplete="username"], [data-testid="data-testid Username input"]'
                  );
                  return !!(pw && user);
                }"""
            )
        )
    except Exception:
        return False


def _wait_for_grafana_auth_state(page, *, timeout_ms: int) -> None:
    """
    Bounded wait (post-goto) for the auth state to SETTLE: resolves as soon as EITHER the painted
    dashboard grid OR a login form/redirect is present. Avoids the race where a synchronous login
    check runs before Grafana's React login form has mounted (the observed "no login line, then
    90s blank" failure). Warm sessions resolve on the first poll (grid already painted) so the good
    path pays ~nothing.
    """
    if timeout_ms <= 0:
        return
    try:
        page.wait_for_function(
            """() => {
              const grid = document.querySelector(
                '.react-grid-layout, [data-testid="dashboard-layout-grid"]'
              );
              if (grid) {
                const gr = grid.getBoundingClientRect();
                if (gr.width > 200 && gr.height > 120) return true;
              }
              const path = (window.location.pathname || '').toLowerCase();
              if (path.indexOf('/login') !== -1) return true;
              if (document.querySelector('input[name="password"], input[type="password"]')) {
                return true;
              }
              if (document.querySelector(
                    'input[name="user"], input[name="username"], #user-input'
                  )) {
                return true;
              }
              return false;
            }""",
            timeout=timeout_ms,
            polling=250,
        )
    except Exception:
        # Neither settled in time — fall through; callers re-check state explicitly.
        pass


def _grafana_auto_login_if_needed(page, dashboard_url: str, *, nav_ms: int, goto_wait: str) -> bool:
    """
    Ensure the page is an authenticated Grafana dashboard, logging in with
    ``P0_GRAPH_SCREENSHOT_USERNAME`` / ``P0_GRAPH_SCREENSHOT_PASSWORD`` if it is sitting on the
    login page. Returns True when authenticated (or login not required), False when we are still
    on the login page and cannot proceed (dead creds / SSO / stale session with no creds) — the
    caller must then FAIL FAST rather than burn the cold grid-reload budget on a login page.

    Detection is positive and cookie-independent: we wait briefly for the auth state to settle,
    then decide from the DOM/URL — never from "cookies exist".
    """
    from p0_logic import config as _config

    user = _config.get_p0_graph_screenshot_username()
    pwd = _config.get_p0_graph_screenshot_password()
    login_ms = _config.get_p0_graph_screenshot_login_timeout_ms()

    # Let the auth state settle so a login form mid-mount is not mistaken for "logged in".
    _wait_for_grafana_auth_state(page, timeout_ms=login_ms)

    if not _grafana_login_page_detected(page):
        # Warm / valid session (grid present or no login form) — proceed immediately.
        return True

    # We are unauthenticated. Distinguish the failure modes clearly.
    if not user or not pwd:
        log.error(
            "grafana login: FAILED — on the login page but no P0_GRAPH_SCREENSHOT_USERNAME/"
            "PASSWORD set (stale profile session). Re-seed with "
            "features/screenshot/scripts/grafana_playwright_login_once.py — captures will be blank"
        )
        return False

    # Give a slow-mounting React login form a brief, bounded chance to render its inputs before we
    # decide "SSO" (no local form). Prevents a false SSO verdict on a still-loading /login page.
    try:
        page.wait_for_selector(
            'input[name="password"], input[type="password"], '
            'input[name="user"], input[name="username"]',
            state="visible",
            timeout=min(login_ms, 8000),
        )
    except Exception:
        pass

    if not _grafana_local_login_fields_present(page):
        log.error(
            "grafana login: FAILED — login page has NO local username/password form (SSO/OAuth "
            "detected at %s). Username/password auto-login cannot work; seed an SSO session headed "
            "with features/screenshot/scripts/grafana_playwright_login_once.py — captures will be "
            "blank",
            (page.url or "").split("?")[0],
        )
        return False

    log.info("grafana login: submitting as %s (pwd_len=%s)", user, len(pwd))
    user_filled = False
    user_loc = None
    for sel in (
        'input[name="user"]',
        'input[name="username"]',
        "#user-input",
        'input[autocomplete="username"]',
        '[data-testid="username-input"] input',
        '[data-testid="Username input"]',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                user_loc = loc.first
                user_loc.click(timeout=5000)
                user_loc.fill("", timeout=5000)
                user_loc.fill(user, timeout=8000)
                user_filled = True
                break
        except Exception:
            continue
    if not user_filled:
        log.error(
            "grafana login: FAILED — username field not found on the login page "
            "(unexpected form / SSO?) — captures will be blank"
        )
        return False
    pwd_filled = False
    pwd_loc = None
    for sel in (
        'input[name="password"]',
        'input[type="password"]',
        '[data-testid="password-input"] input',
        '[data-testid="Password input"]',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                pwd_loc = loc.first
                pwd_loc.click(timeout=5000)
                pwd_loc.fill("", timeout=5000)
                pwd_loc.fill(pwd, timeout=8000)
                pwd_filled = True
                break
        except Exception:
            continue
    if not pwd_filled:
        log.error(
            "grafana login: FAILED — password field not found on the login page "
            "(unexpected form / SSO?) — captures will be blank"
        )
        return False
    clicked = False
    for sel in (
        'button[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        '[data-testid="data-testid Login button"]',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=8000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        try:
            if pwd_loc:
                pwd_loc.press("Enter")
            else:
                page.keyboard.press("Enter")
        except Exception:
            pass
    # Wait (bounded) for navigation OFF /login and the dashboard grid to appear. This is the
    # authoritative success signal — not "the password box went away".
    wait_ms = max(login_ms, 8000)
    try:
        page.wait_for_function(
            """() => {
              const path = (window.location.pathname || '').toLowerCase();
              if (path.indexOf('/login') !== -1) return false;
              const grid = document.querySelector(
                '.react-grid-layout, [data-testid="dashboard-layout-grid"]'
              );
              if (grid) {
                const r = grid.getBoundingClientRect();
                if (r.width > 200 && r.height > 120) return true;
              }
              // Off /login but grid not painted yet: accept absence of the password field as
              // "auth accepted, dashboard loading".
              return !document.querySelector('input[type="password"], input[name="password"]');
            }""",
            timeout=wait_ms,
            polling=300,
        )
    except Exception as e:
        log.debug("grafana login: wait-for-dashboard timed out: %s", e)
    try:
        page.wait_for_load_state(
            goto_wait if goto_wait in ("load", "domcontentloaded", "networkidle") else "load",
            timeout=wait_ms,
        )
    except Exception as e:
        log.debug("grafana login: post-login load_state: %s", e)

    # If we are still on the login page, the credentials were rejected (or SSO intercepted).
    # Fail FAST and LOUD so monitoring is alerted — do NOT let the caller burn the cold reload
    # budget on a login page.
    if _grafana_login_page_detected(page):
        log.error(
            "grafana login: FAILED (still on login / bad credentials?) — captures will be blank"
        )
        return False

    # Authenticated. Re-navigate to the target dashboard if the login flow landed us elsewhere
    # (e.g. Grafana home when there was no redirectTo).
    dash = (dashboard_url or "").strip()
    if dash and "/d/" in dash and dash not in (page.url or ""):
        try:
            page.goto(dash, wait_until=goto_wait, timeout=nav_ms)
        except Exception as e:
            log.warning("grafana login: redirect to dashboard failed: %s", e)
    log.info("grafana login: success (dashboard reached)")
    return True


def _clear_p0_scroll_target_marks(page) -> None:
    try:
        page.evaluate(
            """() => {
              document.querySelectorAll('[data-p0-capture-scroll]').forEach((e) =>
                e.removeAttribute('data-p0-capture-scroll')
              );
            }"""
        )
    except Exception:
        pass


def _split_clip_vertical_halves(clip: Dict[str, int]) -> Tuple[Dict[str, int], Dict[str, int]]:
    h = max(int(clip.get("height") or 0), 2)
    mid = max(h // 2, 1)
    c1 = {**clip, "height": mid}
    c2 = {
        **clip,
        "y": int(clip["y"]) + mid,
        "height": h - mid,
    }
    return c1, c2


def _resolve_capture_tz():
    from p0_logic import config as _config

    name = _config.get_p0_graph_screenshot_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning(
            "p0 graph screenshot: invalid P0_GRAPH_SCREENSHOT_TIMEZONE=%r — using Asia/Kuala_Lumpur",
            name,
        )
        return ZoneInfo("Asia/Kuala_Lumpur")


def _format_captured_at(dt: datetime) -> str:
    """Human-readable 'as of' line; tz-aware ``dt``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def is_graph_capture_busy() -> bool:
    return _read_capture_busy()


def _start_on_demand_capture_thread(
    tenant_token: str,
    post_chat_id: str,
    range_key: str,
    source_chat_label: str,
    *,
    trigger_message_id: str = "",
) -> None:
    tok = (tenant_token or "").strip()
    cid = (post_chat_id or "").strip()
    rk = (range_key or "").strip().lower()
    label = (source_chat_label or "").strip()
    trig_mid = (trigger_message_id or "").strip()

    def _run() -> None:
        _capture_and_post_ranges_thread_body(
            tok,
            cid,
            label,
            [rk],
            on_demand=True,
            trigger_message_id=trig_mid,
        )

    threading.Thread(
        target=_run,
        name=f"p0-graph-screenshot-{rk}",
        daemon=True,
    ).start()


def _drain_on_demand_pending() -> None:
    with _ON_DEMAND_PENDING_LOCK:
        if not _ON_DEMAND_PENDING:
            return
        job = dict(_ON_DEMAND_PENDING.pop(0))
    log.info(
        "p0 graph screenshot on-demand: starting queued job range=%s chat_tail=%s",
        job.get("range_key"),
        str(job.get("post_chat_id") or "")[-12:],
    )
    _start_on_demand_capture_thread(
        str(job.get("tenant_token") or ""),
        str(job.get("post_chat_id") or ""),
        str(job.get("range_key") or ""),
        str(job.get("source_chat_label") or ""),
        trigger_message_id=str(job.get("trigger_message_id") or ""),
    )


def schedule_on_demand_graph_screenshot(
    tenant_token: str,
    post_chat_id: str,
    range_key: str,
    source_chat_label: str = "",
    *,
    trigger_message_id: str = "",
) -> str:
    """
    On-demand single-range capture (e.g. typed ``screenshot 30 min``).

    Returns ``started``, ``queued`` (another capture running), or ``skipped``.
    """
    from p0_logic import config as _config

    tok = (tenant_token or "").strip()
    cid = (post_chat_id or "").strip()
    rk = (range_key or "").strip().lower()
    if not tok or not cid or not _config.build_p0_graph_screenshot_url_for_range(rk):
        log.warning(
            "p0 graph screenshot on-demand: skip schedule tok=%s cid=%s range=%s url=%s",
            bool(tok),
            bool(cid),
            rk,
            bool(_config.build_p0_graph_screenshot_url_for_range(rk)),
        )
        return "skipped"

    label = (source_chat_label or "").strip()
    trig_mid = (trigger_message_id or "").strip()
    job = {
        "tenant_token": tok,
        "post_chat_id": cid,
        "range_key": rk,
        "source_chat_label": label,
        "trigger_message_id": trig_mid,
    }

    if _read_capture_busy():
        with _ON_DEMAND_PENDING_LOCK:
            _ON_DEMAND_PENDING.append(job)
        log.info("p0 graph screenshot on-demand: queued (capture busy) range=%s", rk)
        return "queued"

    _start_on_demand_capture_thread(tok, cid, rk, label, trigger_message_id=trig_mid)
    return "started"


def schedule_p0_graph_screenshot(tenant_token: str, priority: str, source_chat_label: str) -> None:
    """
    Non-blocking: captures Grafana for each auto range (default **6h** only) and posts to
    ``P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID``. Runs for **P0** always; for **P1** only when
    ``P0_GRAPH_SCREENSHOT_ON_P1`` is on.

    If ``P0_GRAPH_SCREENSHOT_INTERVAL_MIN`` > 0, also schedules repeat captures every N minutes
    until no screenshot-eligible session remains (see ``on_p0_session_ended_for_graph_screenshot``).
    """
    from p0_logic import config as _config

    pri = (priority or "").strip().upper()
    if not _priority_screenshot_eligible(pri):
        log.info(
            "p0 graph screenshot: skipped — priority=%s not eligible "
            "(P0 always; P1 needs P0_GRAPH_SCREENSHOT_ON_P1=1)",
            pri or "?",
        )
        return
    if not _config.p0_graph_screenshot_enabled():
        log.info("p0 graph screenshot: skipped — set P0_GRAPH_SCREENSHOT_ENABLED=1")
        return
    chat_id = _config.get_p0_graph_screenshot_target_chat_id()
    grafana_url = _config.get_p0_graph_screenshot_url()
    if not grafana_url or not chat_id:
        missing = []
        if not grafana_url:
            missing.append("P0_GRAPH_SCREENSHOT_URL")
        if not chat_id:
            missing.append("P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID")
        log.warning("p0 graph screenshot: skipped — missing %s", ", ".join(missing))
        return
    tok = (tenant_token or "").strip()
    if not tok:
        log.warning("p0 graph screenshot: skipped — no tenant token")
        return

    label = (source_chat_label or "").strip()
    ranges = _config.get_p0_graph_screenshot_auto_range_keys()
    log.info(
        "p0 graph screenshot: scheduling auto capture ranges=%s target_tail=%s label=%r fast=%s pool=%s",
        ranges,
        chat_id[-12:] if len(chat_id) > 12 else chat_id,
        label[:48],
        _config.get_p0_graph_screenshot_fast_capture(),
        _config.get_p0_graph_screenshot_browser_pool_enabled(),
    )
    range_labels = [
        _config.get_p0_graph_screenshot_range_display(rk) for rk in ranges if rk
    ]
    range_hint = range_labels[0] if len(range_labels) == 1 else ", ".join(range_labels)
    from p0_logic import lark_client as _lark

    def _run() -> None:
        # Post the "Capturing…" notice from INSIDE the daemon thread so that nothing
        # Grafana-related — not even this status POST — runs synchronously on the caller
        # (start_p0) thread. A slow/hung Lark POST here can no longer delay the Bitable step.
        # Suppressible so the Bitable card lands first (P0_GRAPH_SCREENSHOT_CAPTURING_NOTICE=0).
        if _config.get_p0_graph_screenshot_capturing_notice_enabled():
            _lark.post_text_to_chat(
                chat_id,
                tok,
                f"📊 Capturing Grafana dashboard (last {range_hint})…",
            )
        _capture_and_post_ranges_thread_body(tok, chat_id, label)

    threading.Thread(target=_run, name="p0-graph-screenshot", daemon=True).start()
    start_p0_graph_screenshot_interval(label)


def _split_png_vertical_halves(png_bytes: bytes) -> List[bytes]:
    """Split a full-page PNG into upper and lower halves (same width, half height each)."""
    try:
        from PIL import Image
    except ImportError:
        log.warning(
            "p0 graph screenshot: Pillow not installed — cannot split; install pillow or unset "
            "P0_GRAPH_SCREENSHOT_SPLIT_VERTICAL_HALVES"
        )
        return []
    try:
        im = Image.open(BytesIO(png_bytes))
        im.load()
    except Exception as e:
        log.warning("p0 graph screenshot: failed to open PNG for split: %s", e)
        return []
    w, h = im.size
    if h < 4:
        return []
    mid = h // 2
    out: List[bytes] = []
    for box in ((0, 0, w, mid), (0, mid, w, h)):
        crop = im.crop(box)
        buf = BytesIO()
        try:
            crop.save(buf, format="PNG", optimize=True)
        except Exception as e:
            log.warning("p0 graph screenshot: failed to encode split half: %s", e)
            return []
        out.append(buf.getvalue())
    return out


def _png_bytes_uniformly_blank(png: bytes) -> bool:
    """
    Heuristic: near-black PNG with almost no luminance spread → headless compositor / wrong clip.
    """
    try:
        from PIL import Image
        from PIL.ImageStat import Stat
    except ImportError:
        return False
    try:
        im = Image.open(BytesIO(png))
        im.load()
        im = im.convert("L")
        im.thumbnail((160, 160))
        st = Stat(im)
        mean = float(st.mean[0])
        lo, hi = st.extrema[0]
        spread = float(hi) - float(lo)
    except Exception:
        return False
    if mean <= 8.0:
        return True
    if mean <= 18.0 and spread <= 12.0:
        return True
    return False


def _png_panel_region_blank(png: bytes) -> bool:
    """
    Region heuristic for the INTERMITTENT blank capture: the Grafana top chrome
    (Export/Share/Refresh toolbar + section title) paints fine, but the panel area *below* is an
    unpainted, uniform background — no charts. ``_png_bytes_uniformly_blank`` misses this because
    the painted header adds luminance + spread to the *whole-image* stats. Here we look ONLY at the
    lower ~82% (the panel area) and flag it when a single background shade covers almost every
    pixel, or the region has almost no luminance spread.

    Why this won't false-positive on a legitimately dark dashboard: a real Grafana panel area —
    even a dark one, even panels showing ``No data`` — always has chart lines / axes / gridlines /
    legends / panel titles / text, so no single 8-wide luminance bucket dominates (>=95%) and the
    standard deviation is well above ~4. Only a genuinely unpainted background is near-constant.
    It is also only consulted when the readiness gate already timed out, so it can never discard a
    capture the gate accepted.
    """
    try:
        from PIL import Image
        from PIL.ImageStat import Stat
    except ImportError:
        return False
    try:
        im = Image.open(BytesIO(png))
        im.load()
        im = im.convert("L")
        w, h = im.size
        if w < 8 or h < 48:
            return False
        top_cut = int(h * 0.18)  # drop the toolbar + section header (the part that DOES paint)
        region = im.crop((0, top_cut, w, h))
        region.thumbnail((240, 240))
        hist = region.histogram()  # 256 luminance buckets ('L' mode)
        total = float(sum(hist) or 1)
        # 8-wide buckets tolerate PNG / anti-alias dithering of one otherwise-flat background shade.
        buckets = [sum(hist[i:i + 8]) for i in range(0, 256, 8)]
        dominant_frac = max(buckets) / total
        stddev = float(Stat(region).stddev[0])
    except Exception:
        return False
    if dominant_frac >= 0.95:
        return True
    if stddev <= 4.0 and dominant_frac >= 0.85:
        return True
    return False


def _png_list_all_uniformly_blank(pngs: List[bytes]) -> bool:
    if not pngs:
        return False
    for p in pngs:
        if not _png_bytes_uniformly_blank(p):
            return False
    return True


def post_p0_graph_screenshots_to_chat(
    tenant_token: str,
    chat_id: str,
    pngs: List[bytes],
    captured_at: str,
    source_label: str = "",
    *,
    range_label: str = "",
) -> None:
    """
    Post the **As of:** line + image part(s) to a Lark group — same as the P0 auto flow.
    Used by ``_capture_and_post`` and by ``features/screenshot/scripts/grafana_screenshot_run_once.py --post-lark``.
    """
    from p0_logic import config as _config
    from p0_logic import lark_client as _lark

    tok = (tenant_token or "").strip()
    cid = (chat_id or "").strip()
    if not tok or not cid:
        log.warning("p0 graph screenshot: post skipped — missing token or chat_id")
        return
    cap = _config.get_p0_graph_screenshot_caption()
    range_disp = _config.get_p0_graph_screenshot_range_display(range_label) if range_label else ""
    if cap:
        text = cap.replace("{captured_at}", captured_at)
        text = text.replace("{label}", (source_label or "").strip())
        text = text.replace("{range}", range_disp)
    elif range_disp:
        text = f"As of: {captured_at} · Last {range_disp}"
    else:
        text = f"As of: {captured_at}"
    st_t, _ = _lark.post_text_to_chat(cid, tok, text)
    if st_t != 200:
        log.warning("p0 graph screenshot: caption post HTTP=%s", st_t)

    pngs_eff = [p for p in pngs if not _png_bytes_uniformly_blank(p)]
    if len(pngs_eff) < len(pngs):
        log.warning(
            "p0 graph screenshot: dropping %s uniformly blank part(s) before Lark post (common when "
            "part 1 is an unpainted virtualized band and part 2 has the table)",
            len(pngs) - len(pngs_eff),
        )
    if not pngs_eff:
        log.warning(
            "p0 graph screenshot: all image parts look blank — skipping image upload "
            "(try P0_GRAPH_SCREENSHOT_SWIFTSHADER=1, HEADED=1 on VNC, or VIEWPORT_ONLY / FULL_PAGE+no split)"
        )
        _post_capture_failure_to_chat(
            tok,
            cid,
            range_label=range_label,
            reason="Images were blank — try `P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR` + login profile.",
        )
        return
    pngs = pngs_eff

    posted_any = False
    for idx, png in enumerate(pngs):
        fname = "p0-dashboard.png" if len(pngs) == 1 else f"p0-dashboard-part{idx + 1}.png"
        key = _lark.upload_image_bytes_for_im_message(tok, png, fname)
        if not key:
            log.warning("p0 graph screenshot: Lark image upload failed part=%s (check im:resource scope)", idx + 1)
            continue
        st, body = _lark.post_image_to_chat(cid, tok, key)
        if st != 200:
            log.warning(
                "p0 graph screenshot: image message part=%s HTTP=%s body=%s",
                idx + 1,
                st,
                (body or "")[:400],
            )
        else:
            posted_any = True
            log.info(
                "p0 graph screenshot: posted image part=%s/%s to chat_id tail=%s",
                idx + 1,
                len(pngs),
                cid[-12:],
            )
    if not posted_any:
        _post_capture_failure_to_chat(
            tok,
            cid,
            range_label=range_label,
            reason="Lark image upload/post failed — check im:resource scope and bot membership in target chat.",
        )


def _post_capture_failure_to_chat(
    token: str,
    chat_id: str,
    *,
    range_label: str = "",
    reason: str = "",
) -> None:
    from p0_logic import lark_client as _lark

    cid = (chat_id or "").strip()
    tok = (token or "").strip()
    if not cid or not tok:
        return
    rk = (range_label or "").strip()
    detail = (reason or "").strip()
    msg = "📊 Grafana screenshot failed"
    if rk:
        from p0_logic import config as _config

        msg += f" (last {_config.get_p0_graph_screenshot_range_display(rk)})"
    msg += "."
    if detail:
        msg += f" {detail}"
    else:
        msg += (
            " Check server logs (`journalctl -u lark-ops-ai`) — often Playwright hang, "
            "Grafana login, or blank capture."
        )
    st, _ = _lark.post_text_to_chat(cid, tok, msg)
    if st != 200:
        log.warning("p0 graph screenshot: failure notice HTTP=%s", st)


def _on_demand_capture_timed_out() -> bool:
    with _on_demand_timed_out_lock:
        return bool(_on_demand_timed_out)


def _capture_and_post(
    token: str,
    url: str,
    chat_id: str,
    source_label: str,
    *,
    range_label: str = "",
) -> bool:
    if _is_on_demand_capture() and _on_demand_capture_timed_out():
        log.warning("p0 graph screenshot on-demand: skip post (wall-clock timeout already notified)")
        return False
    pngs, captured_at = _capture_png_payloads(url)
    if not pngs:
        log.warning(
            "p0 graph screenshot: capture returned empty range=%s url_head=%r",
            range_label or "default",
            (url or "")[:80],
        )
        detail = _get_capture_error() or "Capture returned no images (Playwright/Grafana)."
        _post_capture_failure_to_chat(
            token,
            chat_id,
            range_label=range_label,
            reason=detail,
        )
        if _is_on_demand_capture():
            from p0_logic import config as _cfg_fail

            _react_to_trigger_message(token, _cfg_fail.get_p0_graph_screenshot_react_failed_emoji())
        return False
    post_p0_graph_screenshots_to_chat(
        token, chat_id, pngs, captured_at, source_label, range_label=range_label
    )
    if _is_on_demand_capture():
        from p0_logic import config as _cfg_ok

        _react_to_trigger_message(token, _cfg_ok.get_p0_graph_screenshot_react_done_emoji())
    return True


def _capture_and_post_ranges(
    token: str,
    chat_id: str,
    source_label: str,
    range_keys: List[str],
) -> None:
    """Capture and post each time range sequentially (one Playwright session per range)."""
    from p0_logic import config as _config

    keys = [k for k in (range_keys or []) if k]
    if not keys:
        keys = _config.get_p0_graph_screenshot_auto_range_keys()
    log.info(
        "p0 graph screenshot: capture started ranges=%s label=%r chat_id_tail=%s on_demand=%s fast=%s",
        keys,
        (source_label or "")[:48],
        chat_id[-12:] if chat_id and len(chat_id) > 12 else chat_id,
        _is_on_demand_capture(),
        _effective_fast_capture(),
    )
    if _is_on_demand_capture():
        log.info(
            "p0 graph screenshot on-demand timing: band_max_ms=%s stable_polls=%s pool=%s top_bottom=%s",
            _config.get_p0_graph_screenshot_on_demand_band_max_wait_ms(),
            _config.get_p0_graph_screenshot_on_demand_band_stable_polls()
            if _effective_fast_capture()
            else _config.get_p0_graph_screenshot_band_stable_polls(),
            _config.get_p0_graph_screenshot_browser_pool_enabled(),
            _config.get_p0_graph_screenshot_top_and_bottom(),
        )
    for rk in keys:
        url = _config.build_p0_graph_screenshot_url_for_range(rk)
        if not url:
            log.warning("p0 graph screenshot: skip range=%s (no URL)", rk)
            continue
        log.info("p0 graph screenshot: capturing range=%s", rk)
        _capture_and_post(token, url, chat_id, source_label, range_label=rk)


def _capture_and_post_ranges_thread_body(
    token: str,
    chat_id: str,
    source_label: str,
    range_keys: Optional[List[str]] = None,
    *,
    on_demand: bool = False,
    trigger_message_id: str = "",
) -> None:
    global _on_demand_timed_out
    from p0_logic import config as _config

    log.info(
        "p0 graph screenshot: capture thread started chat_id_tail=%s on_demand=%s ranges=%s",
        chat_id[-12:] if chat_id and len(chat_id) > 12 else chat_id,
        on_demand,
        range_keys or "(auto)",
    )

    # Claim the capture slot with a BOUNDED acquire (non-reentrant lock, never blocks forever).
    # If a capture is already running (or the lock is wedged past the timeout) we skip (interval)
    # or requeue+notify (on-demand) instead of blocking — this is what stops one stuck capture
    # from permanently starving every future interval tick and on-demand request.
    if not _mark_capture_busy():
        if on_demand:
            rk0 = (range_keys or [""])[0] if range_keys else ""
            with _ON_DEMAND_PENDING_LOCK:
                _ON_DEMAND_PENDING.append(
                    {
                        "tenant_token": token,
                        "post_chat_id": chat_id,
                        "range_key": rk0,
                        "source_chat_label": source_label,
                        "trigger_message_id": trigger_message_id,
                    }
                )
            log.info(
                "p0 graph screenshot on-demand: capture busy — requeued range=%s", rk0
            )
        else:
            log.info(
                "p0 graph screenshot interval: previous capture still running — skip this tick"
            )
        return

    with _on_demand_timed_out_lock:
        _on_demand_timed_out = False
    _capture_ctx_set(
        on_demand=on_demand,
        force_full=False,
        trigger_message_id=trigger_message_id,
    )
    completed = threading.Event()
    watchdog: Optional[threading.Timer] = None
    max_sec = (
        _config.get_p0_graph_screenshot_on_demand_max_sec()
        if on_demand
        else _config.get_p0_graph_screenshot_auto_max_sec()
    )

    # Exactly-once release of the shared capture slot. Callable from BOTH the watchdog (Timer)
    # thread and the capture thread's finally — whichever runs first frees the slot and drains
    # the queue; the other is a no-op. This guarantees the busy flag is ALWAYS cleared even if
    # the capture thread is wedged in an un-interruptible sync Playwright call.
    _final_lock = threading.Lock()
    _finalized = {"done": False}

    def _finalize_release() -> None:
        with _final_lock:
            if _finalized["done"]:
                return
            _finalized["done"] = True
        _clear_capture_busy()
        _drain_on_demand_pending()

    def _capture_watchdog() -> None:
        global _on_demand_timed_out
        if completed.is_set():
            return
        with _on_demand_timed_out_lock:
            _on_demand_timed_out = True
        log.error(
            "p0 graph screenshot: wall-clock timeout after %ss on_demand=%s chat_id_tail=%s "
            "— force-killing Chromium",
            max_sec,
            on_demand,
            chat_id[-12:] if chat_id else "",
        )
        # HARD kill so the stuck sync Playwright call raises and the capture thread unwinds.
        _force_kill_capture_chromium()
        _post_capture_failure_to_chat(
            token,
            chat_id,
            range_label=(range_keys or [""])[0] if range_keys else "",
            reason=(
                f"Timed out after {max_sec}s — Grafana/Playwright was stuck (killed). "
                "Check journalctl -u lark-ops-ai."
            ),
        )
        if on_demand:
            _react_failed_explicit(token, trigger_message_id)
        # Free the slot NOW so interval / on-demand resume even if the wedged thread never
        # reaches its finally.
        _finalize_release()

    watchdog = threading.Timer(float(max_sec), _capture_watchdog)
    watchdog.daemon = True
    watchdog.start()
    try:
        _capture_and_post_ranges(token, chat_id, source_label, range_keys or [])
    except Exception as e:
        if not _on_demand_capture_timed_out():
            _set_capture_error(str(e))
            log.warning("p0 graph screenshot: ranges thread failed: %s", e, exc_info=True)
            _post_capture_failure_to_chat(
                token,
                chat_id,
                range_label=(range_keys or [""])[0] if range_keys else "",
                reason=str(e)[:400],
            )
            if on_demand:
                _react_to_trigger_message(token, _config.get_p0_graph_screenshot_react_failed_emoji())
    finally:
        completed.set()
        if watchdog:
            watchdog.cancel()
        _capture_ctx_clear()
        # Exactly-once: if the watchdog already fired it freed the slot (and may have started a
        # new capture); this call then no-ops so we never clobber the new capture's busy flag.
        _finalize_release()


def _wait_for_grafana_panels_if_configured(page) -> None:
    from p0_logic import config as _config

    panel_timeout = _config.get_p0_graph_screenshot_panel_ready_timeout_ms()
    if panel_timeout <= 0:
        return
    sel = (
        ".react-grid-item, [data-panel-id], [data-viz-key], "
        "[data-testid='dashboard-layout-grid']"
    )
    try:
        page.wait_for_selector(sel, state="visible", timeout=panel_timeout)
        log.info(
            "p0 graph screenshot: dashboard panel DOM ready (waited up to %sms)",
            panel_timeout,
        )
    except Exception as e:
        log.warning(
            "p0 graph screenshot: panel readiness wait failed or timed out — continuing anyway: %s",
            e,
        )


def _wait_for_grafana_chart_content_if_configured(page) -> None:
    from p0_logic import config as _config

    if _highlight_band_capture_mode():
        log.info(
            "p0 graph screenshot: skip global chart wait (TOP_AND_BOTTOM uses per-band panel waits)"
        )
        return
    tmax = _config.get_p0_graph_screenshot_panel_content_ready_timeout_ms()
    if tmax <= 0:
        return
    js = r"""
        () => {
          const root = document.querySelector('main') || document.body;
          if (!root) return false;
          const panels = root.querySelectorAll(
            '[data-panel-id], .react-grid-item, [data-viz-key], '
            + '[data-testid*="panel"], [data-testid*="Panel"]'
          );
          if (panels.length < 1) return false;
          let chartCanv = 0;
          root.querySelectorAll('canvas').forEach((c) => {
            const r = c.getBoundingClientRect();
            if (r.width > 96 && r.height > 56) chartCanv++;
          });
          let bigSvg = 0;
          root.querySelectorAll('svg').forEach((s) => {
            const r = s.getBoundingClientRect();
            if (r.width > 20 && r.height > 12) bigSvg++;
          });
          const loading = root.querySelectorAll(
            '[class*="spinner"], [class*="Spinner"], [class*="loading"], [aria-busy="true"]'
          ).length;
          if (loading > 0) return false;
          if (chartCanv >= 3) return true;
          if (bigSvg >= 6) return true;
          const rows = root.querySelectorAll('table tbody tr, [role="rowgroup"] [role="row"]').length;
          if (rows >= 12 && chartCanv >= 1) return true;
          return false;
        }
    """
    try:
        page.wait_for_function(js, timeout=tmax, polling=400)
        log.info(
            "p0 graph screenshot: chart/table content signal detected (waited up to %sms)",
            tmax,
        )
    except Exception as e:
        log.warning(
            "p0 graph screenshot: chart content wait timed out — screenshot may still be blank: %s",
            e,
        )
        try:
            diag = page.evaluate(
                """() => {
                  const r = document.querySelector('main') || document.body;
                  if (!r) return { panels: 0, chartCanv: 0, textLen: 0 };
                  let chartCanv = 0;
                  r.querySelectorAll('canvas').forEach((c) => {
                    const b = c.getBoundingClientRect();
                    if (b.width > 96 && b.height > 56) chartCanv++;
                  });
                  return {
                    panels: r.querySelectorAll('[data-panel-id], .react-grid-item').length,
                    chartCanv: chartCanv,
                    textLen: (r.innerText || '').length
                  };
                }"""
            )
            log.warning("p0 graph screenshot: DOM diagnostic %s", diag)
        except Exception:
            pass


def _cold_grid_ready_timeout_ms(nav_ms: int) -> int:
    """
    Generous timeout for the cold grid+paint gate. Reuses ``PANEL_CONTENT_READY_TIMEOUT_MS``
    (e.g. 90000) so a cold render — which redirects through login and only paints on the warm
    load — has real time, instead of the short 9s per-band wait. Floors to ``max(nav_ms, 45000)``
    when content-ready is disabled so the self-heal still has a bounded, non-trivial budget.
    """
    from p0_logic import config as _config

    # Cap EACH cold grid-gate attempt so a stuck cold render reloads quickly (self-heal) instead of
    # stalling the full content-ready budget (e.g. 90s) on a single attempt — the 90s stall reads as
    # "frozen". A cold render that will succeed usually paints within ~15-20s; if not, a reload
    # (warm second load) is faster than waiting longer.
    cap = 25_000
    t = _config.get_p0_graph_screenshot_panel_content_ready_timeout_ms()
    if t <= 0:
        return max(min(nav_ms, cap), 15_000)
    return min(t, cap)


_GRID_AND_PAINT_READY_JS = r"""
() => {
  const grid = document.querySelector(
    '.react-grid-layout, [data-testid="dashboard-layout-grid"]'
  );
  if (!grid) return false;
  const gr = grid.getBoundingClientRect();
  if (gr.width < 200 || gr.height < 120) return false;
  // Any panel still showing a spinner/loader means the grid has not settled yet.
  if (grid.querySelector(
        '[class*="spinner"], [class*="Spinner"], [class*="panel-loading"], '
        + '[class*="PanelLoading"], [aria-busy="true"], [role="progressbar"], '
        + '[data-testid*="panel-loader"]'
      )) {
    return false;
  }
  // Require actual painted content (canvas / big SVG / a real table) — a mounted-but-blank
  // grid (the reported symptom: KPI header shows, panel area below is black) must NOT pass.
  let painted = 0;
  grid.querySelectorAll('canvas').forEach((c) => {
    const r = c.getBoundingClientRect();
    if (r.width > 96 && r.height > 48) painted++;
  });
  if (painted >= 1) return true;
  let svgBig = 0;
  grid.querySelectorAll('svg').forEach((s) => {
    const r = s.getBoundingClientRect();
    if (r.width > 40 && r.height > 24) svgBig++;
  });
  if (svgBig >= 3) return true;
  const rows = grid.querySelectorAll(
    'table tbody tr, [role="rowgroup"] [role="row"]'
  ).length;
  if (rows >= 6) return true;
  return false;
}
"""


def _wait_for_grafana_grid_and_paint(page, *, timeout_ms: int) -> bool:
    """
    Wait until the dashboard GRID exists AND at least one panel has actually painted
    (canvas / big SVG / real table), with spinners gone. Returns True when ready, False on
    timeout. This is the cold-start gate: a False result is the signal to RELOAD, not capture.
    """
    if timeout_ms <= 0:
        return _grafana_on_dashboard_page(page)
    try:
        page.wait_for_function(_GRID_AND_PAINT_READY_JS, timeout=timeout_ms, polling=400)
        return True
    except Exception:
        return False


def _reload_grafana_dashboard(page, dashboard_url: str, *, nav_ms: int, goto_wait: str) -> None:
    """Re-navigate to the dashboard URL (falls back to ``page.reload``) and re-seed nav storage."""
    _preset_grafana_nav_local_storage(page)
    dash = (dashboard_url or "").strip()
    try:
        if dash:
            page.goto(dash, wait_until=goto_wait, timeout=nav_ms)
        else:
            page.reload(wait_until=goto_wait, timeout=nav_ms)
    except Exception as e:
        log.warning("p0 graph screenshot: reload goto failed (%s) — trying page.reload()", e)
        try:
            page.reload(wait_until=goto_wait, timeout=nav_ms)
        except Exception as e2:
            log.warning("p0 graph screenshot: page.reload() also failed: %s", e2)


def _ensure_dashboard_grid_ready_with_reload(
    page, dashboard_url: str, *, nav_ms: int, goto_wait: str
) -> bool:
    """
    Cold-start self-heal. After navigation + login, wait (generously) for the dashboard grid to
    exist AND paint. If it does not, RELOAD the dashboard and re-wait — up to
    ``P0_GRAPH_SCREENSHOT_COLD_RELOAD_MAX`` times (a cold Grafana almost always renders on the warm
    second load). Returns True when the grid is painted, False if still not ready after all reloads
    (caller then captures as an honest last resort). A warm/already-rendered page passes the gate on
    the first poll and reloads nothing, so the good path pays ~nothing.
    """
    from p0_logic import config as _config

    cold_wait = _cold_grid_ready_timeout_ms(nav_ms)
    max_reloads = _config.get_p0_graph_screenshot_cold_reload_max()
    for attempt in range(max_reloads + 1):
        if _wait_for_grafana_grid_and_paint(page, timeout_ms=cold_wait):
            if attempt > 0:
                log.info(
                    "p0 graph screenshot: dashboard grid painted after %s reload(s)", attempt
                )
            return True
        if attempt >= max_reloads:
            break
        log.warning(
            "p0 graph screenshot: dashboard grid not detected / panels unpainted after %sms — "
            "reloading (self-heal attempt %s/%s)",
            cold_wait,
            attempt + 1,
            max_reloads,
        )
        _reload_grafana_dashboard(page, dashboard_url, nav_ms=nav_ms, goto_wait=goto_wait)
        # A cold reload can bounce through the login page again before landing on the dashboard.
        if not _grafana_auto_login_if_needed(
            page, dashboard_url, nav_ms=nav_ms, goto_wait=goto_wait
        ):
            log.warning("p0 graph screenshot: re-login during reload self-heal failed")
    if max_reloads > 0:
        # ERROR (not WARNING): a blank capture means the Grafana screenshot effectively did NOT send
        # a usable image — surface it to the monitoring group in real time (min_level=ERROR cutoff).
        # Usually means the Grafana session in the Playwright profile is dead (login page, not the
        # dashboard) — re-seed with scripts/grafana_playwright_login_once.py.
        log.error(
            "p0 graph screenshot: dashboard grid still unpainted after %s reload(s) — capturing a "
            "LIKELY-BLANK image (Grafana session likely dead / stuck on login — re-seed the profile)",
            max_reloads,
        )
    else:
        log.warning(
            "p0 graph screenshot: dashboard grid not detected after login and cold reload "
            "self-heal is disabled (P0_GRAPH_SCREENSHOT_COLD_RELOAD_MAX=0) — screenshot may be blank"
        )
    return False


def _load_grafana_dashboard_for_capture(
    page,
    dashboard_url: str,
    *,
    navigate_only: bool = False,
) -> None:
    """Goto dashboard, login if configured, wait for panels, apply kiosk/zoom (same as capture)."""
    from p0_logic import config as _config

    nav_ms = _config.get_p0_graph_screenshot_nav_timeout_ms()
    goto_wait = _config.get_p0_graph_screenshot_goto_wait_until()
    if navigate_only and _effective_fast_capture():
        goto_wait = "domcontentloaded"
    _preset_grafana_nav_local_storage(page)
    page.goto(dashboard_url, wait_until=goto_wait, timeout=nav_ms)
    if navigate_only and _effective_fast_capture():
        if not _grafana_on_dashboard_page(page):
            if not _grafana_auto_login_if_needed(page, dashboard_url, nav_ms=nav_ms, goto_wait=goto_wait):
                raise RuntimeError(
                    "Grafana login failed — run features/screenshot/scripts/grafana_playwright_login_once.py "
                    "and set P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR"
                )
        # Warm reuse normally passes on the first poll (~free). If a reused context is wedged/blank
        # the same cold self-heal reloads it rather than capturing a stale/blank frame.
        _ensure_dashboard_grid_ready_with_reload(
            page, dashboard_url, nav_ms=nav_ms, goto_wait=goto_wait
        )
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        _wait_for_grafana_panels_if_configured(page)
        settle = _post_nav_settle_ms()
        if settle > 0:
            page.wait_for_timeout(settle)
        _prepare_grafana_dashboard_for_capture(page, dashboard_url)
        return
    if not _grafana_auto_login_if_needed(page, dashboard_url, nav_ms=nav_ms, goto_wait=goto_wait):
        raise RuntimeError(
            "Grafana login failed — login form still visible. "
            "Run features/screenshot/scripts/grafana_playwright_login_once.py + P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR"
        )
    # Cold-start gate: do NOT proceed to band capture while the grid is absent or its panels have
    # not painted. Reloads the dashboard (up to COLD_RELOAD_MAX) — a cold Grafana session renders
    # on the warm second load. This replaces the old passive "grid not detected" warning that
    # captured a blank dashboard anyway.
    _ensure_dashboard_grid_ready_with_reload(
        page, dashboard_url, nav_ms=nav_ms, goto_wait=goto_wait
    )
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    _wait_for_grafana_panels_if_configured(page)
    _wait_for_grafana_chart_content_if_configured(page)
    try:
        page.evaluate("window.scrollBy(0, 600); window.scrollTo(0, 0)")
    except Exception:
        pass
    settle = _post_nav_settle_ms()
    if settle > 0:
        page.wait_for_timeout(settle)
    _prepare_grafana_dashboard_for_capture(page, dashboard_url)


def open_grafana_dashboard_for_inspection() -> int:
    """
    Open **headed** Chromium with the same viewport / kiosk / scene zoom as P0 capture.
    Blocks until Enter in the terminal (for VNC sizing checks). Does not screenshot or post to Lark.
    """
    from p0_logic import config as _config

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright not installed (pip install playwright; playwright install chromium)")
        return 1
    raw_url = _config.get_p0_graph_screenshot_url()
    if not raw_url:
        log.error("Set P0_GRAPH_SCREENSHOT_URL in .env")
        return 1
    kiosk_on = _config.get_p0_graph_screenshot_append_kiosk()
    url = _prepare_grafana_capture_url(raw_url, kiosk=kiosk_on)
    w = _config.get_p0_graph_screenshot_viewport_width()
    h = _config.get_p0_graph_screenshot_viewport_height()
    user_data = _config.get_p0_graph_screenshot_playwright_user_data_dir()
    dsf = _config.get_p0_graph_screenshot_device_scale_factor()
    zoom_pct = _config.get_p0_graph_screenshot_zoom_percent()
    launch_args = list(_config.get_p0_graph_screenshot_chromium_args())
    channel = _config.get_p0_graph_screenshot_chromium_channel()
    top_bottom = _uses_top_and_bottom_framing()
    log.info(
        "p0 graph screenshot inspect: headed viewport=%sx%s zoom=%s%% top+bottom=%s",
        w,
        h,
        zoom_pct,
        top_bottom,
    )
    with sync_playwright() as p:
        if user_data:
            context = p.chromium.launch_persistent_context(
                user_data,
                headless=False,
                channel=(channel or None),
                viewport={"width": w, "height": h},
                device_scale_factor=dsf,
                args=launch_args,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                _load_grafana_dashboard_for_capture(page, url)
                print(
                    "\nGrafana open (same sizing as P0). Scroll and check layout. Press Enter to close…",
                    flush=True,
                )
                input()
            finally:
                context.close()
        else:
            browser = p.chromium.launch(
                headless=False, channel=(channel or None), args=launch_args
            )
            try:
                page = browser.new_page(
                    viewport={"width": w, "height": h},
                    device_scale_factor=dsf,
                )
                _load_grafana_dashboard_for_capture(page, url)
                print(
                    "\nGrafana open (same sizing as P0). Scroll and check layout. Press Enter to close…",
                    flush=True,
                )
                input()
            finally:
                browser.close()
    return 0


def _browser_pool_ttl_sec() -> int:
    return 300


def _browser_pool_shutdown_state(st: Dict[str, Any]) -> None:
    try:
        ctx = st.get("context")
        if ctx:
            ctx.close()
    except Exception as e:
        log.debug("p0 graph screenshot pool: context close: %s", e)
    try:
        pw = st.get("pw")
        if pw:
            pw.stop()
    except Exception as e:
        log.debug("p0 graph screenshot pool: playwright stop: %s", e)


def _browser_pool_close() -> None:
    with _BROWSER_POOL_LOCK:
        st = dict(_BROWSER_POOL)
        _BROWSER_POOL.clear()
    _browser_pool_shutdown_state(st)


# A cold persistent-context launch must never hang forever waiting on a profile lock.
_PERSISTENT_LAUNCH_TIMEOUT_MS = 60_000


def _kill_stale_grafana_chromium(user_data: str) -> None:
    """
    Kill any orphaned Chromium still holding the persistent Grafana profile. A pooled browser
    survives a ``systemctl restart`` (the child is not reaped), keeps the ``user_data_dir`` locked,
    and makes the next cold ``launch_persistent_context`` hang forever. Call ONLY on a cold launch —
    captures are serialised by ``_capture_busy``, so this can't kill a live capture's browser.
    """
    ud = (user_data or "").strip()
    if not ud:
        return
    try:
        import subprocess

        # Pattern must NOT start with '-' or pkill treats it as an option. Drop the leading dashes;
        # "-f" still matches the substring anywhere in the full command line.
        r = subprocess.run(["pkill", "-f", f"user-data-dir={ud}"], timeout=10)
        if r.returncode == 0:
            log.warning(
                "p0 graph screenshot: killed stale Chromium holding profile %s (freeing lock)", ud
            )
            time.sleep(1.0)  # let the OS release the SingletonLock before relaunch
    except Exception as e:
        log.warning("p0 graph screenshot: stale-chromium cleanup failed: %s", e)


def _channel_not_installed(exc: Exception, channel: str) -> bool:
    """
    True when ``exc`` indicates the requested Chromium ``channel`` (full build for new headless)
    is not installed on this box — i.e. only the headless *shell* was downloaded. In that case
    the caller should retry WITHOUT a channel so captures keep working (bundled headless shell),
    while logging that panels may render black until ``playwright install chromium`` is run.
    """
    if not channel:
        return False
    msg = str(exc).lower()
    return (
        "executable doesn't exist" in msg
        or "is not found" in msg
        or "please run the following command to download" in msg
        or "looks like playwright" in msg
    )


def _launch_persistent_context_with_channel(
    pw, user_data, *, headless, channel, viewport, device_scale_factor, args, timeout
):
    """
    ``launch_persistent_context`` with the new-headless ``channel`` (default ``chromium`` on Linux),
    falling back to the bundled headless shell if that full build is not installed. Fail loud, not
    wedged: a missing full build logs a clear "run playwright install chromium" line and still
    returns a (headless-shell) context rather than crashing the whole capture.
    """
    try:
        return pw.chromium.launch_persistent_context(
            user_data,
            headless=headless,
            channel=(channel or None),
            viewport=viewport,
            device_scale_factor=device_scale_factor,
            args=args,
            timeout=timeout,
        )
    except Exception as e:
        if _channel_not_installed(e, channel):
            log.error(
                "p0 graph screenshot: Chromium channel=%s not installed (%s) — falling back to the "
                "bundled headless SHELL, which may render Grafana panels BLACK. Fix: run "
                "`playwright install chromium` (full build) on the box, then retry.",
                channel,
                e,
            )
            return pw.chromium.launch_persistent_context(
                user_data,
                headless=headless,
                viewport=viewport,
                device_scale_factor=device_scale_factor,
                args=args,
                timeout=timeout,
            )
        raise


def _browser_pool_acquire(
    *,
    user_data: str,
    w: int,
    h: int,
    dsf: float,
    launch_args: List[str],
    headless: bool,
    channel: str = "",
) -> Optional[Tuple[Any, Any, Any, bool]]:
    """Return ``(playwright, context, page, reused)`` from warm pool, or ``None`` to cold-start."""
    from p0_logic import config as _config

    if not (
        _config.get_p0_graph_screenshot_browser_pool_enabled()
        and user_data
        and not _capture_ctx_force_full()
    ):
        return None
    now = time.time()
    with _BROWSER_POOL_LOCK:
        last = float(_BROWSER_POOL.get("last") or 0)
        if _BROWSER_POOL.get("context") and now - last > _browser_pool_ttl_sec():
            log.info("p0 graph screenshot pool: idle TTL expired — restarting browser")
            st_old = dict(_BROWSER_POOL)
            _BROWSER_POOL.clear()
            _browser_pool_shutdown_state(st_old)
        if _BROWSER_POOL.get("page") and _BROWSER_POOL.get("context"):
            _BROWSER_POOL["last"] = now
            log.info("p0 graph screenshot pool: reusing warm Chromium (skip cold launch)")
            return (
                _BROWSER_POOL.get("pw"),
                _BROWSER_POOL.get("context"),
                _BROWSER_POOL.get("page"),
                True,
            )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        _kill_stale_grafana_chromium(user_data)
        pw = sync_playwright().start()
        context = _launch_persistent_context_with_channel(
            pw,
            user_data,
            headless=headless,
            channel=channel,
            viewport={"width": w, "height": h},
            device_scale_factor=dsf,
            args=launch_args,
            timeout=_PERSISTENT_LAUNCH_TIMEOUT_MS,
        )
        page = context.pages[0] if context.pages else context.new_page()
        with _BROWSER_POOL_LOCK:
            _BROWSER_POOL.update(pw=pw, context=context, page=page, last=time.time())
        log.info("p0 graph screenshot pool: started warm Chromium at %s", user_data)
        return pw, context, page, False
    except Exception as e:
        log.warning("p0 graph screenshot pool: start failed — cold capture fallback: %s", e)
        return None


def _browser_pool_touch() -> None:
    with _BROWSER_POOL_LOCK:
        if _BROWSER_POOL.get("context"):
            _BROWSER_POOL["last"] = time.time()


def _derive_grafana_render_url(dashboard_url: str) -> str:
    """
    Turn a normal Grafana dashboard URL into its server-side Render-API URL:
    ``https://<host>/d/<uid>/<slug>?...`` → ``https://<host>/render/d/<uid>/<slug>?...``.

    Preserves ``orgId`` / ``from`` / ``to`` / timezone already on the URL, drops ``refresh``
    (pointless for a one-shot render), and appends ``width`` / ``height`` (and ``tz`` when the
    capture timezone is known and not already present). Idempotent if ``/render`` is already there.
    """
    from p0_logic import config as _config

    u = (dashboard_url or "").strip() or _config.get_p0_graph_screenshot_url()
    if not u:
        return ""
    parsed = urlparse(u)
    path = parsed.path or "/"
    if not path.startswith("/render/"):
        idx = path.find("/d/")
        if idx < 0:
            # Not a dashboard-style path we recognise — cannot build a render URL.
            return ""
        path = path[:idx] + "/render" + path[idx:]

    q = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
         if k.lower() not in ("refresh", "width", "height")]
    w = _config.get_p0_graph_screenshot_viewport_width()
    h = _config.get_p0_graph_screenshot_render_height()
    q.append(("width", str(w)))
    q.append(("height", str(h)))
    # Kiosk = panels-only (no Grafana left nav / top chrome) so the render is full-width and sized
    # like the old capture, not squished beside the sidebar. Honors P0_GRAPH_SCREENSHOT_KIOSK.
    # Grafana 11 dropped ``kiosk=tv`` (the nav dock/sidebar stayed and leaked into the render); the
    # current form is a bare valueless ``kiosk`` param, appended as ``&kiosk`` (no ``=``) below.
    add_kiosk = _config.get_p0_graph_screenshot_append_kiosk() and not any(k.lower() == "kiosk" for k, _ in q)
    if not any(k.lower() == "tz" for k, _ in q):
        tz_name = (_config.get_p0_graph_screenshot_timezone_name() or "").strip()
        if tz_name:
            q.append(("tz", tz_name))
    query = urlencode(q)
    if add_kiosk:
        query = (query + "&kiosk") if query else "kiosk"
    return urlunparse(parsed._replace(path=path, query=query))


def _autocrop_render_vertical(png_bytes: bytes) -> bytes:
    """
    Trim the uniform page-background padding above/below the actual dashboard content.

    We render deliberately TALL (see ``RENDER_HEIGHT`` default) so the whole dashboard is always
    captured — this then removes the leftover black band at the bottom (and any top gap) so the
    2-band split lands on real panels instead of empty space, without anyone tuning the height to
    the dashboard's exact pixel size. Full width is preserved (never trims left/right, so panel
    legends stay intact). No-ops if Pillow is missing or there's nothing meaningful to trim.
    """
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return png_bytes
    try:
        im = Image.open(BytesIO(png_bytes))
        im.load()
        rgb = im.convert("RGB")
        bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
        bbox = ImageChops.difference(rgb, bg).getbbox()
    except Exception as e:
        log.warning("p0 graph screenshot render: autocrop failed to inspect PNG: %s", e)
        return png_bytes
    if not bbox:
        return png_bytes
    _, top, _, bottom = bbox
    margin = 12
    top = max(0, top - margin)
    bottom = min(im.height, bottom + margin)
    if bottom - top >= im.height - 1:
        return png_bytes  # content already fills the frame — nothing to trim
    try:
        cropped = im.crop((0, top, im.width, bottom))
        buf = BytesIO()
        cropped.save(buf, format="PNG", optimize=True)
    except Exception as e:
        log.warning("p0 graph screenshot render: autocrop encode failed, using full image: %s", e)
        return png_bytes
    log.info(
        "p0 graph screenshot render: autocrop %spx -> %spx tall (trimmed background padding)",
        im.height,
        cropped.height,
    )
    return buf.getvalue()


def _split_png_vertical_by_ratio(png_bytes: bytes, ratio: float) -> List[bytes]:
    """
    Split one tall PNG into a top band (height = ratio × total) and a bottom band.
    Falls back to the whole image as a single part if Pillow is missing or the split fails.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning(
            "p0 graph screenshot render: Pillow not installed — posting single render PNG "
            "(install pillow for the 2-band split)"
        )
        return [png_bytes]
    try:
        im = Image.open(BytesIO(png_bytes))
        im.load()
    except Exception as e:
        log.warning("p0 graph screenshot render: failed to open render PNG for split: %s", e)
        return [png_bytes]
    w, h = im.size
    if h < 8:
        return [png_bytes]
    r = max(0.1, min(float(ratio or 0.5), 0.9))
    mid = max(1, min(int(h * r), h - 1))
    out: List[bytes] = []
    for box in ((0, 0, w, mid), (0, mid, w, h)):
        try:
            crop = im.crop(box)
            buf = BytesIO()
            crop.save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
        except Exception as e:
            log.warning("p0 graph screenshot render: failed to encode render split band: %s", e)
            return [png_bytes]
    return out


def _capture_via_grafana_render_api(capture_url: Optional[str] = None) -> Tuple[List[bytes], str]:
    """
    PRIMARY capture path (no browser): fetch the dashboard from the Grafana server-side Render API
    (``grafana-image-renderer``) over HTTP with basic auth, validate it's a real PNG, then split the
    single tall image into the usual 2 Lark bands (top / bottom).

    Returns ``(pngs, captured_at)`` — empty list on any failure so the caller can fall back to
    Playwright (unless ``P0_GRAPH_SCREENSHOT_RENDER_API_STRICT=1``).
    """
    from p0_logic import config as _config

    render_url = _derive_grafana_render_url((capture_url or "").strip())
    if not render_url:
        _set_capture_error("Could not derive Render-API URL from P0_GRAPH_SCREENSHOT_URL (no /d/ path).")
        log.warning(
            "p0 graph screenshot render: cannot derive /render URL from %r",
            (capture_url or "")[:80],
        )
        return [], ""

    user = _config.get_p0_graph_screenshot_username()
    pwd = _config.get_p0_graph_screenshot_password()
    if not user or not pwd:
        _set_capture_error("Render API needs P0_GRAPH_SCREENSHOT_USERNAME / _PASSWORD (basic auth).")
        log.warning("p0 graph screenshot render: missing basic-auth username/password")
        return [], ""

    timeout_sec = _config.get_p0_graph_screenshot_render_timeout_sec()
    tz = _resolve_capture_tz()

    try:
        import requests
    except ImportError:
        _set_capture_error("requests not installed on server (needed for Render API).")
        log.warning("p0 graph screenshot render: requests not installed")
        return [], ""

    # Log without query so from/to/tz are visible but no secrets leak (auth is in the header).
    log.info(
        "p0 graph screenshot render: GET %s?<params> width/height=%sx%s timeout=%ss",
        render_url.split("?", 1)[0],
        _config.get_p0_graph_screenshot_viewport_width(),
        _config.get_p0_graph_screenshot_render_height(),
        timeout_sec,
    )
    try:
        resp = requests.get(
            render_url,
            auth=(user, pwd),
            timeout=timeout_sec,
            stream=False,
        )
    except Exception as e:
        _set_capture_error(f"Render API request failed: {e}")
        log.warning("p0 graph screenshot render: HTTP request failed: %s", e)
        return [], ""

    status = resp.status_code
    ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    body = resp.content or b""
    if status != 200 or ctype != "image/png" or len(body) < 3000:
        _set_capture_error(
            f"Render API returned HTTP={status} type={ctype or '?'} size={len(body)} "
            "(expected 200 image/png > 3KB)."
        )
        log.warning(
            "p0 graph screenshot render: bad response HTTP=%s type=%s size=%s first=%r",
            status,
            ctype or "?",
            len(body),
            body[:80],
        )
        return [], ""

    cap_time = _format_captured_at(datetime.now(tz))
    body = _autocrop_render_vertical(body)
    parts = _split_png_vertical_by_ratio(body, _config.get_p0_graph_screenshot_render_split_ratio())
    log.info(
        "p0 graph screenshot render: OK size=%s bytes → %s image part(s) captured_at=%s",
        len(body),
        len(parts),
        cap_time,
    )
    return parts, cap_time


def _capture_png_payloads(capture_url: Optional[str] = None) -> Tuple[List[bytes], str]:
    """
    Returns a non-empty list of PNG byte blobs and a formatted capture timestamp.
    On-demand: retries once with full cold capture if the first attempt returns empty.

    PRIMARY path: when ``P0_GRAPH_SCREENSHOT_USE_RENDER_API=1`` the Grafana server-side Render API is
    used (no browser). On render failure it falls back to Playwright below unless
    ``P0_GRAPH_SCREENSHOT_RENDER_API_STRICT=1``.
    """
    from p0_logic import config as _config

    if _config.get_p0_graph_screenshot_use_render_api():
        pngs, captured_at = _capture_via_grafana_render_api(capture_url)
        if pngs:
            return pngs, captured_at
        if _config.get_p0_graph_screenshot_render_api_strict():
            log.warning(
                "p0 graph screenshot render: failed and RENDER_API_STRICT=1 — not falling back to Playwright"
            )
            return [], ""
        log.info(
            "p0 graph screenshot render: failed — falling back to Playwright capture (%s)",
            _get_capture_error() or "no detail",
        )

    if _is_on_demand_capture() and _effective_fast_capture():
        attempts = 1
    else:
        attempts = 2 if _is_on_demand_capture() else 1
    for attempt in range(attempts):
        if attempt > 0:
            log.info("p0 graph screenshot: on-demand retry — full cold capture (attempt %s)", attempt + 1)
            _browser_pool_close()
            _capture_ctx.force_full = True
        pngs, captured_at = _capture_png_payloads_once(capture_url)
        if pngs:
            return pngs, captured_at
    return [], ""


def _capture_png_payloads_once(capture_url: Optional[str] = None) -> Tuple[List[bytes], str]:
    """
    Single Playwright capture attempt. With ``P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY=1`` and scroll count > 1,
    returns one PNG per scroll step (up to 8).
    """
    from p0_logic import config as _config

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set_capture_error("Playwright not installed on server.")
        log.warning("p0 graph screenshot: playwright not installed (pip install playwright; playwright install chromium)")
        return [], ""

    raw_url = (capture_url or "").strip() or _config.get_p0_graph_screenshot_url()
    kiosk_on = _config.get_p0_graph_screenshot_append_kiosk()
    url = _prepare_grafana_capture_url(raw_url, kiosk=kiosk_on)
    if kiosk_on and url != raw_url:
        log.info("p0 graph screenshot: capture URL adjusted (kiosk / no auto-refresh)")
    clip_selectors = _config.get_p0_graph_screenshot_clip_selectors()
    w = _config.get_p0_graph_screenshot_viewport_width()
    h = _config.get_p0_graph_screenshot_viewport_height()
    wait_ms = _config.get_p0_graph_screenshot_wait_ms()
    nav_ms = _config.get_p0_graph_screenshot_nav_timeout_ms()
    full_page = _config.get_p0_graph_screenshot_full_page()
    split_halves = _config.get_p0_graph_screenshot_split_vertical_halves()
    goto_wait = _config.get_p0_graph_screenshot_goto_wait_until()
    user_data = _config.get_p0_graph_screenshot_playwright_user_data_dir()
    tz = _resolve_capture_tz()

    def _snap(page) -> Tuple[List[bytes], str]:
        at = datetime.now(tz)
        cap_time = _format_captured_at(at)
        if _config.get_p0_graph_screenshot_viewport_only():
            log.info(
                "p0 graph screenshot: P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY=1 — skip CSS clip/body clip chain"
            )
            if _config.get_p0_graph_screenshot_top_and_bottom():
                top_bottom = _viewport_top_and_bottom_screenshots(page, h, url)
                if len(top_bottom) >= 2:
                    return top_bottom, cap_time
                if len(top_bottom) == 1:
                    return top_bottom, cap_time
                log.info("p0 graph screenshot: top+bottom empty — fallback scroll chain")
            scroll_n = _config.get_p0_graph_screenshot_viewport_scroll_count()
            if scroll_n >= 2:
                scroll_parts = _viewport_scroll_chain_screenshots(page, h, scroll_n)
                if len(scroll_parts) >= 2:
                    return scroll_parts, cap_time
                if len(scroll_parts) == 1:
                    if split_halves:
                        halved = _split_png_vertical_halves(scroll_parts[0])
                        if len(halved) == 2:
                            return halved, cap_time
                    return scroll_parts, cap_time
                log.info(
                    "p0 graph screenshot: viewport scroll chain empty — "
                    "fallback single viewport (Pillow split if split_halves)"
                )
            raw = page.screenshot(full_page=False, type="png")
            if split_halves:
                parts = _split_png_vertical_halves(raw)
                if len(parts) == 2:
                    return parts, cap_time
                if not parts:
                    return [raw], cap_time
            return [raw], cap_time
        if _config.get_p0_graph_screenshot_full_document():
            log.info(
                "p0 graph screenshot: FULL_DOCUMENT=1 — full **scroll height** (can be very tall / mostly empty). "
                "For **two viewport screenshots** (top of board, then scrolled down), use "
                "P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY=1 + SPLIT_VERTICAL_HALVES=1 and turn FULL_DOCUMENT off."
            )
            raw = page.screenshot(full_page=True, type="png")
            if split_halves:
                parts = _split_png_vertical_halves(raw)
                if len(parts) == 2:
                    return parts, cap_time
                if not parts:
                    log.warning(
                        "p0 graph screenshot: Pillow split failed — posting single full-document PNG "
                        "(install Pillow for two-part vertical split)"
                    )
                return [raw], cap_time
            return [raw], cap_time
        clip: Optional[Dict[str, int]] = None
        clip_sel: Optional[str] = None
        if clip_selectors:
            clip, clip_sel = _pick_dashboard_clip(page, clip_selectors, w, h)
        if split_halves:
            log.info(
                "p0 graph screenshot: split vertical halves viewport=%sx%s effective_clip=%s",
                w,
                h,
                clip if clip else "full document (no selector match)",
            )
            if clip and clip_sel:
                scroll_sel = _mark_best_scroll_target_under_root(page, clip_sel)
                try:
                    vh = _scrollbar_virtualized_metrics(page, scroll_sel)
                    vis_root = _measure_visible_clip_rect(page, clip_sel)
                    vis_h = int((vis_root or {}).get("height") or 0)
                    clip_h = int(clip.get("height") or 0)
                    # Tall logical clip vs visible dashboard body → geometric bisect is unsafe (virtualized / undrawn band).
                    tall_clip = vis_h > 80 and clip_h > int(vis_h * 1.15)
                    if vh:
                        sh, ch = vh
                        max_scroll = max(0, sh - ch)
                        # Slightly above 1.0: 50 % zoom and nested layouts often sit just above 1.2×.
                        looks_virtualized = ch > 80 and sh > int(ch * 1.05) and max_scroll > 0
                        # Grafana table bodies use a tall scrollHeight but only paint the viewport —
                        # bisecting document clip yields a black upper half.
                        if looks_virtualized or (tall_clip and max_scroll > 0):
                            log.info(
                                "p0 graph screenshot: viewport pair capture scroll_h=%s client_h=%s "
                                "max_scroll=%s scroll_sel=%s tall_clip=%s",
                                sh,
                                ch,
                                max_scroll,
                                scroll_sel[:48] + ("…" if len(scroll_sel) > 48 else ""),
                                tall_clip,
                            )
                            try:
                                page.evaluate(
                                    """(sel) => {
                                      const e = document.querySelector(sel);
                                      if (e) e.scrollTop = 0;
                                    }""",
                                    scroll_sel,
                                )
                                page.wait_for_timeout(450)
                                vis1 = _measure_visible_clip_rect(page, scroll_sel)
                                if not vis1:
                                    vis1 = _measure_visible_clip_rect(page, clip_sel) or clip
                                p1 = page.screenshot(full_page=True, type="png", clip=vis1)
                                bottom_st = max_scroll
                                page.evaluate(
                                    """({ sel, st }) => {
                                      const e = document.querySelector(sel);
                                      if (e) e.scrollTop = st;
                                    }""",
                                    {"sel": scroll_sel, "st": bottom_st},
                                )
                                page.wait_for_timeout(600)
                                vis2 = (
                                    _measure_visible_clip_rect(page, scroll_sel)
                                    or vis1
                                )
                                p2 = page.screenshot(full_page=True, type="png", clip=vis2)
                                if p1 and p2:
                                    try:
                                        page.evaluate(
                                            """(sel) => {
                                              const e = document.querySelector(sel);
                                              if (e) e.scrollTop = 0;
                                            }""",
                                            scroll_sel,
                                        )
                                    except Exception:
                                        pass
                                    return [p1, p2], cap_time
                            except Exception as e:
                                log.warning(
                                    "p0 graph screenshot: virtualized dual viewport capture failed: %s",
                                    e,
                                )
                    if tall_clip:
                        log.info(
                            "p0 graph screenshot: skip geometric clip split (clip_h=%s >> vis_h=%s) — single full_page",
                            clip_h,
                            vis_h,
                        )
                        raw_fb = page.screenshot(full_page=True, type="png")
                        parts_fb = _split_png_vertical_halves(raw_fb)
                        if len(parts_fb) == 2:
                            return parts_fb, cap_time
                        return [raw_fb], cap_time
                    c1, c2 = _split_clip_vertical_halves(clip)
                    try:
                        p1 = page.screenshot(full_page=True, type="png", clip=c1)
                        p2 = page.screenshot(full_page=True, type="png", clip=c2)
                        if p1 and p2:
                            return [p1, p2], cap_time
                    except Exception as e:
                        log.warning("p0 graph screenshot: dual clip screenshot failed: %s", e)
                finally:
                    _clear_p0_scroll_target_marks(page)
            raw = page.screenshot(full_page=True, type="png")
            parts = _split_png_vertical_halves(raw)
            if len(parts) == 2:
                return parts, cap_time
            if not parts:
                log.warning("p0 graph screenshot: split failed — posting single full_page PNG")
            else:
                log.warning("p0 graph screenshot: unexpected split part count=%s — single PNG", len(parts))
            return [raw], cap_time
        if clip:
            try:
                raw = page.screenshot(full_page=True, type="png", clip=clip)
                return [raw], cap_time
            except Exception as e:
                log.warning("p0 graph screenshot: clipped screenshot failed, falling back: %s", e)
        raw = page.screenshot(full_page=full_page, type="png")
        return [raw], cap_time

    def _snap_with_blank_viewport_fallback(page) -> Tuple[List[bytes], str]:
        out = _snap(page)
        pngs, cap = out
        if not _config.get_p0_graph_screenshot_blank_fallback_viewport():
            return out
        if not pngs or not _png_list_all_uniformly_blank(pngs):
            return out
        log.warning(
            "p0 graph screenshot: capture looks uniformly blank — retry viewport-only "
            "(try P0_GRAPH_SCREENSHOT_SWIFTSHADER=1 on Linux if still black; see env.example)"
        )
        at2 = datetime.now(tz)
        cap2 = _format_captured_at(at2)
        if split_halves and _config.get_p0_graph_screenshot_viewport_only():
            n_pages = _config.get_p0_graph_screenshot_viewport_scroll_count()
            if n_pages >= 2:
                chain = _viewport_scroll_chain_screenshots(page, h, n_pages)
                if len(chain) >= 2:
                    pngs2 = chain
                elif len(chain) == 1:
                    halved = _split_png_vertical_halves(chain[0])
                    pngs2 = halved if len(halved) == 2 else chain
                else:
                    raw = page.screenshot(full_page=False, type="png")
                    parts = _split_png_vertical_halves(raw)
                    pngs2 = parts if len(parts) == 2 else [raw]
            else:
                raw = page.screenshot(full_page=False, type="png")
                parts = _split_png_vertical_halves(raw)
                pngs2 = parts if len(parts) == 2 else [raw]
        else:
            raw = page.screenshot(full_page=False, type="png")
            if split_halves:
                parts = _split_png_vertical_halves(raw)
                pngs2 = parts if len(parts) == 2 else [raw]
            else:
                pngs2 = [raw]
        if not pngs2 or _png_list_all_uniformly_blank(pngs2):
            log.warning(
                "p0 graph screenshot: viewport retry still blank — install pillow, use "
                "HEADED=1 on VNC, or verify Grafana login/session in profile"
            )
            return out
        log.info("p0 graph screenshot: viewport-only retry succeeded (non-blank)")
        return pngs2, cap2

    launch_args = list(_config.get_p0_graph_screenshot_chromium_args())
    if _config.get_p0_graph_screenshot_swiftshader():
        extra = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        launch_args.extend([a for a in extra if a not in launch_args])
        log.info("p0 graph screenshot: SwiftShader (ANGLE) flags enabled for headless GL")
    headless = _config.get_p0_graph_screenshot_playwright_headless()
    channel = _config.get_p0_graph_screenshot_chromium_channel()
    if channel:
        log.info(
            "p0 graph screenshot: Chromium channel=%s (new headless mode — real GPU/canvas compositing)",
            channel,
        )
    snap_full = split_halves or full_page or bool(clip_selectors)
    dsf = _config.get_p0_graph_screenshot_device_scale_factor()
    zoom_pct = _config.get_p0_graph_screenshot_zoom_percent()
    if dsf > 1.0 and zoom_pct < 100:
        log.warning(
            "p0 graph screenshot: ZOOM_PERCENT=%s with DEVICE_SCALE_FACTOR=%s often looks wrong in Lark — use DSF=1",
            zoom_pct,
            dsf,
        )
    pooled = _browser_pool_acquire(
        user_data=user_data,
        w=w,
        h=h,
        dsf=dsf,
        launch_args=launch_args,
        headless=headless,
        channel=channel,
    )
    if pooled:
        _pw, _ctx, page, reused = pooled
        log.info(
            "p0 graph screenshot: pooled capture viewport=%sx%s reused=%s on_demand_fast=%s",
            w,
            h,
            reused,
            _is_on_demand_capture() and _effective_fast_capture(),
        )
        try:
            _load_grafana_dashboard_for_capture(page, url, navigate_only=reused)
            out = _snap_with_blank_viewport_fallback(page)
            if out[0]:
                _browser_pool_touch()
                return out
            log.warning("p0 graph screenshot: pooled snap returned no images — cold fallback")
            _set_capture_error("Pooled capture produced no images.")
            _browser_pool_close()
        except RuntimeError as e:
            _set_capture_error(str(e))
            log.error("p0 graph screenshot: %s", e)
            _browser_pool_close()
        except Exception as e:
            _set_capture_error(f"Pooled capture error: {e}")
            log.warning("p0 graph screenshot: pooled capture failed — cold fallback: %s", e)
            _browser_pool_close()

    with sync_playwright() as p:
        if user_data:
            log.info(
                "p0 graph screenshot: using persistent profile (Grafana session) at %s headless=%s",
                user_data,
                headless,
            )
            _kill_stale_grafana_chromium(user_data)
            context = _launch_persistent_context_with_channel(
                p,
                user_data,
                headless=headless,
                channel=channel,
                viewport={"width": w, "height": h},
                device_scale_factor=dsf,
                args=launch_args,
                timeout=_PERSISTENT_LAUNCH_TIMEOUT_MS,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                log.info(
                    "p0 graph screenshot: goto wait_until=%s full_page=%s viewport=%sx%s "
                    "device_scale_factor=%s wait_after_ms=%s",
                    goto_wait,
                    snap_full,
                    w,
                    h,
                    dsf,
                    wait_ms,
                )
                try:
                    _load_grafana_dashboard_for_capture(page, url)
                except RuntimeError as e:
                    _set_capture_error(str(e))
                    log.error("p0 graph screenshot: %s", e)
                    return [], ""
                out = _snap_with_blank_viewport_fallback(page)
                if not out[0]:
                    _set_capture_error("Cold capture (persistent profile) returned no images.")
                return out
            finally:
                context.close()
        try:
            browser = p.chromium.launch(
                headless=headless, channel=(channel or None), args=launch_args
            )
        except Exception as e:
            if _channel_not_installed(e, channel):
                log.error(
                    "p0 graph screenshot: Chromium channel=%s not installed (%s) — falling back to "
                    "bundled headless SHELL (panels may render BLACK). Run `playwright install "
                    "chromium` on the box.",
                    channel,
                    e,
                )
                browser = p.chromium.launch(headless=headless, args=launch_args)
            else:
                raise
        try:
            page = browser.new_page(
                viewport={"width": w, "height": h},
                device_scale_factor=dsf,
            )
            log.info(
                "p0 graph screenshot: goto wait_until=%s full_page=%s viewport=%sx%s "
                "device_scale_factor=%s wait_after_ms=%s",
                goto_wait,
                snap_full,
                w,
                h,
                dsf,
                wait_ms,
            )
            try:
                _load_grafana_dashboard_for_capture(page, url)
            except RuntimeError as e:
                _set_capture_error(str(e))
                log.error("p0 graph screenshot: %s", e)
                return [], ""
            out = _snap_with_blank_viewport_fallback(page)
            if not out[0]:
                _set_capture_error("Cold capture returned no images — check Grafana URL/login.")
            return out
        finally:
            browser.close()
