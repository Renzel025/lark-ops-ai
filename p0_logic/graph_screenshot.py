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
``P0_GRAPH_SCREENSHOT_FULL_DOCUMENT=0``. That captures the **viewport at scroll top**, then **scrolls the
main dashboard down by ~one viewport** and captures again (two different “pages” of panels — not a
50/50 pixel crop of one frame). If the page does not scroll, falls back to Pillow split of a single
viewport. ``P0_GRAPH_SCREENSHOT_FULL_DOCUMENT=1`` is **full document** height (``full_page=True``) — often
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
    """Append ``kiosk`` / ``k kiosk=tv`` when missing — Grafana hides side menu & yields denser panel view."""
    u = (url or "").strip()
    if not u or not enable:
        return u
    low = u.lower()
    if "kiosk" in low:
        return u
    parsed = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != "kiosk"]
    q.append(("kiosk", "tv"))
    new_query = urlencode(q)
    return urlunparse(parsed._replace(query=new_query))


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


def _compute_viewport_pair_scroll_delta(
    page, scroll_sel: Optional[str], viewport_h: int
) -> int:
    if scroll_sel:
        m = _scrollbar_virtualized_metrics(page, scroll_sel)
        if m:
            sh, ch = m
            max_scroll = max(0, sh - ch)
            if max_scroll <= 0:
                return 0
            delta = min(max_scroll, max(int(ch * 0.92), 1))
            return delta
    try:
        raw = page.evaluate(
            """() => {
              const se = document.scrollingElement || document.documentElement;
              const sh = Math.max(se ? se.scrollHeight : 0, document.body ? document.body.scrollHeight : 0);
              const ch = window.innerHeight || se.clientHeight || 720;
              return { sh: Math.ceil(sh), ch: Math.ceil(ch) };
            }"""
        )
        sh = int(raw["sh"])
        ch = int(raw["ch"])
        max_scroll = max(0, sh - ch)
        if max_scroll <= 0:
            return 0
        vh = max(int(viewport_h or ch), 240)
        return min(max_scroll, max(int(vh * 0.92), 1))
    except Exception:
        return max(1, int(max(viewport_h, 720) * 0.92))


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


def _viewport_scroll_pair_screenshots(
    page,
    viewport_h: int,
) -> List[bytes]:
    """
    Two viewport-sized PNGs: first at scroll top, second after scrolling the main dashboard down by
    ~one viewport (shows below-the-fold panels). Returns [] if the page does not scroll enough.
    """
    scroll_sel: Optional[str] = None
    try:
        _clear_p0_dash_page_scroll_marks(page)
        if _mark_wide_dashboard_scroll_container(page):
            scroll_sel = "[data-p0-dash-page-scroll='1']"
        _scroll_pair_reset_top(page, scroll_sel)
        page.wait_for_timeout(420)
        p1 = page.screenshot(full_page=False, type="png")
        delta = _compute_viewport_pair_scroll_delta(page, scroll_sel, viewport_h)
        if delta <= 0:
            log.info(
                "p0 graph screenshot: viewport scroll pair skipped — no vertical overflow (delta=0)"
            )
            return []
        log.info(
            "p0 graph screenshot: viewport scroll pair delta=%s scroll_sel=%s",
            delta,
            scroll_sel or "window",
        )
        _apply_viewport_pair_scroll(page, scroll_sel, delta)
        page.wait_for_timeout(680)
        p2 = page.screenshot(full_page=False, type="png")
        if p1 and p2:
            return [p1, p2]
        return []
    except Exception as e:
        log.warning("p0 graph screenshot: viewport scroll pair failed: %s", e)
        return []
    finally:
        _scroll_pair_reset_top(page, scroll_sel)
        _clear_p0_dash_page_scroll_marks(page)


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


def _capture_png_payloads() -> Tuple[List[bytes], str]:
    """
    Returns a non-empty list of PNG byte blobs and a formatted capture timestamp.
    With ``P0_GRAPH_SCREENSHOT_SPLIT_VERTICAL_HALVES=1``, returns two blobs (viewport scroll pair or Pillow halves).
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
            if split_halves:
                scroll_parts = _viewport_scroll_pair_screenshots(page, h)
                if len(scroll_parts) == 2:
                    return scroll_parts, cap_time
                log.info(
                    "p0 graph screenshot: viewport scroll pair missing or no overflow — "
                    "fallback Pillow split on single viewport (install pillow for two PNGs)"
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
            pair = _viewport_scroll_pair_screenshots(page, h)
            if len(pair) == 2:
                pngs2 = pair
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

    def _wait_for_grafana_panels_if_configured(page) -> None:
        panel_timeout = _config.get_p0_graph_screenshot_panel_ready_timeout_ms()
        if panel_timeout <= 0:
            return
        # Grafana 8–11: grid items; some builds use data-panel-id on the panel wrapper.
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
        """
        Grid can exist while time-series cells are still empty (black). Wait until a **large** chart
        canvas exists, enough SVG widgets, an error string, or a very full **table** (table-only boards).
        """
        tmax = _config.get_p0_graph_screenshot_panel_content_ready_timeout_ms()
        if tmax <= 0:
            return
        # Runs in browser; panels may use canvas (Time series) or SVG (Stat, bar gauge); tables have text.
        # Do **not** treat ``panels >= 2 && long text`` alone as ready — dashboards like Core Metrics render
        # the left table first while the main time-series grid is still empty (black); that caused early screenshots.
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

    def _goto_and_wait(page) -> None:
        page.goto(url, wait_until=goto_wait, timeout=nav_ms)
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        _wait_for_grafana_panels_if_configured(page)
        _wait_for_grafana_chart_content_if_configured(page)
        # Nudge lazy panels / below-the-fold queries (harmless if no scroll).
        try:
            page.evaluate("window.scrollBy(0, 600); window.scrollTo(0, 0)")
        except Exception:
            pass
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

    launch_args = list(_config.get_p0_graph_screenshot_chromium_args())
    if _config.get_p0_graph_screenshot_swiftshader():
        extra = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        launch_args.extend([a for a in extra if a not in launch_args])
        log.info("p0 graph screenshot: SwiftShader (ANGLE) flags enabled for headless GL")
    headless = _config.get_p0_graph_screenshot_playwright_headless()
    snap_full = split_halves or full_page or bool(clip_selectors)
    dsf = _config.get_p0_graph_screenshot_device_scale_factor()
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
                _goto_and_wait(page)
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
            _goto_and_wait(page)
            return _snap_with_blank_viewport_fallback(page)
        finally:
            browser.close()
