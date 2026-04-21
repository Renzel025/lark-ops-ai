"""
Optional: when a P0 session starts, capture a dashboard URL (e.g. **Grafana**) with Playwright and post
the PNG to a Lark group.

Without ``P0_GRAPH_SCREENSHOT_PLAYWRIGHT_USER_DATA_DIR``, each run uses a **fresh** Chromium — fine for
anonymous/public dashboards only. For logged-in Grafana, point that env at a **persistent profile
directory** where you completed login once (headed), similar to Slack ``SESSION_DIR``.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("lark-ops-ai")


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


def _capture_and_post(token: str, url: str, chat_id: str, source_label: str) -> None:
    from . import config as _config
    from . import lark_client as _lark

    png = _capture_png_bytes()
    if not png:
        log.warning("p0 graph screenshot: capture returned empty")
        return
    key = _lark.upload_image_bytes_for_im_message(token, png, "p0-dashboard.png")
    if not key:
        log.warning("p0 graph screenshot: Lark image upload failed (check im:resource scope)")
        return
    cap = _config.get_p0_graph_screenshot_caption()
    if cap:
        text = cap
        if source_label and "{label}" in text:
            text = text.replace("{label}", source_label)
        st_t, _ = _lark.post_text_to_chat(chat_id, token, text)
        if st_t != 200:
            log.warning("p0 graph screenshot: caption post HTTP=%s", st_t)
    st, body = _lark.post_image_to_chat(chat_id, token, key)
    if st != 200:
        log.warning(
            "p0 graph screenshot: image message HTTP=%s body=%s",
            st,
            (body or "")[:400],
        )
    else:
        log.info("p0 graph screenshot: posted image to chat_id tail=%s", chat_id[-12:])


def _capture_png_bytes() -> Optional[bytes]:
    from . import config as _config

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("p0 graph screenshot: playwright not installed (pip install playwright; playwright install chromium)")
        return None

    url = _config.get_p0_graph_screenshot_url()
    w = _config.get_p0_graph_screenshot_viewport_width()
    h = _config.get_p0_graph_screenshot_viewport_height()
    wait_ms = _config.get_p0_graph_screenshot_wait_ms()
    nav_ms = _config.get_p0_graph_screenshot_nav_timeout_ms()
    full_page = _config.get_p0_graph_screenshot_full_page()
    user_data = _config.get_p0_graph_screenshot_playwright_user_data_dir()

    launch_args = _config.get_p0_graph_screenshot_chromium_args()
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
                page.goto(url, wait_until="load", timeout=nav_ms)
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
                return page.screenshot(full_page=full_page, type="png")
            finally:
                context.close()
        browser = p.chromium.launch(headless=True, args=launch_args)
        try:
            page = browser.new_page(viewport={"width": w, "height": h})
            page.goto(url, wait_until="load", timeout=nav_ms)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            return page.screenshot(full_page=full_page, type="png")
        finally:
            browser.close()
