// End-to-end scheduled-break regression using only an isolated local fixture.
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const readline = require('readline');
const { chromium } = require(require.resolve('playwright', { paths: [path.resolve(path.dirname(process.execPath), '..')] }));
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'collector-break-'));
let server, browser;

(async () => {
  fs.mkdirSync('logs', { recursive: true });
  const python = spawnSync('py', ['-3.14', '-c', 'import sys; print(sys.executable)'], { windowsHide: true, encoding: 'utf8', timeout: 15000 });
  assert(python.status === 0 && python.stdout.trim(), 'Python 3.14 is required');
  server = spawn(python.stdout.trim(), ['-u', path.join(__dirname, 'visual_collector_server.py'), fixture, 'auto-pause'], { windowsHide: true });
  let errors = '';
  server.stderr.on('data', data => { errors += data.toString(); });
  const root = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Fixture timed out: '+errors)), 20000);
    readline.createInterface({ input: server.stdout }).on('line', line => {
      if (line.startsWith('http://')) { clearTimeout(timer); resolve(line); }
    });
    server.once('exit', code => { clearTimeout(timer); reject(new Error('Fixture stopped: '+code)); });
  });
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(root+'/#collector-panel');
  await page.waitForFunction(() => document.querySelector('#gallery-count').textContent === '32');
  assert(await page.locator('#collector-work-minutes').isDisabled(), 'Disabled default must disable work time');
  await page.locator('#collector-host').selectOption('e-hentai.org');
  await page.locator('#collector-mode').selectOption('rating_full');
  await page.locator('#collector-auto-pause').check();
  await page.locator('#collector-work-minutes').fill('0.1');
  await page.locator('#collector-rest-minutes').fill('0.1');
  await page.locator('#collector-save').click();
  await page.waitForFunction(() => document.querySelector('#collector-auto-pause-feedback').textContent.includes('设置已保存'));
  const saved = await page.locator('#collector-auto-pause-feedback').textContent();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.auto-pause-settings').scrollIntoViewIfNeeded();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile settings overflow');
  await page.screenshot({ path: path.resolve('logs/auto-pause-settings-mobile.png') });
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#collector-auto-pause').checked);
  assert(await page.locator('#collector-work-minutes').inputValue() === '0.1', 'Work time did not restore');
  await page.locator('#collector-save').click();
  await page.waitForFunction(() => document.querySelector('#collector-auto-pause-feedback').textContent.includes('设置已保存'));
  await page.locator('#collector-verify').click();
  await page.waitForFunction(() => document.querySelector('#collector-feedback').textContent.includes('随机验证成功'));
  await page.locator('#collector-start').click();
  await page.waitForFunction(() => !document.querySelector('#collector-estimate').hidden);
  assert((await page.locator('#estimate-detail').textContent()).includes('预计耗时已计入'), 'Rest estimate missing');
  await page.locator('#collector-confirm').click();
  await page.waitForFunction(() => document.querySelector('#collector-schedule-status').textContent.includes('定时休息中'), null, { timeout: 25000 });
  assert(await page.locator('#collector-auto-pause-feedback').textContent() === saved, 'Polling replaced saved settings feedback');
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.locator('#collector-job').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.resolve('logs/auto-pause-resting-desktop.png') });
  const waitingCount = await page.locator('#collector-job-counts').textContent();
  await page.waitForFunction(() => document.querySelector('#collector-schedule-status').textContent.includes('已完成 1 次休息'), null, { timeout: 15000 });
  await page.waitForFunction(previous => document.querySelector('#collector-job-counts').textContent !== previous, waitingCount);
  await page.locator('#collector-pause').click();
  await page.waitForFunction(() => document.querySelector('#collector-job-state').textContent === '已暂停');
  assert(await page.locator('#collector-schedule-status').isHidden(), 'Manual pause must cancel countdown');
  await page.locator('#collector-resume').click();
  await page.waitForFunction(() => !document.querySelector('#collector-estimate').hidden);
  await page.locator('#collector-confirm').click();
  await page.waitForFunction(() => document.querySelector('#collector-job-state').textContent === '采集完成', null, { timeout: 20000 });
  assert(await page.locator('#collector-schedule-status').isHidden(), 'Completed queue must not wait for another break');
  assert(pageErrors.length === 0, 'Browser errors: '+pageErrors.join('; '));
  console.log('Scheduled-break UI passed: defaults, save/reload, mobile layout, sticky feedback, rest countdown, automatic continuation, pause and completion.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    const stopped = new Promise(resolve => server.once('exit', resolve));
    server.kill();
    await stopped;
  }
  const resolvedFixture = path.resolve(fixture);
  if (path.dirname(resolvedFixture) !== path.resolve(os.tmpdir()) || !path.basename(resolvedFixture).startsWith('collector-break-')) {
    throw new Error('Unexpected temporary fixture directory');
  }
  fs.rmSync(resolvedFixture, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
});
