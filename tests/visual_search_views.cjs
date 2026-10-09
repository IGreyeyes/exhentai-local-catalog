"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),output=path.join(project,'logs');
const fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-search-views-'));
const views=['minimal','compact','extended','thumbnails'];
let server,browser;

async function ready(page){
  await page.locator('.saved-card').first().waitFor();
  await page.waitForFunction(()=>document.querySelector('#results').getAttribute('aria-busy')==='false'&&!document.querySelector('#search-view').disabled);
}
async function inspect(page,view,width){
  await page.selectOption('#search-view',view);
  await page.waitForFunction(()=>!document.querySelector('#search-view').disabled);
  const metrics=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth-innerWidth,cardOverflow:[...document.querySelectorAll('.saved-card')].some(card=>card.scrollWidth>card.clientWidth+1),count:document.querySelectorAll('.saved-card').length}));
  assert(metrics.overflow<=1&&!metrics.cardOverflow,`Overflow in ${view} at ${width}: ${JSON.stringify(metrics)}`);
  assert(metrics.count===40,'View changed the page size');
  assert(await page.locator('.saved-cover').count()===(['extended','thumbnails'].includes(view)?40:0),'Unexpected covers');
  assert(await page.locator('.saved-tag-section button').count()===(['compact','extended'].includes(view)?130:0),'Tags missing or truncated');
  if(view==='extended'){
    assert(await page.locator('.saved-tag-namespace').first().textContent()==='语言','Matched namespace is not first');
    assert(await page.locator('.saved-tag-values button.matched').count()===40,'Matched tags not highlighted');
    assert((await page.locator('.saved-collector').first().textContent()).includes('我 · 测试采集者'),'Original collector not displayed');
  }
  await page.locator('.results-section').evaluate(section=>section.scrollIntoView({block:'start'}));
  await page.screenshot({path:path.join(output,`search-${view}-${width}.png`)});
}
async function main(){
  fs.mkdirSync(output,{recursive:true});
  server=spawn('py',['-3.14','-X','utf8','-u',path.join(__dirname,'visual_collector_server.py'),fixture,'search-views'],{windowsHide:true});
  const base=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>reject(new Error('Fixture exited: '+code)));
  });
  browser=await chromium.launch({headless:true,channel:'chrome'});
  let page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/cover/*',route=>route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="176" height="235"><rect width="176" height="235" fill="#edf1e8"/></svg>'}));
  const query='/?tag=language%3Aenglish&sort=favorites';
  await page.goto(base+query);await ready(page);
  assert(await page.inputValue('#search-view')==='extended','Default view is not extended');
  const originalURL=page.url();
  const originalIds=await page.locator('.saved-card').evaluateAll(cards=>cards.map(card=>card.querySelector('.saved-id').textContent));
  const counts=await page.locator('.saved-count strong').allTextContents();
  assert(counts[5]==='0'&&counts[6]==='—','Unknown favorites confused with zero');
  assert(new Set(await page.locator('.gallery-category').evaluateAll(nodes=>nodes.map(node=>getComputedStyle(node).backgroundColor))).size>=6,'Category colors missing');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:width===390?844:1000});
    for(const view of views){
      await inspect(page,view,width);
      assert(page.url()===originalURL,'View changed search filters or URL');
      assert(JSON.stringify(await page.locator('.saved-card').evaluateAll(cards=>cards.map(card=>card.querySelector('.saved-id').textContent)))===JSON.stringify(originalIds),'View changed sorting');
    }
  }
  await page.selectOption('#link-target','e-hentai.org');
  assert((await page.locator('.saved-title').first().getAttribute('href')).startsWith('https://e-hentai.org/'),'Source switch failed');
  await page.locator('#next').click();await ready(page);
  assert(await page.locator('.saved-card').count()===6&&await page.inputValue('#search-view')==='thumbnails','Pagination lost the view');
  await page.goto(base+'/records');await page.locator('.saved-card').first().waitFor();
  assert(await page.inputValue('#record-view')==='extended','Search view changed record view');
  await page.selectOption('#record-view','compact');await page.waitForFunction(()=>!document.querySelector('#record-view').disabled);
  await browser.close();browser=await chromium.launch({headless:true,channel:'chrome'});
  page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+query);await ready(page);
  assert(await page.inputValue('#search-view')==='thumbnails','New browser lost the saved search view');
  await page.selectOption('#search-view','extended');await page.waitForFunction(()=>!document.querySelector('#search-view').disabled);
  await page.locator('.saved-tag-values button').filter({hasText:'示例画师'}).first().click();
  assert((await page.locator('#selected-tags').textContent()).includes('示例画师'),'Tag click failed');
  await page.locator('#search-button').click();await ready(page);
  assert(await page.locator('.saved-card').count()===6,'Added tag did not filter the search');
  await page.goto(base+'/records');await page.locator('.saved-card').first().waitFor();
  assert(await page.inputValue('#record-view')==='compact','Search view overwrote saved record view');
  assert(errors.length===0,'Browser script errors: '+errors.join('; '));
  console.log(JSON.stringify({passed:true,views:4,widths:[1440,390],completeTags:true,zeroAndUnknown:true,originalCollector:true,sourceSwitch:true,pagination:true,independentSavedViews:true,freshBrowser:true,tagFilter:true,scriptErrors:errors}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server){server.kill();await new Promise(resolve=>server.once('exit',resolve));}fs.rmSync(fixture,{recursive:true,force:true});});
