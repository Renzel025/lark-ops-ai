const puppeteer = require("puppeteer");
const path = require("path");
const fs = require("fs");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const DEBUG = process.env.DEBUG === "1"; // set DEBUG=1 to enable extra screenshots/dumps
const TG_GROUP_URL = process.env.TG_GROUP_URL || "https://web.telegram.org/k/#-3891715605";

async function clickCenter(page, elHandle) {
  const box = await elHandle.boundingBox();
  if (!box) {
    console.error("Bounding box not found for the element.");
    return false;
  }
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.click(x, y);
  return true;
}

async function snap(page, name) {
  if (!DEBUG) return;
  const file = `${name}.png`;
  // fullPage screenshots are slow on small EC2; keep it viewport-only
  await page.screenshot({ path: file });
  console.log(`📸 Saved ${file}`);
}

async function dumpHeaderButtons(page) {
  const data = await page.evaluate(() => {
    const header =
      document.querySelector("header") ||
      document.querySelector(".topbar") ||
      document.querySelector("[class*='topbar']") ||
      document.body;

    const btns = Array.from(header.querySelectorAll("button, div[role='button'], a"));
    return btns.slice(0, 120).map((el, idx) => {
      const r = el.getBoundingClientRect();
      return {
        idx,
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || "").trim().slice(0, 80),
        aria: (el.getAttribute("aria-label") || "").trim().slice(0, 80),
        title: (el.getAttribute("title") || "").trim().slice(0, 80),
        cls: (el.className || "").toString().slice(0, 160),
        x: Math.round(r.x),
        y: Math.round(r.y),
        w: Math.round(r.width),
        h: Math.round(r.height),
      };
    });
  });

  fs.writeFileSync("header_buttons_dump.json", JSON.stringify(data, null, 2));
  console.log("🧾 Wrote header_buttons_dump.json");
}

async function findVideoCallButton(page) {
  // 1) Try by aria-label/title keywords (best when available)
  const keywordSelectors = [
    'button[aria-label*="video" i]',
    'button[title*="video" i]',
    'div[role="button"][aria-label*="video" i]',
    'div[role="button"][title*="video" i]',
    'button[aria-label*="video chat" i]',
    'button[title*="video chat" i]',
  ];

  for (const sel of keywordSelectors) {
    const el = await page.$(sel);
    if (el) return el;
  }

  // 2) Fallback: pick icon button by POSITION near top-right header
  const handle = await page.evaluateHandle(() => {
    const header =
      document.querySelector("header") ||
      document.querySelector(".topbar") ||
      document.querySelector("[class*='topbar']") ||
      document.body;

    const candidates = Array.from(header.querySelectorAll("button, div[role='button']"));

    const filtered = candidates
      .map((el) => ({ el, r: el.getBoundingClientRect() }))
      .filter(({ r }) => r.width >= 24 && r.height >= 24 && r.y >= 0 && r.y < 160);

    if (!filtered.length) return null;

    // Rightmost first
    filtered.sort((a, b) => b.r.x - a.r.x);

    // Typical header right side order: menu (⋮), search, video
    // So video is often index 2 from the right
    const pick = filtered[2]?.el || filtered[1]?.el || filtered[0]?.el;
    return pick || null;
  });

  const el = handle.asElement();
  return el || null;
}

async function clickStartJoinIfPopup(page) {
  // After clicking video icon, a menu/modal might appear
  await sleep(300);

  const clicked = await page.evaluate(() => {
    const wanted = ["start", "join", "video chat", "call", "continue", "ok"];
    const nodes = Array.from(
      document.querySelectorAll("button, [role='menuitem'], [role='button'], .MenuItem, .btn-primary")
    );

    const pick = nodes.find((n) => {
      const t =
        ((n.innerText || "") + " " + (n.getAttribute("aria-label") || "") + " " + (n.getAttribute("title") || ""))
          .trim()
          .toLowerCase();
      return wanted.some((w) => t.includes(w));
    });

    if (!pick) return { ok: false };

    pick.style.outline = "3px solid lime";
    pick.click();
    return {
      ok: true,
      text: (pick.innerText || pick.getAttribute("aria-label") || "popup").trim(),
    };
  });

  return clicked;
}

(async () => {
  const sessionDir = path.resolve(__dirname, "./tg_session");

  const browser = await puppeteer.launch({
    headless: false,
    userDataDir: sessionDir,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--window-size=1280,720", // smaller = faster + less RAM
      "--disable-gpu",
      "--mute-audio",
      "--disable-background-networking",
      "--disable-background-timer-throttling",
      "--disable-renderer-backgrounding",
      "--disable-features=TranslateUI",
      "--disable-sync",
    ],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 720 });

  page.setDefaultTimeout(60000);
  page.setDefaultNavigationTimeout(0);

  try {
    console.log("🚀 Opening Telegram Web group directly...");
    await page.goto(TG_GROUP_URL, { waitUntil: "networkidle2" });

    console.log("⏳ Waiting for left column (logged in UI)...");
    await page.waitForSelector("div#column-left", { timeout: 60000 });

    console.log("⏳ Waiting for chat header...");
    await page.waitForSelector("header, .topbar, [class*='topbar']", { timeout: 60000 });

    // small settle for icons; much smaller than 3s
    await sleep(400);
    await snap(page, "before_click_video");

    console.log("🔎 Finding video call icon...");
    const videoBtn = await findVideoCallButton(page);

    if (!videoBtn) {
      console.error("❌ Video call button not found.");
      if (DEBUG) await dumpHeaderButtons(page);
      await snap(page, "cant_find_video_icon");
      throw new Error("Video call icon not found.");
    }

    // highlight when debugging
    if (DEBUG) {
      await page.evaluate((el) => {
        el.style.outline = "3px solid red";
        el.style.background = "rgba(255,0,0,0.12)";
      }, videoBtn);
      await snap(page, "highlight_video_icon");
    }

    console.log("🖱️ Clicking video call icon...");
    const clicked = await clickCenter(page, videoBtn);
    if (!clicked) throw new Error("Could not click video icon (no bounding box).");

    console.log("✅ Clicked video icon!");
    await sleep(250);
    await snap(page, "after_click_video_icon");

    console.log("🔍 Trying to click Start/Join popup if present...");
    const popup = await clickStartJoinIfPopup(page);

    if (popup.ok) {
      console.log(`✅ Popup clicked: ${popup.text}`);
      await sleep(400);
      await snap(page, "after_popup");
    } else {
      console.log("ℹ️ No Start/Join popup detected (maybe it started directly).");
    }
  } catch (e) {
    console.error("🔥 Error:", e.message);
    await page.screenshot({ path: "error_final.png" }).catch(() => {});
    console.log("📸 Saved error_final.png");

    // Dump buttons on error (helps lock selector if UI changes)
    try {
      await dumpHeaderButtons(page);
    } catch {}
  } finally {
    await sleep(500);
    await browser.close();
    console.log("Browser closed.");
  }
})();
