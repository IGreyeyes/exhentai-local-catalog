const { chromium } = require('playwright');
const path = require('path');

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function layout(page, viewport, suffix) {
  await page.setViewportSize(viewport);
  await page.goto('http://127.0.0.1:8765/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('#gallery-count')?.textContent !== '—');
  const metrics = await page.evaluate(() => {
    const box = selector => {
      const value = document.querySelector(selector).getBoundingClientRect();
      return {left:value.left,right:value.right,width:value.width,top:value.top,bottom:value.bottom};
    };
    return {
      viewport: {width:innerWidth,height:innerHeight},
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      panelOverflow: document.querySelector('.search-panel').scrollWidth - document.querySelector('.search-panel').clientWidth,
      include: box('#tag-field'), exclude: box('#exclude-tag-field'), blacklist: box('.blacklist-panel'),
      credentialImport: Boolean(document.querySelector('#credential-import')),
      credentialExport: Boolean(document.querySelector('#credential-export')),
    };
  });
  assert(metrics.overflow <= 1 && metrics.panelOverflow <= 1, `Search layout overflow: ${JSON.stringify(metrics)}`);
  assert(metrics.include.width > 200 && metrics.exclude.width > 200 && metrics.blacklist.width > 200, `Search controls too narrow: ${JSON.stringify(metrics)}`);
  assert(metrics.credentialImport && metrics.credentialExport, 'Credential file controls are missing');
  await page.screenshot({path:path.resolve(`logs/search-${suffix}.png`),fullPage:true});
  return metrics;
}

(async () => {
  const browser = await chromium.launch({headless:true,executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); });
    page.on('pageerror', error => errors.push(error.message));
    const desktop = await layout(page, {width:1440,height:1000}, 'desktop');
    const mobile = await layout(page, {width:390,height:844}, 'mobile');
    await page.locator('#collector-panel > summary').click();
    await page.locator('.credential-file-actions').scrollIntoViewIfNeeded();
    const credentialLayout = await page.locator('.credential-file-actions').evaluate(element => {
      const parent = element.getBoundingClientRect();
      const buttons = [...element.querySelectorAll('button')].map(button => {
        const value = button.getBoundingClientRect();
        return {left:value.left,right:value.right,top:value.top,bottom:value.bottom,width:value.width,height:value.height};
      });
      return {parent:{left:parent.left,right:parent.right,width:parent.width},buttons,overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};
    });
    assert(credentialLayout.overflow <= 1 && credentialLayout.buttons.every(button => button.left >= credentialLayout.parent.left && button.right <= credentialLayout.parent.right && button.height >= 36), `Credential controls overlap: ${JSON.stringify(credentialLayout)}`);
    await page.screenshot({path:path.resolve('logs/collector-mobile.png'),fullPage:false});

    await page.setViewportSize({width:1440,height:1000});
    await page.goto('http://127.0.0.1:8765/', {waitUntil:'domcontentloaded'});
    await page.locator('#tag-input').fill('other:anthology');
    await page.locator('#tag-input').press('Enter');
    await page.locator('#exclude-tag-input').fill('language:chinese');
    await page.locator('#exclude-tag-input').press('Enter');
    await page.locator('#search-button').click();
    await page.waitForFunction(() => document.querySelector('#results')?.getAttribute('aria-busy') === 'false');
    assert(new URL(page.url()).searchParams.get('exclude_tag') === 'language:chinese', 'Excluded tag was not preserved in the URL');
    const forbidden = await page.locator('.result-card').evaluateAll(cards => cards.filter(card => [...card.querySelectorAll('.result-tags button')].some(button => button.title.includes('language:chinese'))).length);
    assert(forbidden === 0, 'A result containing the excluded tag was rendered');

    await page.goto('http://127.0.0.1:8765/records', {waitUntil:'domcontentloaded'});
    await page.locator('.saved-card').first().waitFor({state:'visible'});
    assert(await page.locator('#record-sort').inputValue() === 'favorites_desc', 'Records page did not default to favorite count descending');
    const counts = await page.locator('.saved-count strong').evaluateAll(nodes => nodes.map(node => Number(node.textContent.replaceAll(',', ''))));
    assert(counts.every((value,index) => index === 0 || counts[index-1] >= value), `Record counts are not descending: ${counts.slice(0,10)}`);
    assert(errors.length === 0, `Browser errors: ${errors.join(' | ')}`);
    console.log(JSON.stringify({desktop,mobile,credentialLayout,checkedResults:true,recordCountSample:counts.slice(0,5)}));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
