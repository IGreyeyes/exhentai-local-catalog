"use strict";
const path = require("path"), fs = require("fs"), os = require("os"), readline = require("readline");
const {spawn} = require("child_process");
const {chromium} = require(require.resolve("playwright", {paths: [path.resolve(path.dirname(process.execPath), "..")]}));
const assert = (value, message) => { if (!value) throw new Error(message); };
const project = path.resolve(__dirname, ".."), output = path.join(project, "logs");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "excatalog-covers-ui-"));
let server, browser, page;
(async () => {
  fs.mkdirSync(output, {recursive: true});
  server = spawn(path.join(project, ".venv-desktop", "Scripts", "python.exe"), ["-X", "utf8", "-u", path.join(__dirname, "visual_covers_server.py"), fixture], {windowsHide: true});
  let diagnostics = "";
  server.stderr.on("data", chunk => { diagnostics += String(chunk); });
  const {base} = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Fixture startup: " + diagnostics)), 15000);
    readline.createInterface({input: server.stdout}).on("line", line => {
      if (line.startsWith('{"base"')) { clearTimeout(timer); resolve(JSON.parse(line)); }
    });
    server.once("exit", code => { clearTimeout(timer); reject(new Error("Fixture exit: " + code + diagnostics)); });
  });
  browser = await chromium.launch({headless: true, channel: "chrome"});
  page = await browser.newPage({viewport: {width: 1440, height: 1100}});
  const errors = [], coverResponses = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("response", response => { if (response.url().includes("/api/cover/")) coverResponses.push(response); });
  // A fixture must never send browser requests to a real source site.
  await page.route("**/*", route => route.request().url().startsWith(base + "/") ? route.continue() : route.abort());
  await page.goto(base + "/?tag=language%3Aenglish&sort=oldest");
  await page.locator(".saved-card").last().scrollIntoViewIfNeeded();
  await page.waitForFunction(() => [...document.querySelectorAll(".saved-cover img")].length === 3 && [...document.querySelectorAll(".saved-cover img")].every(image => image.complete && image.naturalWidth));
  const before = await (await page.request.get(base + "/test/cover-attempts")).json();
  assert(Object.values(before).every(count => count === 1), "Initial downloads were repeated");
  const originalURL = page.url();
  await page.locator("#cover-retry").click();
  await page.waitForFunction(() => document.querySelector('img[data-cover-path^="/api/cover/1?"]').naturalWidth === 120);
  await page.waitForFunction(() => document.querySelector("#cover-retry-feedback").textContent.includes("1 张封面仍需等待"));
  assert(page.url() === originalURL, "Retry changed the search or page");
  assert(await page.locator("#cover-retry").isDisabled(), "Retry button permits rapid repeat clicks");
  const after = await (await page.request.get(base + "/test/cover-attempts")).json();
  assert(after["test-1.png"] === 2 && after["test-2.png"] === 1 && after["test-3.png"] === 1, "Retry bypassed rate limiting or downloaded a cached image");
  for (const response of coverResponses) {
    if ((await response.headerValue("content-type")).includes("svg")) assert(await response.headerValue("cache-control") === "no-store", "Placeholder is cached");
  }
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 1100});
    await page.locator("#cover-retry").scrollIntoViewIfNeeded();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Search overflow at " + width);
    await page.screenshot({path: path.join(output, `covers-search-${width}.png`)});
  }
  await page.goto(base + "/records");
  await page.locator(".saved-card").first().waitFor();
  assert(await page.locator("#cover-retry").isVisible(), "Records retry is missing");
  await page.waitForTimeout(5100);
  await page.locator("#cover-retry").click();
  await page.waitForFunction(() => document.querySelector("#cover-retry-feedback").textContent.includes("冷却"));
  await page.route("**/api/covers/retry", route => route.fulfill({status: 400, contentType: "application/json", body: JSON.stringify({error: "请间隔至少 5 秒再重试封面。"})}));
  await page.waitForFunction(() => !document.querySelector("#cover-retry").disabled);
  await page.locator("#cover-retry").click();
  await page.waitForFunction(() => document.querySelector("#cover-retry-feedback").classList.contains("error"));
  assert((await page.locator("#cover-retry-feedback").textContent()).includes("5 秒"), "Failure feedback is missing");
  await page.unroute("**/api/covers/retry");
  await page.goto(base + "/maintenance#covers-section");
  await page.waitForFunction(() => document.querySelector("#cover-cache-count").textContent === "2");
  await page.locator("#cover-retry").click();
  await page.waitForFunction(() => document.querySelector("#cover-retry-feedback").textContent.includes("冷却"));
  await page.setViewportSize({width: 1440, height: 1000});
  await page.screenshot({path: path.join(output, "covers-maintenance-1440.png")});
  await page.setViewportSize({width: 390, height: 1000});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Maintenance overflows");
  await page.screenshot({path: path.join(output, "covers-maintenance-390.png")});
  assert(!errors.length, "Browser errors: " + errors.join("; "));
  console.log(JSON.stringify({passed: true, recovery: true, cachedImagesRetained: true, rateLimitRetained: true, searchPreserved: true, errorFeedback: true, responsiveWidths: [1440, 390], browserErrors: errors}));
})().catch(async error => {
  if (page) {
    console.error(await page.evaluate(() => ({feedback: document.getElementById("cover-retry-feedback")?.textContent, images: [...document.querySelectorAll(".saved-cover img")].map(image => ({source: image.getAttribute("src"), width: image.naturalWidth, complete: image.complete}))})));
    await page.screenshot({path: path.join(output, "covers-ui-failure.png")});
  }
  console.error(error); process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) { server.kill(); await new Promise(resolve => server.once("exit", resolve)); }
  const resolved = path.resolve(fixture), temporary = path.resolve(os.tmpdir()) + path.sep;
  if (!resolved.startsWith(temporary) || !path.basename(resolved).startsWith("excatalog-covers-ui-")) throw new Error("Unexpected fixture directory");
  fs.rmSync(resolved, {recursive: true, force: true});
});
