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
content wait can follow from config.

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
**Solid black** on Linux headless is often missing GPU compositing — SwiftShader flags are enabled by
default on Linux (see ``get_p0_graph_screenshot_swiftshader``); set ``P0_GRAPH_SCREENSHOT_SWIFTSHADER=0`` to force off.
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
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Playwright: imported inside ``_capture_png_payloads`` only, so this module loads even when
# ``playwright`` is not installed (optional feature / lighter test imports).

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (see p0_logic/requirements.txt backports.zoneinfo)
    from backports.zoneinfo import ZoneInfo

log = logging.getLogger("lark-ops-ai")


def _apply_kiosk_to_grafana_url(url: str, enable: bool) -> str:
    """Append ``kiosk=tv`` (+ hide dashboard chrome) when missing — Grafana 11 often ignores kiosk alone."""
    u = (url or "").strip()
    if not u or not enable:
        return u
    low = u.lower()
    parsed = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
    if "kiosk" not in low:
        q = [(k, v) for k, v in q if k.lower() != "kiosk"]
        q.append(("kiosk", "tv"))
    if not any(k.lower() == "_dash.hidetimepicker" for k, _ in q):
        q.append(("_dash.hideTimePicker", "true"))
    if not any(k.lower() == "_dash.hidevariables" for k, _ in q):
        q.append(("_dash.hideVariables", "true"))
    new_query = urlencode(q)
    return urlunparse(parsed._replace(query=new_query))


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
    w = min(3840, max(320, int(round(base_w * factor))))
    h = min(2160, max(240, int(round(base_h * factor))))
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
            """({ scale, invPct }) => {
              document.documentElement.style.zoom = '';
              document.body.style.zoom = '';
              document.querySelectorAll(
                '[data-testid="dashboard-scene"], .dashboard-container, '
                + '[data-testid="page-content"], main .page-body, main'
              ).forEach((el) => {
                el.style.transform = 'scale(' + scale + ')';
                el.style.transformOrigin = 'top left';
                el.style.width = invPct + '%';
                el.style.maxWidth = invPct + '%';
              });
            }""",
            {"scale": scale, "invPct": inv_pct},
        )
        log.info(
            "p0 graph screenshot: top+bottom scene zoom=%s%% (base viewport)",
            pct,
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
    from . import config as _config

    return _config.get_p0_graph_screenshot_viewport_only() and _config.get_p0_graph_screenshot_top_and_bottom()


def _prepare_grafana_dashboard_for_capture(page, dashboard_url: str) -> None:
    """Kiosk re-goto, undock nav, apply zoom — before screenshots."""
    from . import config as _config

    kiosk_on = _config.get_p0_graph_screenshot_append_kiosk()
    u = _apply_kiosk_to_grafana_url((dashboard_url or "").strip(), kiosk_on)
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
        if top_bottom:
            cap_w, cap_h = base_w, base_h
            log.info(
                "p0 graph screenshot: top+bottom framing — viewport=%sx%s scene_zoom=%s%%",
                cap_w,
                cap_h,
                zoom_pct,
            )
        else:
            cap_w, cap_h = _effective_capture_viewport(base_w, base_h, zoom_pct)
            if cap_w != base_w or cap_h != base_h:
                log.info(
                    "p0 graph screenshot: capture viewport=%sx%s (base=%sx%s zoom=%s%%)",
                    cap_w,
                    cap_h,
                    base_w,
                    base_h,
                    zoom_pct,
                )
        page.set_viewport_size({"width": cap_w, "height": cap_h})
    except Exception as e:
        log.debug("p0 graph screenshot: set_viewport_size: %s", e)
    _ensure_grafana_sidebar_collapsed(page)
    if top_bottom:
        _apply_dashboard_scene_zoom(page, _config.get_p0_graph_screenshot_zoom_percent())
    else:
        _clear_capture_zoom_styles(page)
        _apply_page_zoom_percent(page, _config.get_p0_graph_screenshot_zoom_percent())
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


def _measure_grafana_highlight_band_clips(page, *, include_login_panel: bool = True) -> List[Dict[str, int]]:
    """
    Two bands (``include_login_panel=False``, default VNC-style):
      Band 1 = Apisix/KPI/FPMS (stops before Login With Password).
      Band 2 = Login With Password + CPMS/IGO/Pulsar (login strip at top of 2nd PNG).
    Three bands (``include_login_panel=True``):
      Band 1 = top block; band 2 = CPMS/IGO/Pulsar; band 3 = Login panel only.
    """
    try:
        raw = page.evaluate(
            """({ includeLogin }) => {
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
                for (const el of document.querySelectorAll(
                  'h2, h3, button, div, span, [class*="row"], [data-testid*="row" i]'
                )) {
                  const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
                  if (!t || t.length > maxLen) continue;
                  if (!re.test(t)) continue;
                  const r = el.getBoundingClientRect();
                  if (r.height > 180) continue;
                  return sy + r.top - 4;
                }
                return null;
              };

              const loginRe = /login\\s*(with\\s*)?password/i;
              const cpmsRe = /CPMS1\\.0|CPMS2\\.0|CPMS\\s*1\\.0|CPMS1\\.0\\s*\\/\\s*CPMS2\\.0/i;

              let loginSplit = findSplitY(loginRe, 120);
              let cpmsSplit = findSplitY(cpmsRe, 120);

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
              const band2Start = includeLogin
                ? cpmsSplit
                : ((loginSplit != null && loginSplit < cpmsSplit) ? loginSplit : cpmsSplit);
              const midPanels = includeLogin
                ? panels.filter((p) => !isLoginPanel(p) && p.top >= cpmsSplit - 6)
                : panels.filter((p) => p.top >= band2Start - 6);
              const loginPanels = panels.filter((p) => isLoginPanel(p));

              const u1 = union(topPanels.length ? topPanels : panels.filter((p) => p.top < band2Start));
              const u2 = union(midPanels.length ? midPanels : panels.filter((p) => p.top >= band2Start));
              if (!u1 || !u2) return null;

              const pad = 8;
              const mk = (u, minH) => ({
                x: Math.max(left, Math.floor(u.x0 - pad)),
                y: Math.max(0, Math.floor(u.y0 - pad)),
                width: Math.max(320, Math.ceil(u.x1 - u.x0 + pad * 2)),
                height: Math.max(minH, Math.ceil(u.y1 - u.y0 + pad * 2)),
              });

              const out = {
                loginSplit,
                cpmsSplit,
                band1: mk(u1, 200),
                band2: mk(u2, 200),
                band3: null,
              };

              if (includeLogin && loginPanels.length) {
                const u3 = union(loginPanels);
                if (u3) out.band3 = mk(u3, 72);
              }
              return out;
            }""",
            {"includeLogin": bool(include_login_panel)},
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
        log.info(
            "p0 graph screenshot: highlight bands login_y=%s cpms_y=%s parts=%s sizes=%s",
            raw.get("loginSplit"),
            raw.get("cpmsSplit"),
            len(out),
            [(c["width"], c["height"]) for c in out],
        )
        return out
    except (TypeError, ValueError):
        return []


def _wait_for_charts_in_document_band(page, doc_clip: Dict[str, int], *, timeout_ms: int = 15000) -> None:
    """Wait for canvas/SVG inside a band clip — bottom band is off-screen until scroll."""
    if timeout_ms <= 0:
        return
    try:
        page.wait_for_function(
            """(clip) => {
              const sy = window.scrollY || 0;
              const y0 = clip.y;
              const y1 = clip.y + clip.height;
              const panels = document.querySelectorAll(
                '[data-panelid], .react-grid-item, [data-panel-id]'
              );
              let canv = 0;
              let svg = 0;
              panels.forEach((el) => {
                const r = el.getBoundingClientRect();
                const docTop = sy + r.top;
                const docBot = sy + r.bottom;
                if (docBot < y0 || docTop > y1) return;
                el.querySelectorAll('canvas').forEach((c) => {
                  const cr = c.getBoundingClientRect();
                  if (cr.width > 80 && cr.height > 40) canv++;
                });
                el.querySelectorAll('svg').forEach((s) => {
                  const sr = s.getBoundingClientRect();
                  if (sr.width > 20 && sr.height > 12) svg++;
                });
              });
              return canv >= 1 || svg >= 3;
            }""",
            doc_clip,
            timeout=timeout_ms,
            polling=500,
        )
    except Exception as e:
        log.debug("p0 graph screenshot: band chart wait: %s", e)


def _screenshot_viewport_at_band_start(page, doc_clip: Dict[str, int]) -> Optional[bytes]:
    """
    VNC-style: scroll to band top, capture **one viewport** (1920×1080), not the full band height.
    Avoids huge 1800×1300+ PNGs from full_page band clips.
    """
    from . import config as _config

    try:
        y = int(doc_clip.get("y") or 0)
        vh = _config.get_p0_graph_screenshot_viewport_height()
        page.evaluate("(y) => window.scrollTo(0, Math.max(0, y - 8))", y)
        page.wait_for_timeout(700)
        vp_band = {
            "x": int(doc_clip.get("x") or 0),
            "y": y,
            "width": int(doc_clip.get("width") or 1920),
            "height": vh,
        }
        _wait_for_charts_in_document_band(page, vp_band, timeout_ms=8000)
        page.wait_for_timeout(400)
        raw = _dashboard_viewport_screenshot(page)
        if raw and not _png_bytes_uniformly_blank(raw):
            return _normalize_screenshot_png(raw)
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


def _screenshot_highlight_bands(page) -> List[bytes]:
    """Capture dashboard bands: 2 (VNC-style) or 3 (optional separate Login PNG)."""
    from . import config as _config

    include_login = _config.get_p0_graph_screenshot_include_login_panel()
    clips = _measure_grafana_highlight_band_clips(page, include_login_panel=include_login)
    if len(clips) < 2:
        return []
    pngs: List[bytes] = []
    labels = (
        ("top KPI/FPMS", "Login+CPMS/IGO/Pulsar")
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
        raw = _screenshot_viewport_at_band_start(page, clip)
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
        from . import config as _config

        _scroll_pair_reset_top(page, scroll_sel)
        extra = min(_config.get_p0_graph_screenshot_wait_ms(), 8000)
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


def _grafana_auto_login_if_needed(page, dashboard_url: str, *, nav_ms: int, goto_wait: str) -> bool:
    """Return True if logged in (or login not required). False if still on login form."""
    from . import config as _config

    user = _config.get_p0_graph_screenshot_username()
    pwd = _config.get_p0_graph_screenshot_password()
    if not user or not pwd:
        return True
    if not _grafana_login_form_visible(page):
        return True
    log.info(
        "p0 graph screenshot: Grafana login page — auto-login user=%s (pwd_len=%s)",
        user,
        len(pwd),
    )
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
        log.warning("p0 graph screenshot: auto-login — username field not found")
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
        log.warning("p0 graph screenshot: auto-login — password field not found")
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
    wait_ms = max(nav_ms, 45_000)
    try:
        page.wait_for_function(
            """() => {
              const grid = document.querySelector(
                '.react-grid-layout, [data-testid="dashboard-layout-grid"]'
              );
              if (grid) {
                const r = grid.getBoundingClientRect();
                if (r.width > 200 && r.height > 120) return true;
              }
              const pw = document.querySelector('input[type="password"], input[name="password"]');
              if (!pw) return true;
              const r = pw.getBoundingClientRect();
              const st = window.getComputedStyle(pw);
              if (r.width < 40 || st.display === 'none' || st.visibility === 'hidden') return true;
              return false;
            }""",
            timeout=wait_ms,
            polling=400,
        )
    except Exception as e:
        log.warning("p0 graph screenshot: auto-login wait for dashboard: %s", e)
    try:
        page.wait_for_load_state(
            goto_wait if goto_wait in ("load", "domcontentloaded", "networkidle") else "load",
            timeout=wait_ms,
        )
    except Exception as e:
        log.debug("p0 graph screenshot: auto-login load_state: %s", e)
    if _grafana_on_dashboard_page(page):
        log.info("p0 graph screenshot: auto-login — dashboard grid visible")
    elif _grafana_login_form_visible(page):
        log.warning(
            "p0 graph screenshot: auto-login failed — login form still visible "
            "(pwd_len=%s — wrong password or use P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR)",
            len(pwd),
        )
        return False
    else:
        log.info(
            "p0 graph screenshot: auto-login — no login form (dashboard may still be loading)"
        )
    dash = (dashboard_url or "").strip()
    if dash and "/d/" in dash and dash not in (page.url or ""):
        try:
            page.goto(dash, wait_until=goto_wait, timeout=nav_ms)
        except Exception as e:
            log.warning("p0 graph screenshot: auto-login redirect to dashboard failed: %s", e)
    log.info("p0 graph screenshot: auto-login succeeded")
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
    from . import config as _config

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


def schedule_p0_graph_screenshot(tenant_token: str, priority: str, source_chat_label: str) -> None:
    """
    Non-blocking: starts a daemon thread to screenshot ``P0_GRAPH_SCREENSHOT_URL`` and post to
    ``P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID``. Only runs for **P0** when env is enabled.
    """
    if (priority or "").strip().upper() != "P0":
        return
    from . import config as _config

    if not _config.p0_graph_screenshot_enabled():
        return
    url = _config.get_p0_graph_screenshot_url()
    chat_id = _config.get_p0_graph_screenshot_target_chat_id()
    if not url or not chat_id:
        log.debug("p0 graph screenshot: disabled or missing URL/target chat")
        return
    tok = (tenant_token or "").strip()
    if not tok:
        return

    label = (source_chat_label or "").strip()

    def _run() -> None:
        try:
            _capture_and_post(tok, url, chat_id, label)
        except Exception as e:
            log.warning("p0 graph screenshot: thread failed: %s", e, exc_info=True)

    t = threading.Thread(target=_run, name="p0-graph-screenshot", daemon=True)
    t.start()


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
) -> None:
    """
    Post the **As of:** line + image part(s) to a Lark group — same as the P0 auto flow.
    Used by ``_capture_and_post`` and by ``scripts/grafana_screenshot_run_once.py --post-lark``.
    """
    from . import config as _config
    from . import lark_client as _lark

    tok = (tenant_token or "").strip()
    cid = (chat_id or "").strip()
    if not tok or not cid:
        log.warning("p0 graph screenshot: post skipped — missing token or chat_id")
        return
    cap = _config.get_p0_graph_screenshot_caption()
    if cap:
        text = cap.replace("{captured_at}", captured_at)
        text = text.replace("{label}", (source_label or "").strip())
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
        return
    pngs = pngs_eff

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
            log.info(
                "p0 graph screenshot: posted image part=%s/%s to chat_id tail=%s",
                idx + 1,
                len(pngs),
                cid[-12:],
            )


def _capture_and_post(token: str, url: str, chat_id: str, source_label: str) -> None:
    pngs, captured_at = _capture_png_payloads()
    if not pngs:
        log.warning("p0 graph screenshot: capture returned empty")
        return
    post_p0_graph_screenshots_to_chat(token, chat_id, pngs, captured_at, source_label)


def _wait_for_grafana_panels_if_configured(page) -> None:
    from . import config as _config

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
    from . import config as _config

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
          if (chartCanv >= 1) return true;
          if (bigSvg >= 4) return true;
          const t = (root.innerText || '').trim();
          if (t.length > 400 && /error|no data|query|failed|exception/i.test(t)) return true;
          const rows = root.querySelectorAll('table tbody tr, [role="rowgroup"] [role="row"]').length;
          if (rows >= 12 && t.length > 2000) return true;
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


def _load_grafana_dashboard_for_capture(page, dashboard_url: str) -> None:
    """Goto dashboard, login if configured, wait for panels, apply kiosk/zoom (same as capture)."""
    from . import config as _config

    nav_ms = _config.get_p0_graph_screenshot_nav_timeout_ms()
    goto_wait = _config.get_p0_graph_screenshot_goto_wait_until()
    wait_ms = _config.get_p0_graph_screenshot_wait_ms()
    _preset_grafana_nav_local_storage(page)
    page.goto(dashboard_url, wait_until=goto_wait, timeout=nav_ms)
    if not _grafana_auto_login_if_needed(page, dashboard_url, nav_ms=nav_ms, goto_wait=goto_wait):
        raise RuntimeError(
            "Grafana login failed — login form still visible. "
            "Use scripts/grafana_playwright_login_once.py + P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR"
        )
    if not _grafana_on_dashboard_page(page):
        log.warning(
            "p0 graph screenshot: dashboard grid not detected after login — "
            "screenshot may be wrong (panels still loading?)"
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
    if wait_ms > 0:
        page.wait_for_timeout(wait_ms)
    _prepare_grafana_dashboard_for_capture(page, dashboard_url)


def open_grafana_dashboard_for_inspection() -> int:
    """
    Open **headed** Chromium with the same viewport / kiosk / scene zoom as P0 capture.
    Blocks until Enter in the terminal (for VNC sizing checks). Does not screenshot or post to Lark.
    """
    from . import config as _config

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
    url = _apply_kiosk_to_grafana_url(raw_url, kiosk_on)
    w = _config.get_p0_graph_screenshot_viewport_width()
    h = _config.get_p0_graph_screenshot_viewport_height()
    user_data = _config.get_p0_graph_screenshot_playwright_user_data_dir()
    dsf = _config.get_p0_graph_screenshot_device_scale_factor()
    zoom_pct = _config.get_p0_graph_screenshot_zoom_percent()
    launch_args = list(_config.get_p0_graph_screenshot_chromium_args())
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
            browser = p.chromium.launch(headless=False, args=launch_args)
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


def _capture_png_payloads() -> Tuple[List[bytes], str]:
    """
    Returns a non-empty list of PNG byte blobs and a formatted capture timestamp.
    With ``P0_GRAPH_SCREENSHOT_VIEWPORT_ONLY=1`` and ``P0_GRAPH_SCREENSHOT_VIEWPORT_SCROLL_COUNT`` > 1 (or split
    on with default 2), returns one PNG per scroll step (up to 8).
    """
    from . import config as _config

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("p0 graph screenshot: playwright not installed (pip install playwright; playwright install chromium)")
        return [], ""

    raw_url = _config.get_p0_graph_screenshot_url()
    kiosk_on = _config.get_p0_graph_screenshot_append_kiosk()
    url = _apply_kiosk_to_grafana_url(raw_url, kiosk_on)
    if kiosk_on and url != raw_url:
        log.info("p0 graph screenshot: appended kiosk=tv to URL (hide Grafana side menu)")
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
    snap_full = split_halves or full_page or bool(clip_selectors)
    dsf = _config.get_p0_graph_screenshot_device_scale_factor()
    zoom_pct = _config.get_p0_graph_screenshot_zoom_percent()
    if dsf > 1.0 and zoom_pct < 100:
        log.warning(
            "p0 graph screenshot: ZOOM_PERCENT=%s with DEVICE_SCALE_FACTOR=%s often looks wrong in Lark — use DSF=1",
            zoom_pct,
            dsf,
        )
    with sync_playwright() as p:
        if user_data:
            log.info(
                "p0 graph screenshot: using persistent profile (Grafana session) at %s headless=%s",
                user_data,
                headless,
            )
            context = p.chromium.launch_persistent_context(
                user_data,
                headless=headless,
                viewport={"width": w, "height": h},
                device_scale_factor=dsf,
                args=launch_args,
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
                    log.error("p0 graph screenshot: %s", e)
                    return [], ""
                return _snap_with_blank_viewport_fallback(page)
            finally:
                context.close()
        browser = p.chromium.launch(headless=headless, args=launch_args)
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
                log.error("p0 graph screenshot: %s", e)
                return [], ""
            return _snap_with_blank_viewport_fallback(page)
        finally:
            browser.close()
