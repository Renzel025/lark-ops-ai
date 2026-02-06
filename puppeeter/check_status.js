const puppeteer = require("puppeteer");
const path = require("path");
const fs = require("fs");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getTimestamp() {
  const now = new Date();
  return now
    .toISOString()
    .replace(/T/, "_")
    .replace(/:/g, "-")
    .replace(/\..+/, "");
}

(async () => {
  const sessionDir = path.resolve(__dirname, "./tg_session");
  const screenshotDir = path.resolve(__dirname, "./screenshots");

  if (!fs.existsSync(screenshotDir)) {
    fs.mkdirSync(screenshotDir, { recursive: true });
  }

  const browser = await puppeteer.launch({
    headless: false,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1920,1080"],
    userDataDir: sessionDir,
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080 });

  try {
    console.log("🔍 Checking Telegram login status...");

    await page.goto("https://web.telegram.org/a/", { waitUntil: "networkidle2" });
    await sleep(5000); // works everywhere

    const isLoggedIn = await page.evaluate(() => {
      return !!document.querySelector(".chat-list") || !!document.querySelector("#main-columns");
    });

    const status = isLoggedIn ? "logged_in" : "not_logged_in";
    const timestamp = getTimestamp();

    const screenshotPath = path.join(screenshotDir, `telegram_${status}_${timestamp}.png`);
    await page.screenshot({ path: screenshotPath });

    console.log(`✅ STATUS: ${isLoggedIn ? "Already logged in." : "Not logged in yet (QR or phone input detected)."}`);
    console.log(`📸 Screenshot saved: ${screenshotPath}`);
  } catch (err) {
    console.error("🔥 Error:", err.message);
  } finally {
    await browser.close();
  }
})();
