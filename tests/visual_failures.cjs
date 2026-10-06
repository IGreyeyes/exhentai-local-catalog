const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const readline = require('readline');
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const fixture = fs.mkdtempSync(path.join(os.tmpdir(),'failure-visual-'));
let server,browser;

async function inspect(page,viewport,name) {
  await page.setViewportSize(viewport);
  await page.locator('.saved-card').first().scrollIntoViewIfNeeded();
  const metrics = await page.evaluate(() => ({
    overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
    cardOverflow:Math.max(...[...document.querySelectorAll('.saved-card')].map(card=>card.scrollWidth-card.clientWidth)),
    failureCards:document.querySelectorAll('.saved-card.failed').length,
    unknownCounts:[...document.querySelectorAll('.saved-count strong')].filter(item=>item.textContent==='—').length,
  }));
  assert(metrics.overflow<=1&&metrics.cardOverflow<=1,'Failure layout overflow: '+JSON.stringify(metrics));
  await page.screenshot({path:path.resolve(`logs/failure-records-${name}.png`),fullPage:false});
  return metrics;
}

(async()=>{
  fs.mkdirSync('logs',{recursive:true});
  server=spawn('py',['-3.14','-u',path.join(__dirname,'visual_collector_server.py'),fixture,'failures'],{windowsHide:true});
  const root=await new Promise((resolve,reject)=>{
    const lines=readline.createInterface({input:server.stdout}),timer=setTimeout(()=>reject(new Error('Fixture timeout')),20000);
    lines.on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code));});
  });
  browser=await chromium.launch({headless:true,executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await context.route('https://e-hentai.org/**',route=>route.fulfill({status:200,contentType:'text/html',body:'<p>Mock source page</p>'}));
  await page.route('**/api/cover/*',route=>route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120"/>'}));
  await page.goto(root+'/records?collection=failed');
  await page.waitForFunction(()=>document.querySelector('#records-list').getAttribute('aria-busy')==='false'&&document.querySelector('.saved-card.failed'));
  assert(await page.locator('.saved-card.failed').count()===7,'Failed-only records are missing');
  assert(await page.locator('#saved-failed').textContent()==='7','Wrong failure summary');
  assert(await page.locator('#record-sort').inputValue()==='recorded_desc','Failures should default to most recent');
  assert(await page.locator('.saved-failure-url').first().getAttribute('href')==='https://e-hentai.org/g/20000/abcdef0123/','Missing-metadata failure lost its original URL');
  assert(await page.locator('.saved-failure-info img').count()===0,'Error text was rendered as HTML');
  const desktop=await inspect(page,{width:1440,height:1100},'desktop');
  const mobile=await inspect(page,{width:390,height:844},'mobile');
  const cacheCard=page.locator('.saved-card').filter({has:page.locator('.saved-id',{hasText:'ID 3'})});
  assert(await cacheCard.locator('.saved-count strong').textContent()==='288','Failed refresh discarded the last good count');
  assert((await cacheCard.textContent()).includes('刷新失败'),'Refresh failure not identified');
  const popup=page.waitForEvent('popup');
  await page.locator('.saved-failure-url').first().click();
  const sourcePage=await popup;await sourcePage.close();
  await page.waitForFunction(()=>[...document.querySelectorAll('.saved-card')].some(card=>card.textContent.includes('ID 20000')&&card.textContent.includes('已点开 1 次')));
  await page.locator('select[aria-label="作品 ID 20000 的阅读状态"]').selectOption('planned');
  await page.waitForFunction(()=>[...document.querySelectorAll('.saved-card')].some(card=>card.textContent.includes('ID 20000')&&card.querySelector('.reading-planned')));
  await page.locator('[data-collection="success"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.saved-card').length===2&&document.querySelector('#records-list').getAttribute('aria-busy')==='false');
  assert(await page.locator('.saved-card.failed').count()===0,'Success filter includes failures');
  await page.locator('[data-collection="all"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.saved-card').length===9);
  await page.goto(root+'/records?collection=failed&job_id=1');
  await page.waitForFunction(()=>!document.querySelector('#record-job-filter').hidden);
  await page.locator('#clear-record-job').click();
  await page.waitForFunction(()=>!new URL(location.href).searchParams.has('job_id'));
  await page.goto(root+'/#collector-panel');
  await page.waitForFunction(()=>!document.querySelector('#collector-job-errors').hidden);
  assert(await page.locator('#collector-job-errors').getAttribute('open')===null,'Failure details should start collapsed');
  await page.locator('#collector-errors-summary').click();
  assert(await page.locator('#collector-error-list li').count()===5,'Recent failure list missing');
  assert(await page.locator('#collector-error-list a').count()===5,'Recent failure URLs missing');
  await page.evaluate(()=>window.collectorUI.refresh());
  assert(await page.locator('#collector-job-errors').getAttribute('open')!==null,'Polling collapsed the expanded failures');
  await page.locator('#collector-job-errors').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.resolve('logs/collector-failure-links.png'),fullPage:false});
  await page.locator('#collector-errors-more').click();
  await page.waitForFunction(()=>document.querySelectorAll('.saved-card.failed').length===7);
  assert(new URL(page.url()).searchParams.get('job_id')==='1','All-failures link did not preserve the original task');
  assert(errors.length===0,'Browser errors: '+errors.join(' | '));
  console.log(JSON.stringify({desktop,mobile,failedRefresh:true,sourceLinkTracking:true,readingState:true,collapsedFailures:true,taskLink:true,browserErrors:errors}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(server&&server.exitCode===null){const stopped=new Promise(resolve=>server.once('exit',resolve));server.kill();await stopped;}
  fs.rmSync(fixture,{recursive:true,force:true});
});
