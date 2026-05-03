"""
Optional: when a P0 session starts, capture a dashboard URL (e.g. **Grafana**) with Playwright and post
the PNG to a Lark group.

A text line with the **capture date/time** (when ``page.screenshot`` completed) is posted before the
image — see ``P0_GRAPH_SCREENSHOT_CAPTION``, ``{captured_at}``, and ``P0_GRAPH_SCREENSHOT_TIMEZONE``.

**Clean “panels-only” grabs (default):** ``P0_GRAPH_SCREENSHOT_KIOSK=1`` appends Grafana **kiosk** mode to
the URL (hides left navigation). ``P0_GRAPH_SCREENSHOT_CLIP_SELECTOR`` (or the built-in fallback chain)
picks the **dashboard body** so the PNG is not one huge browser window — only the scrollable chart area.

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


def _capture_and_post(token: str, url: str, chat_id: str, source_label: str) -> None:
    from . import config as _config
    from . import lark_client as _lark

    pngs, captured_at = _capture_png_payloads()
    if not pngs:
        log.warning("p0 graph screenshot: capture returned empty")
        return
    cap = _config.get_p0_graph_screenshot_caption()
    if cap:
        text = cap.replace("{captured_at}", captured_at)
        text = text.replace("{label}", (source_label or "").strip())
    else:
        text = f"As of: {captured_at}"
    st_t, _ = _lark.post_text_to_chat(chat_id, token, text)
    if st_t != 200:
        log.warning("p0 graph screenshot: caption post HTTP=%s", st_t)

    for idx, png in enumerate(pngs):
        fname = "p0-dashboard.png" if len(pngs) == 1 else f"p0-dashboard-part{idx + 1}.png"
        key = _lark.upload_image_bytes_for_im_message(token, png, fname)
        if not key:
            log.warning("p0 graph screenshot: Lark image upload failed part=%s (check im:resource scope)", idx + 1)
            continue
        st, body = _lark.post_image_to_chat(chat_id, token, key)
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
                chat_id[-12:],
            )


def _capture_png_payloads() -> Tuple[List[bytes], str]:
    """
    Returns a non-empty list of PNG byte blobs and a formatted capture timestamp.
    With ``P0_GRAPH_SCREENSHOT_SPLIT_VERTICAL_HALVES=1``, returns two blobs (upper / lower half).
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
        Grid can exist while every panel is still an empty dark box — wait for canvas/SVG or text.
        """
        tmax = _config.get_p0_graph_screenshot_panel_content_ready_timeout_ms()
        if tmax <= 0:
            return
        # Runs in browser; panels may use canvas (Time series) or SVG (Stat, bar gauge); tables have text.
        js = r"""
            () => {
              const root = document.querySelector('main') || document.body;
              if (!root) return false;
              const panels = root.querySelectorAll(
                '[data-panel-id], .react-grid-item, [data-viz-key], '
                + '[data-testid*="panel"], [data-testid*="Panel"]'
              );
              if (panels.length < 1) return false;
              let canv = 0;
              root.querySelectorAll('canvas').forEach((c) => {
                const r = c.getBoundingClientRect();
                if (r.width > 4 && r.height > 4) canv++;
              });
              let bigSvg = 0;
              root.querySelectorAll('svg').forEach((s) => {
                const r = s.getBoundingClientRect();
                if (r.width > 20 && r.height > 12) bigSvg++;
              });
              if (canv >= 1) return true;
              if (bigSvg >= 4) return true;
              const t = (root.innerText || '').trim();
              if (t.length > 400 && /error|no data|query|failed|exception/i.test(t)) return true;
              if (panels.length >= 2 && t.length > 1200) return true;
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
                      if (!r) return { panels: 0, canv: 0, textLen: 0 };
                      return {
                        panels: r.querySelectorAll('[data-panel-id], .react-grid-item').length,
                        canv: r.querySelectorAll('canvas').length,
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

    launch_args = _config.get_p0_graph_screenshot_chromium_args()
    snap_full = split_halves or full_page or bool(clip_selectors)
    with sync_playwright() as p:
        if user_data:
            log.info("p0 graph screenshot: using persistent profile (Grafana session) at %s", user_data)
            context = p.chromium.launch_persistent_context(
                user_data,
                headless=True,
                viewport={"width": w, "height": h},
                args=launch_args,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                log.info(
                    "p0 graph screenshot: goto wait_until=%s full_page=%s viewport=%sx%s wait_after_ms=%s",
                    goto_wait,
                    snap_full,
                    w,
                    h,
                    wait_ms,
                )
                _goto_and_wait(page)
                return _snap(page)
            finally:
                context.close()
        browser = p.chromium.launch(headless=True, args=launch_args)
        try:
            page = browser.new_page(viewport={"width": w, "height": h})
            log.info(
                "p0 graph screenshot: goto wait_until=%s full_page=%s viewport=%sx%s wait_after_ms=%s",
                goto_wait,
                snap_full,
                w,
                h,
                wait_ms,
            )
            _goto_and_wait(page)
            return _snap(page)
        finally:
            browser.close()
