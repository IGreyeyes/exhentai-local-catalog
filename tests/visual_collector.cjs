const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const readline = require('readline');

function assert(condition, message) { if (!condition) throw new Error(message); }
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'collector-visual-'));
let server, browser;
async function startServer() {
  server = spawn('py', ['-3.14', '-u', path.join(__dirname, 'visual_collector_server.py'), fixture], { windowsHide: true });
  let errors = '';
  server.stderr.on('data', value => errors += value.toString());
  return new Promise((resolve, reject) => {
    const lines = readline.createInterface({ input: server.stdout });
    const timer = setTimeout(() => reject(new Error('Fixture startup timed out: ' + errors)), 20000);
    lines.on('line', line => { if (line.startsWith('http://')) { clearTimeout(timer); resolve(line); } });
    server.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited (${code}): ${errors}`)); });
  });
}
async function stopServer() {
  if (server && server.exitCode === null) {
    const stopped = new Promise(resolve => server.once('exit', resolve));
    server.kill();
    await stopped;
  }
}
async function waitRound(page, current, completed) {
  await page.waitForFunction(({ current, completed }) => {
    const rounds = document.querySelector('#round-plan-rounds').textContent;
    const status = document.querySelector('#collector-job-state').textContent;
    return rounds === `${completed} / 10` && status.includes(`第 ${current} 轮已结束`);
  }, { current, completed }, { timeout: 60000 });
  await page.waitForFunction(() => !document.querySelector('#collector-next-round').disabled);
}
async function waitForProgressViewport(page) {
  await page.waitForFunction(() => {
    const card = document.querySelector('#collector-job');
    const box = card.getBoundingClientRect();
    return !card.hidden && box.top >= 0 && box.top <= 40;
  });
}
async function screenshotAndCheck(page, viewport, name) {
  await page.setViewportSize(viewport);
  await page.locator('#collector-job').scrollIntoViewIfNeeded();
  const metrics = await page.locator('#collector-job').evaluate(element => {
    const button = element.querySelector('#collector-next-round').getBoundingClientRect();
    const box = element.getBoundingClientRect();
    return {
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cardOverflow: element.scrollWidth - element.clientWidth,
      nextButtonFits: button.left >= box.left && button.right <= box.right,
      savedNotice: element.querySelector('#collector-save-note').textContent,
      progress: element.querySelector('#collector-total-progress').value,
    };
  });
  assert(metrics.overflow <= 1 && metrics.cardOverflow <= 1 && metrics.nextButtonFits, 'Collector overflow: ' + JSON.stringify(metrics));
  await page.screenshot({ path: path.resolve(`logs/collector-rounds-${name}.png`), fullPage: false });
  return metrics;
}

(async () => {
  fs.mkdirSync('logs', { recursive: true });
  let root = await startServer();
  browser = await chromium.launch({ headless: true, executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  // Avoid external cover requests; the entire scenario uses generated local metadata.
  await page.route('**/api/cover/*', route => route.fulfill({ status: 200, contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120"/>' }));
  await page.goto(root, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('#gallery-count').textContent !== '—');
  await page.locator('#tag-input').fill('language:english');
  await page.locator('#tag-input').press('Enter');
  await page.locator('#search-button').click();
  await page.waitForFunction(() => document.querySelector('#results').getAttribute('aria-busy') === 'false' && document.querySelector('.result-card'));
  await page.locator('#collector-panel > summary').click();
  assert(await page.locator('#collector-mode').inputValue() === 'rounds', 'New plans should default to rounds');
  await page.locator('#collector-host').selectOption('e-hentai.org');
  await page.locator('#collector-skip-existing').check();
  await page.locator('#collector-batch-size').selectOption('custom');
  await page.locator('#collector-custom-batch-size').fill('37');
  await page.locator('#collector-save').click();
  await page.waitForFunction(() => document.querySelector('#collector-config-state').textContent.includes('设置已应用'));
  await page.locator('#collector-start').click();
  await page.waitForFunction(() => document.querySelector('#collector-estimate').hidden === false);
  assert((await page.locator('#estimate-detail').textContent()).includes('271 轮'), 'Custom size did not reach backend');
  await page.locator('#collector-dismiss').click();
  await page.locator('#collector-batch-size').selectOption('1000');
  await page.locator('#collector-save').click();
  await page.waitForFunction(() => !document.querySelector('#collector-verify').disabled);
  await page.locator('#collector-verify').click();
  await page.waitForFunction(() => document.querySelector('#collector-feedback').textContent.includes('验证成功'));
  await page.locator('#collector-start').click();
  await page.waitForFunction(() => !document.querySelector('#collector-confirm').disabled);
  assert((await page.locator('#estimate-detail').textContent()).includes('分为 10 轮'), 'Full round plan not previewed');
  await page.locator('#collector-confirm').click();
  await waitForProgressViewport(page);
  await waitRound(page, 1, 1);
  assert(await page.locator('#round-plan-total').textContent() === '10,000', 'Wrong frozen queue size');
  assert(await page.locator('#round-plan-remaining').textContent() === '9,000', 'Round 1 did not stop at 1,000');
  await page.locator('#collector-view-progress').scrollIntoViewIfNeeded();
  await page.locator('#collector-view-progress').click();
  await waitForProgressViewport(page);
  await page.locator('#collector-next-round').click();
  await page.waitForFunction(() => !document.querySelector('#collector-confirm').disabled);
  await page.locator('#collector-confirm').click();
  await waitForProgressViewport(page);
  await waitRound(page, 2, 2);
  assert(await page.locator('#round-plan-remaining').textContent() === '8,000', 'Round 2 did not stop');
  const desktop = await screenshotAndCheck(page, { width: 1440, height: 1100 }, 'desktop');
  const mobile = await screenshotAndCheck(page, { width: 390, height: 844 }, 'mobile');
  // Restart the actual fixture process against the same SQLite database.
  await stopServer();
  root = await startServer();
  await page.goto(root, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('#round-plan-remaining').textContent === '8,000');
  assert(await page.locator('#collector-panel').getAttribute('open') !== null, 'Saved plan should open automatically');
  assert(await page.locator('#collector-start').isDisabled(), 'No search was submitted after restart');
  assert(await page.locator('#collector-host').inputValue() === 'e-hentai.org', 'Original host not restored');
  await page.locator('#collector-save').click();
  await page.waitForFunction(() => !document.querySelector('#collector-verify').disabled);
  await page.locator('#collector-verify').click();
  await page.waitForFunction(() => document.querySelector('#collector-feedback').textContent.includes('验证成功'));
  await page.locator('#collector-next-round').click();
  await page.waitForFunction(() => !document.querySelector('#collector-confirm').disabled);
  await page.locator('#collector-confirm').click();
  await waitRound(page, 3, 3);
  assert(await page.locator('#round-plan-remaining').textContent() === '7,000', 'Restart did not continue round 3');
  assert(errors.length === 0, 'Browser errors: ' + errors.join(' | '));
  console.log(JSON.stringify({ desktop, mobile, customSize: true, automaticStops: true, restartWithoutSearch: true, progressFocus: true, progressShortcut: true, browserErrors: errors }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  await stopServer();
  fs.rmSync(fixture, { recursive: true, force: true });
});
