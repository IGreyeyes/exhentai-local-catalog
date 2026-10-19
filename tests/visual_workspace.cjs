"use strict";
// Exercise the reorganized UI against disposable data and a simulated source.
const path = require("path"), fs = require("fs"), readline = require("readline");
const {spawn} = require("child_process");
const {chromium} = require(require.resolve("playwright", {paths: [path.resolve(path.dirname(process.execPath), "..")]}));
const assert = (value, message) => { if (!value) throw new Error(message); };
const project = path.resolve(__dirname, ".."), output = path.join(project, "logs");
fs.mkdirSync(output, {recursive: true});
const fixture = fs.mkdtempSync(path.join(output, "excatalog-workspace-"));
let server, browser;

async function main() {
  server = spawn("py", ["-3.14", "-X", "utf8", "-u", path.join(__dirname, "visual_filters_server.py"), fixture], {windowsHide: true});
  let diagnostics = "";
  server.stderr.on("data", chunk => { diagnostics += String(chunk); });
  const base = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Fixture timeout: " + diagnostics)), 15000);
    readline.createInterface({input: server.stdout}).on("line", line => {
      if (line.startsWith("http://")) { clearTimeout(timer); resolve(line); }
    });
    server.once("exit", code => { clearTimeout(timer); reject(new Error("Fixture exited " + code)); });
  });
  browser = await chromium.launch({headless: true, channel: "chrome"});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}, reducedMotion: "reduce"});
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    return url.origin === base && !url.pathname.startsWith("/api/cover/") ? route.continue() : route.abort();
  });
  await page.goto(base + "/?tag=language%3Aenglish");
  await page.locator("#results .saved-card").first().waitFor();
  assert(!await page.locator("#search-advanced").evaluate(node => node.open), "Advanced filters must start collapsed");
  assert(await page.locator("#results-heading").evaluate(node => node.getBoundingClientRect().top) < 730, "Search results buried below settings");
  await page.screenshot({path: path.join(output, "workspace-search-wide.png")});
  await page.locator("#search-advanced > summary").click();
  await page.locator("#exclude-tag-input").fill("other:anthology");
  await page.locator("#exclude-tag-input").press("Enter");
  await page.locator("#use-tag-blacklist").uncheck();
  await page.locator("#include-inactive").check();
  await page.locator("#search-advanced > summary").click();
  assert((await page.locator("#search-filter-summary").textContent()).includes("排除 1 个标签"), "Collapsed exclusion not disclosed");
  assert((await page.locator("#search-filter-summary").textContent()).includes("黑名单未启用"), "Disabled blacklist not disclosed");
  assert((await page.locator("#search-filter-summary").textContent()).includes("包含历史记录"), "Hidden inactive filter not disclosed");
  await page.locator("#reset-filters").click();
  assert(!(await page.locator("#search-filter-summary").textContent()).includes("包含历史记录"), "Reset left stale filter summary");
  await page.goto(base + "/?tag=language%3Aenglish&include_inactive=1&use_blacklist=0");
  await page.locator("#results .saved-card").first().waitFor();
  assert((await page.locator("#search-filter-summary").textContent()).includes("包含历史记录"), "URL restoration lost collapsed summary");
  await page.locator(".nav-collector").click();
  assert(await page.locator("#collector-panel").evaluate(node => node.open), "Collector navigation did not reveal workspace");
  assert(new URL(page.url()).searchParams.get("tag") === "language:english", "Collector navigation discarded search");
  await page.locator("#collector-panel").screenshot({path: path.join(output, "workspace-collector-wide.png")});

  await page.goto(base + "/records?state=watched&source=exhentai.org");
  await page.locator("#records-list .saved-card").first().waitFor();
  assert((await page.locator("#records-filter-summary").textContent()).includes("看过"), "Record state not visible while collapsed");
  assert((await page.locator("#records-filter-summary").textContent()).includes("ExHentai"), "Record source not disclosed");
  await page.locator("#clear-record-filters").click();
  await page.waitForFunction(() => document.querySelectorAll("#records-list .saved-card").length > 1);
  await page.screenshot({path: path.join(output, "workspace-records-wide.png")});

  await page.goto(base + "/maintenance#backup-section");
  await page.waitForFunction(() => !document.querySelector("#maintenance-backup").disabled);
  assert(await page.locator(".maintenance-section:visible").count() === 1, "Maintenance shows multiple tools");
  assert(await page.locator("#backup-section").isVisible(), "Backup deep link failed");
  await page.locator("#backup-directory").fill(path.join(fixture, "draft-backup"));
  await page.locator('.maintenance-tabs a[href="#catalog-section"]').click();
  assert(await page.locator("#catalog-section").isVisible(), "Catalog tab did not show");
  await page.goBack();
  assert(await page.locator("#backup-section").isVisible(), "Back did not restore tool");
  assert((await page.locator("#backup-directory").inputValue()).endsWith("draft-backup"), "Switching tool lost unsaved field");
  await page.locator("#backup-directory").fill(path.join(fixture, "backups"));
  await page.locator("#maintenance-backup").click();
  await page.locator("#backup-list .backup-row .button").first().waitFor();
  await page.waitForFunction(() => !document.querySelector("#maintenance-backup").disabled);
  await page.locator("#backup-list .backup-row .button").first().click();
  assert(await page.locator("#restore-section").isVisible(), "Import this backup did not switch tool");
  assert(new URL(page.url()).hash === "#restore-section", "Restore link not recorded in history");
  assert(await page.locator("#restore-directory").evaluate(node => node === document.activeElement), "Restore field not focused");
  await page.goto(base + "/maintenance");
  await page.waitForFunction(() => !document.querySelector("#cloud-sync-save").disabled);
  await page.screenshot({path: path.join(output, "workspace-maintenance-wide.png")});
  await page.locator('.maintenance-tabs a[href="#favorites-section"]').click();
  await page.locator(".merge-rules-help > summary").click();
  assert(await page.locator(".favorites-merge-rules table").isVisible(), "Merge rules unavailable");
  await page.goto(base + "/maintenance?upload-guide=1#backup-section");
  assert(await page.locator("#cloud-upload-guide").isVisible(), "Upload guide hidden by tool navigation");
  await page.goto(base + "/maintenance#stop");
  assert(await page.locator("#maintenance-stop-confirm").isVisible(), "Legacy stop link broken");
  await page.locator("#maintenance-stop-cancel").click();

  for (const width of [1440, 1100, 800, 760, 390]) {
    await page.setViewportSize({width, height: 1000});
    for (const route of ["/?tag=language%3Aenglish", "/records", "/maintenance", "/contact", "/changelog"]) {
      await page.goto(base + route);
      if (route.startsWith("/?")) await page.locator("#results .saved-card").first().waitFor();
      if (route === "/records") await page.locator("#records-list .saved-card").first().waitFor();
      assert(await page.locator(".sidebar .nav-item:visible").count() === 6, `Missing navigation at ${width}: ${route}`);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Page overflow at ${width}: ${route}`);
      if (route === "/maintenance") {
        for (const link of await page.locator(".maintenance-tabs a").all()) {
          await link.click();
          assert(await page.locator(".maintenance-section:visible").count() === 1, "Tool isolation failed");
          assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Tool overflow at ${width}: ${await link.getAttribute("href")}`);
        }
      }
      if (width === 390) await page.screenshot({path: path.join(output, `workspace-${route === "/" || route.startsWith("/?") ? "search" : route.slice(1)}-small.png`)});
    }
  }
  assert(errors.length === 0, "Browser errors: " + errors.join("; "));
  console.log(JSON.stringify({passed: true, collapsedFilterSummary: true, collectorLinkKeepsSearch: true, maintenanceHistory: true, draftsPreserved: true, restoreShortcut: true, legacyLinks: true, allToolsReachable: true, responsiveWidths: [1440, 1100, 800, 760, 390], scriptErrors: errors}));
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) { server.kill(); await new Promise(resolve => server.once("exit", resolve)); }
  const target = path.resolve(fixture);
  if (path.dirname(target) !== path.resolve(output) || !path.basename(target).startsWith("excatalog-workspace-")) throw new Error("Unexpected cleanup path");
  fs.rmSync(target, {recursive: true, force: true, maxRetries: 5, retryDelay: 150});
});
