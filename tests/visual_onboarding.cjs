"use strict";
// Real local APIs, disposable data, DPAPI test credentials, and a simulated source.
const path = require("path"), fs = require("fs"), os = require("os"), readline = require("readline");
const {spawn} = require("child_process");
const {chromium} = require(require.resolve("playwright", {paths: [path.resolve(path.dirname(process.execPath), "..")]}));
const assert = (value, message) => { if (!value) throw new Error(message); };
const project = path.resolve(__dirname, ".."), output = path.join(project, "logs");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "excatalog-onboarding-"));
let server, browser, base;

async function startServer() {
  server = spawn("py", ["-3.14", "-X", "utf8", "-u", path.join(__dirname, "visual_collector_server.py"), fixture], {windowsHide: true});
  base = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Fixture did not start")), 15000);
    readline.createInterface({input: server.stdout}).on("line", line => { if (line.startsWith("http://")) {clearTimeout(timer); resolve(line);} });
    server.once("exit", code => {clearTimeout(timer); reject(new Error("Fixture exited: " + code));});
  });
}
async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const exited = new Promise(resolve => server.once("exit", resolve));
  server.kill(); await exited;
}
async function main() {
  fs.mkdirSync(output, {recursive: true});
  await startServer();
  browser = await chromium.launch({headless: true, channel: "chrome"});
  const page = await browser.newPage({viewport: {width: 1360, height: 960}, reducedMotion: "reduce"});
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    return url.origin === base && !url.pathname.startsWith("/api/cover/") ? route.continue() : route.abort();
  });
  async function openCollector() {
    await page.goto(base + "/?tag=language%3Aenglish#collector-panel");
    await page.locator("#results .saved-card").first().waitFor();
    await page.waitForFunction(() => document.querySelector("#collector-proxy-status").textContent.includes("恢复") || document.querySelector("#credential-state").textContent.includes("尚未载入"));
  }
  await openCollector();
  await page.locator("#collector-proxy").fill("http://127.0.0.1:7890");
  await page.waitForFunction(() => document.querySelector("#collector-proxy-status").textContent.startsWith("采集代理已保存"));
  await stopServer(); await startServer(); await openCollector();
  await page.waitForFunction(() => document.querySelector("#collector-proxy").value === "http://127.0.0.1:7890");
  assert((await page.locator("#credential-state").textContent()).includes("尚未载入"), "Proxy persistence should not persist login");
  await page.locator("#cookie-member").fill("12345");
  await page.locator("#cookie-hash").fill("test-hash-value");
  await page.locator("#cookie-igneous").fill("test-igneous-value");
  await page.locator("#collector-settings button[type=submit]").click();
  await page.waitForFunction(() => document.querySelector("#credential-state").textContent.includes("登录信息已载入"));
  const encoded = await page.evaluate(async () => {
    const {action_token} = await (await fetch("/api/status")).json();
    const response = await fetch("/api/credentials/export", {method: "POST", headers: {"Content-Type": "application/json", "X-Catalog-Token": action_token}, body: "{}"});
    if (!response.ok) throw new Error("Fixture credential export failed");
    return btoa(String.fromCharCode(...new Uint8Array(await response.arrayBuffer())));
  });
  const login = path.join(fixture, "test-login.ehcred"); fs.writeFileSync(login, Buffer.from(encoded, "base64"));
  await stopServer(); await startServer(); await openCollector();
  await page.route("**/api/credentials/import", async route => {await new Promise(resolve => setTimeout(resolve, 300)); await route.continue();});
  await page.locator("#credential-file").setInputFiles(login);
  await page.waitForFunction(() => document.querySelector("#credential-import").textContent === "正在导入…");
  await page.waitForFunction(() => document.querySelector("#credential-feedback").textContent.startsWith("导入成功"));
  assert((await page.locator("#credential-state").textContent()).includes("待验证"), "Imported login was not visibly marked pending verification");
  for (const id of ["cookie-member", "cookie-hash", "cookie-igneous"]) {
    assert(await page.locator("#" + id).inputValue() === "", "Credential leaked into input");
    assert((await page.locator("#" + id).getAttribute("placeholder")).includes("已载入"), "Empty credential field is ambiguous");
  }
  assert(!await page.locator("#credential-export").isDisabled(), "Import did not refresh export action");
  await page.locator("#collector-access-settings").screenshot({path: path.join(output, "onboarding-login-feedback.png")});
  await page.locator("#credential-file").setInputFiles({name: "bad.ehcred", mimeType: "application/octet-stream", buffer: Buffer.from("invalid test file")});
  await page.waitForFunction(() => document.querySelector("#credential-feedback").classList.contains("error"));
  assert((await page.locator("#credential-state").textContent()).includes("已载入"), "Failed import cleared previous login");
  await page.locator("#credential-file").setInputFiles(login);
  await page.waitForFunction(() => document.querySelector("#credential-feedback").textContent.startsWith("导入成功"));
  await page.locator("#credential-file").dispatchEvent("cancel");
  assert((await page.locator("#credential-feedback").textContent()).includes("已取消"), "Cancelled import lacks feedback");
  assert(await page.locator("#collector-start").textContent() === "开始采集", "Start entry is unclear");
  await page.locator("#collector-start").click();
  await page.locator("#collector-estimate").waitFor({state: "visible"});
  assert(await page.locator("#collector-confirm").isDisabled(), "Unverified import allowed collection");
  assert(!(await page.evaluate(async () => (await (await fetch("/api/collector")).json()).job)), "Start skipped confirmation");
  await page.locator("#collector-dismiss").click();
  await page.locator("#collector-verify").click();
  await page.waitForFunction(() => document.querySelector("#collector-config-state").textContent.includes("已验证"));
  await page.locator("#collector-start").click();
  await page.waitForFunction(() => !document.querySelector("#collector-estimate").hidden && !document.querySelector("#collector-confirm").disabled);
  await page.locator("#collector-estimate").screenshot({path: path.join(output, "onboarding-start-confirmation.png")});
  await page.locator("#collector-dismiss").click();
  await page.locator("#collector-proxy").fill("http://127.0.0.1:7891");
  await page.waitForFunction(() => document.querySelector("#collector-proxy-status").textContent.startsWith("采集代理已保存"));
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#collector-proxy").value === "http://127.0.0.1:7891");
  assert((await page.locator("#collector-config-state").textContent()).includes("请先应用"), "Restored draft incorrectly claims to be applied");
  await page.locator("#collector-proxy").fill("");
  await page.waitForFunction(() => document.querySelector("#collector-proxy-status").textContent.startsWith("已保存清空"));
  await stopServer(); await startServer(); await openCollector();
  await page.waitForFunction(() => document.querySelector("#collector-config-state").textContent === "尚未配置");
  assert(await page.locator("#collector-proxy").inputValue() === "", "Cleared proxy returned after restart");

  await page.goto(base + "/maintenance#network-section");
  await page.waitForFunction(() => !document.querySelector("#network-fields").disabled);
  assert(await page.locator(".maintenance-tabs a[href='#network-section']").count() === 0, "Duplicate proxy tool remains");
  assert(await page.locator(".sidebar .nav-item.selected").count() === 1, "Multiple selected navigation items");
  assert(await page.locator(".nav-network").getAttribute("aria-current") === "page", "Proxy navigation not highlighted");
  assert(await page.locator(".intro h1").textContent() === "网络代理", "Wrong proxy page heading");
  await page.locator("#network-enabled").check();
  await page.locator("#network-host").fill("proxy.example.invalid");
  await page.locator('.sidebar a[href="/maintenance"]').click();
  assert(await page.locator("#cloud-sync-section").isVisible(), "Cannot return to maintenance");
  await page.goBack();
  assert(await page.locator("#network-section").isVisible(), "Back did not restore proxy");
  assert(await page.locator("#network-host").inputValue() === "proxy.example.invalid", "Navigation discarded proxy draft");
  await page.screenshot({path: path.join(output, "onboarding-network-navigation.png")});
  for (const width of [1100, 760, 390]) {
    await page.setViewportSize({width, height: 960});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Proxy layout overflow at " + width);
  }
  await page.screenshot({path: path.join(output, "onboarding-network-small.png")});
  assert(errors.length === 0, "Browser errors: " + errors.join("; "));
  console.log(JSON.stringify({passed: true, proxyRestartAndClear: true, loginImportFeedback: true, failedImportPreservesLogin: true, verificationAndConfirmation: true, networkNavigationAndHistory: true, responsive: true}));
}
main().catch(error => {console.error(error); process.exitCode = 1;}).finally(async () => {
  if (browser) await browser.close(); await stopServer();
  const target = path.resolve(fixture);
  if (path.dirname(target) !== path.resolve(os.tmpdir()) || !path.basename(target).startsWith("excatalog-onboarding-")) throw new Error("Unexpected fixture cleanup path");
  fs.rmSync(target, {recursive: true, force: true, maxRetries: 5, retryDelay: 150});
});
