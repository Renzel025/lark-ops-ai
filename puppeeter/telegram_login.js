const puppeteer = require("puppeteer");
const path = require("path");
const fs = require("fs");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
    // 1. Linisin muna ang mga lumang screenshots bago magsimula
    const oldFiles = fs.readdirSync(__dirname).filter(f => f.endsWith('.png'));
    oldFiles.forEach(f => fs.unlinkSync(path.join(__dirname, f)));
    console.log("🗑️ Old screenshots cleared.");

    const sessionDir = path.resolve(__dirname, "./tg_session");
    const browser = await puppeteer.launch({
        headless: false,
        args: ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,1280"],
        userDataDir: sessionDir,
    });

    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 1280 });

    try {
        console.log("🚀 Loading Telegram Web A...");
        await page.goto("https://web.telegram.org/a/", { waitUntil: "networkidle2" });

        while (true) {
            console.log("⏳ Waiting for QR code to render (Bypassing loading screen)...");
            
            // 2. Hintayin ang mismong QR canvas o ang main login container
            try {
                await page.waitForSelector('canvas, .qr-container', { timeout: 45000 });
                await sleep(5000); // Extra cushion para siguradong loaded ang image
            } catch (e) {
                console.log("⚠️ Loading took too long, refreshing page...");
                await page.reload({ waitUntil: "networkidle2" });
                continue;
            }

            // 3. Take screenshot kapag tapos na ang loading
            await page.screenshot({ path: "SCAN_ME.png" });
            console.log("📸 NEW QR SAVED: SCAN_ME.png");
            console.log("👉 ACTION: I-download na via SCP at i-scan!");

            // 4. Check for login status
            let loggedIn = false;
            for (let i = 0; i < 12; i++) { // 1 minute wait
                await sleep(5000);
                loggedIn = await page.evaluate(() => {
                    return !!document.querySelector('.chat-list') || !!document.querySelector('#main-columns');
                });

                if (loggedIn) break;
                process.stdout.write(".");
            }

            if (loggedIn) {
                console.log("\n✅ SUCCESS: Logged in!");
                await page.screenshot({ path: "LOGGED_IN.png" });
                break;
            } else {
                console.log("\n🔄 Refreshing for a fresh QR...");
                await page.reload({ waitUntil: "networkidle2" });
            }
        }

    } catch (err) {
        console.error("🔥 Error:", err.message);
        await page.screenshot({ path: "ERROR_SCAN.png" });
    } finally {
        await browser.close();
    }
})();
