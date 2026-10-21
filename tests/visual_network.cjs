"use strict";
const path = require("path"), fs = require("fs"), os = require("os"), readline = require("readline");
const {spawn} = require("child_process");
const {chromium} = require(require.resolve("playwright", {paths: [path.resolve(path.dirname(process.execPath), "..")]}));
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const project = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "excatalog-network-ui-"));
let server, browser;
(async () => {
  server = spawn("py", ["-3.14", "-X", "utf8", "-u", path.join(__dirname, "visual_network_server.py"), fixture], {windowsHide: true});
  let diagnostics = "";
  server.stderr.on("data", chunk => { diagnostics += String(chunk); });
  const config = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Fixture did not start: " + diagnostics)), 15000);
    readline.createInterface({input: server.stdout}).on("line", line => {
      if (line.startsWith('{"base"')) { clearTimeout(timer); resolve(JSON.parse(line)); }
    });
    server.once("exit", code => { clearTimeout(timer); reject(new Error("Fixture exited " + code + diagnostics)); });
  });
  browser = await chromium.launch({headless: true, channel: "chrome"});
  const page = await browser.newPage({viewport: {width: 1440, height: 1080}}), errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(config.base + "/maintenance");
  await page.locator("#cloud-sync-section").waitFor({state: "visible"});
  await page.locator(".sidebar .nav-network").click();
  await page.waitForFunction(() => !document.getElementById("network-fields").disabled);
  assert(await page.locator("#network-host").isDisabled(), "Disabled proxy should disable custom fields");
  await page.locator("#network-enabled").check();
  await page.locator("#network-host").fill("127.0.0.1");
  await page.locator("#network-port").fill(String(config.proxy_port));
  await page.locator("#network-username").fill("test-proxy-user");
  await page.locator("#network-password").fill("test-proxy-password");
  await page.locator("#network-test").click();
  await page.waitForFunction(() => document.getElementById("network-result").dataset.state === "success");
  assert((await page.locator("#network-result").textContent()).includes("HTTP 200"), "Proxy response status missing");
  assert((await page.locator("#network-result").textContent()).includes("自定义代理"), "Proxy route missing");
  assert(!fs.existsSync(path.join(fixture, "data", "network-settings.ehproxy")), "Test should not save settings");
  await page.locator("#network-save").click();
  await page.waitForFunction(() => document.getElementById("network-saved-note").textContent.startsWith("已保存"));
  assert(await page.locator("#network-password").inputValue() === "", "Password was echoed after save");
  await page.reload();
  await page.waitForFunction(() => !document.getElementById("network-fields").disabled);
  assert(await page.locator("#network-enabled").isChecked(), "Enabled choice was not restored");
  assert(await page.locator("#network-port").inputValue() === String(config.proxy_port), "Port was not restored");
  assert(await page.locator("#network-password").inputValue() === "", "Saved password was exposed");
  assert((await page.locator("#network-password").getAttribute("placeholder")).includes("已保存"), "Saved password state missing");
  await page.locator("#network-section").screenshot({path: path.join(project, "logs/network-settings-wide.png")});
  await page.locator("#network-domains").fill("example.invalid");
  await page.locator('.sidebar a[href="/maintenance"]').click();
  await page.goBack();
  assert(await page.locator("#network-domains").inputValue() === "example.invalid", "Switching tools lost unsaved input");
  await page.locator("#network-clear-password").check();
  await page.locator("#network-save").click();
  await page.waitForFunction(() => !document.getElementById("network-fields").disabled);
  assert(!(await page.locator("#network-password").getAttribute("placeholder")).includes("已保存"), "Clear password was not applied");
  await page.locator("#network-port").fill("65536");
  assert(!await page.locator("#network-form").evaluate(form => form.checkValidity()), "Invalid port passed form validation");
  await page.locator("#network-port").fill(String(config.proxy_port));
  await page.locator("#network-test").click();
  await page.waitForFunction(() => !document.getElementById("network-fields").disabled);
  await page.setViewportSize({width: 390, height: 844});
  await page.locator("#network-title").click();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Mobile horizontal overflow");
  await page.screenshot({path: path.join(project, "logs/network-settings-mobile.png"), fullPage: true});
  await page.locator("#network-enabled").uncheck();
  await page.locator("#network-fallback").selectOption("direct");
  await page.locator("#network-save").click();
  await page.waitForFunction(() => document.getElementById("network-saved-note").textContent.includes("直接连接"));
  await page.locator("#network-target").selectOption("cover");
  await page.waitForFunction(() => document.getElementById("network-cover-hint").textContent.includes("没有封面地址"));
  assert(await page.locator("#network-test").isDisabled(), "Missing image target must not run a homepage test");
  assert(await page.locator("#network-cover-url").isHidden(), "Missing image target exposes a stale URL");
  for (const route of ["/", "/records", "/changelog", "/contact"]) {
    await page.goto(config.base + route);
    assert(await page.locator("a.nav-network").isVisible(), "Network navigation missing on " + route);
  }
  assert(errors.length === 0, errors.join("\n"));
  console.log("Network UI passed: test without saving, encrypted persistence, password masking/clearing, validation, history, mobile layout and navigation.");
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) { server.kill(); await new Promise(resolve => server.once("exit", resolve)); }
  const expectedParent = fs.realpathSync(os.tmpdir());
  const actual = fs.realpathSync(fixture);
  if (path.dirname(actual).toLowerCase() !== expectedParent.toLowerCase() || !path.basename(actual).startsWith("excatalog-network-ui-")) throw new Error("Unexpected fixture cleanup path");
  fs.rmSync(actual, {recursive: true, force: true});
});
