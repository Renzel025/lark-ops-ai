---
name: diagnose-grafana
description: Diagnose and fix the P0 Grafana screenshot pipeline — blank image, left sidebar showing, bottom panels cut off, single image instead of two bands, or a black void. Use when the user says the Grafana screenshot is broken/wrong.
---

# Diagnose the Grafana screenshot (lark-ops-ai)

Hard-won facts — check these in order. Service runs `python3.8`; unit is `lark-ops-ai`.

## 1. Which capture path is active
- `P0_GRAPH_SCREENSHOT_USE_RENDER_API=1` → server-side **Render API** (grafana-image-renderer). **Preferred** — reliable, no browser.
- `=0` → **Playwright/Chromium** fallback — flaky: blank canvas panels, slow, and the left nav/sidebar leaks in on Grafana 11.

## 2. Symptom → cause → fix

- **Left sidebar shows in the render** → the kiosk param. Grafana 11 **removed `kiosk=tv`**; the code now appends a bare **`&kiosk`**. Verify the box has that commit (`git log`). Browser test: opening `.../d/<uid>/...?kiosk` should hide the nav; `?kiosk=tv` won't on GF11.

- **Single image instead of two bands, OR black void at the bottom** → **Pillow isn't importable by the SERVICE's Python.** `pip install pillow` in your shell usually lands in pyenv 3.9, NOT the `python3.8` the service runs. Fix by installing into the service's interpreter:
  ```bash
  EXE=$(systemctl show lark-ops-ai -p ExecStart --value | grep -oE '(/[^ ]*)?python[0-9.]*' | head -1)
  echo "service python = $EXE"
  $EXE -m pip install pillow
  systemctl restart lark-ops-ai
  ```
  Confirm: the log line `render: Pillow not installed — posting single render PNG` disappears and `render: autocrop …px -> …px` appears (split + auto-crop now run).

- **Bottom (Pulsar) panels cut off** → the render height. `P0_GRAPH_SCREENSHOT_RENDER_HEIGHT` is a tall **canvas** (default 4000) + auto-crop, not an exact size. If still cut, either the dashboard is taller than the canvas, or grafana-image-renderer is **capping** height on the Grafana server (`GF_RENDERING_VIEWPORT_MAX_HEIGHT` / `[rendering] viewport_max_height`). Test the ACTUAL returned pixel size:
  ```bash
  curl -s -u <user>:<pass> -o /tmp/t.png '<host>/render/d/<uid>/<slug>?orgId=1&from=now-6h&to=now&width=1920&height=8000&kiosk'
  python3 -c "from PIL import Image; print(Image.open('/tmp/t.png').size)"
  ```
  - `(1920, ~3000)` despite asking 8000 → renderer is capping height → fix on the **Grafana server**.
  - `(1920, 8000)` → renderer honors it → deploy the auto-crop code / raise `RENDER_HEIGHT`.

- **Image too big / wrong size** → lower `P0_GRAPH_SCREENSHOT_VIEWPORT_WIDTH` (keep **≥1200** or Grafana reflows the columns). `VIEWPORT_HEIGHT` is **Playwright-only** and ignored by the Render API — changing it does nothing on the render path.

## 3. Key log lines
```bash
journalctl -u lark-ops-ai --no-pager | grep -iE "graph screenshot render|autocrop|part\(s\)|falling back|bad response" | tail
```
- `render: OK size=… → 2 image part(s)` = working.
- `render: bad response HTTP=401` = wrong Grafana creds.
- `render: bad response … type=text/html` = `grafana-image-renderer` plugin not installed on Grafana.
- `render: … falling back to Playwright` = the Render API failed (read the reason on that line).

## 4. Test end-to-end
```bash
python3 features/screenshot/scripts/grafana_screenshot_run_once.py --post-lark
```
Expect a clean, panels-only, two-band capture posted to `P0_GRAPH_SCREENSHOT_TARGET_CHAT_ID`.
