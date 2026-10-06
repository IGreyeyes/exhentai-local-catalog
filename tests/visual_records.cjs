const { chromium } = require('playwright');
const path = require('path');

async function inspect(page, viewport, output) {
  await page.setViewportSize(viewport);
  await page.goto(process.env.RECORDS_URL || 'http://127.0.0.1:8765/records', { waitUntil: 'networkidle' });
  await page.locator('.saved-card').first().waitFor({ state: 'visible' });
  await page.waitForFunction(() => { const image = document.querySelector('.saved-card .saved-cover img'); return image && image.complete && image.naturalWidth > 0; });
  const metrics = await page.evaluate(() => {
    const card = document.querySelector('.saved-card');
    const cover = card.querySelector('.saved-cover');
    const content = card.querySelector('.saved-content');
    const count = card.querySelector('.saved-count');
    const box = element => {
      const value = element.getBoundingClientRect();
      return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width, height: value.height };
    };
    return {
      viewport: { width: innerWidth, height: innerHeight },
      bodyOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cardOverflow: card.scrollWidth - card.clientWidth,
      cover: box(cover), content: box(content), count: box(count),
      previewTags: card.querySelectorAll('.saved-tag-preview button').length,
      tagGroups: card.querySelectorAll('.saved-tag-preview .saved-tag-group').length,
      detailsPresent: Boolean(card.querySelector('.saved-tag-details')),
      imageLoaded: Boolean(cover.querySelector('img').naturalWidth),
    };
  });
  if (metrics.bodyOverflow > 1 || metrics.cardOverflow > 1) throw new Error(`Horizontal overflow: ${JSON.stringify(metrics)}`);
  if (metrics.previewTags < 1 || metrics.detailsPresent || !metrics.imageLoaded) throw new Error(`Tag/image state invalid: ${JSON.stringify(metrics)}`);
  if (viewport.width >= 1000) {
    if (metrics.cover.width < 140) throw new Error(`Desktop cover too small: ${metrics.cover.width}`);
    if (metrics.cover.right > metrics.content.left || metrics.content.right > metrics.count.left) throw new Error(`Desktop columns overlap: ${JSON.stringify(metrics)}`);
  } else {
    if (metrics.cover.width < 90) throw new Error(`Mobile cover too small: ${metrics.cover.width}`);
    if (metrics.cover.right > metrics.content.left) throw new Error(`Mobile cover/content overlap: ${JSON.stringify(metrics)}`);
  }
  await page.locator('.saved-card').first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: output, fullPage: false });
  return metrics;
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe' });
  try {
    const page = await browser.newPage();
    const prefix = process.env.SCREENSHOT_PREFIX || 'records';
    const desktop = await inspect(page, { width: 1440, height: 1000 }, path.resolve(`logs/${prefix}-desktop.png`));
    const mobile = await inspect(page, { width: 390, height: 844 }, path.resolve(`logs/${prefix}-mobile.png`));
    console.log(JSON.stringify({ desktop, mobile }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
