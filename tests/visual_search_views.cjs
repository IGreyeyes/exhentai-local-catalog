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
async function inspect(page,view,width,expectedTags=130){
  await page.selectOption('#search-view',view);
  await page.waitForFunction(()=>!document.querySelector('#search-view').disabled);
  const metrics=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth-innerWidth,cardOverflow:[...document.querySelectorAll('.saved-card')].some(card=>card.scrollWidth>card.clientWidth+1),count:document.querySelectorAll('.saved-card').length}));
  assert(metrics.overflow<=1&&!metrics.cardOverflow,`Overflow in ${view} at ${width}: ${JSON.stringify(metrics)}`);
  assert(metrics.count===40,'View changed the page size');
  assert(await page.locator('.saved-ratio').count()===40&&await page.locator('.saved-ratio').first().isVisible(),'Rating is hidden in '+view);
  assert(await page.locator('.saved-cover').count()===(['extended','thumbnails'].includes(view)?40:0),'Unexpected covers');
  assert(await page.locator('.saved-tag-section button').count()===(['compact','extended'].includes(view)?expectedTags:0),'Tags missing or truncated');
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
  await page.locator('#rating-system-guide summary').click();
  assert(await page.locator('#rating-system-guide tbody tr').count()===5,'Rating thresholds are missing');
  assert((await page.locator('.rating-guide-content').textContent()).includes('至少 20 人评分'),'Minimum rating sample is missing');
  assert((await page.locator('.rating-guide-content').textContent()).includes('2.7（一般）'),'Rating calculation example is missing');
  await page.locator('#rating-system-guide').screenshot({path:path.join(output,'search-rating-guide.png')});
  await page.locator('#rating-system-guide summary').click();
  for(const sort of ['newest','oldest','rating','pages','favorites']){
    await page.goto(base+'/?tag=language%3Aenglish&sort='+sort);await ready(page);
    assert(await page.locator('.saved-ratio').count()===40&&await page.locator('.saved-ratio').first().isVisible(),'Rating missing with search sort '+sort);
  }
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
  for(const sort of ['favorites_desc','favorites_asc','recorded_desc','recorded_asc','rating_desc','state_updated_desc']){
    await page.selectOption('#record-sort',sort);
    await Promise.all([page.waitForResponse(response=>response.url().includes('/api/records?')&&response.status()===200),page.click('#filter-records')]);
    await page.waitForFunction(()=>!document.querySelector('#filter-records').disabled);
    assert(await page.locator('.saved-ratio').count()===6&&await page.locator('.saved-ratio').first().isVisible(),'Rating missing with records sort '+sort);
  }
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:width===390?844:1000});
    for(const view of views){
      await page.selectOption('#record-view',view);await page.waitForFunction(()=>!document.querySelector('#record-view').disabled);
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
      assert(overflow<=1&&await page.locator('.saved-ratio').first().isVisible(),'Record rating layout failed in '+view+' at '+width);
    }
  }
  await page.locator('#rating-system-guide summary').click();
  assert(await page.locator('#rating-system-guide tbody tr').count()===5,'Rating guide missing on records page');
  await page.locator('#rating-system-guide').screenshot({path:path.join(output,'records-rating-guide.png')});
  await page.selectOption('#record-sort','favorites_per_rating');
  await Promise.all([page.waitForResponse(response=>response.url().includes('/api/records?')&&response.status()===200),page.click('#filter-records')]);
  await page.waitForFunction(()=>!document.querySelector('#filter-records').disabled);
  await page.waitForFunction(()=>document.querySelectorAll('.saved-ratio').length===6);
  assert(JSON.stringify(await page.locator('.saved-id').allTextContents())===JSON.stringify([6,3,2,1,5,4].map(gid=>'ID '+gid)),'Record ratio order or tie breaking is wrong');
  assert(await page.locator('.saved-ratio').nth(3).textContent()==='收藏/评分 0.0（冷门）','Real zero ratio was hidden');
  assert(await page.locator('.saved-ratio').nth(4).textContent()==='收藏/评分 样本不足','Small sample was rated');
  assert(await page.locator('.saved-ratio').nth(5).textContent()==='收藏/评分 未评分','Zero denominator was treated as a ratio');
  await page.goto(base+'/?tag=language%3Aenglish&sort=favorites_per_rating');await ready(page);
  assert((await page.locator('#favorite-coverage').textContent()).includes('收藏/评分可评级 4 / 46 条'),'Ratio coverage is wrong');
  const ratioOrder=await page.locator('.saved-id').allTextContents();
  assert(JSON.stringify(ratioOrder.slice(0,5))===JSON.stringify(['ID 6','ID 3','ID 2','ID 1','ID 46']),'Search ratio order is wrong');
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:width===390?844:1000});
    for(const view of views){
      await inspect(page,view,width,100);
      assert(JSON.stringify(await page.locator('.saved-id').allTextContents())===JSON.stringify(ratioOrder),'Ratio view changed the order');
      assert(await page.locator('.saved-ratio').first().textContent()==='收藏/评分 308.5（杰作）','Ratio is missing in '+view);
    }
    await page.locator('.results-section').evaluate(section=>section.scrollIntoView({block:'start'}));
    await page.screenshot({path:path.join(output,`search-ratio-${width}.png`)});
  }
  assert(errors.length===0,'Browser script errors: '+errors.join('; '));
  console.log(JSON.stringify({passed:true,views:4,widths:[1440,390],completeTags:true,zeroAndUnknown:true,originalCollector:true,sourceSwitch:true,pagination:true,independentSavedViews:true,freshBrowser:true,tagFilter:true,ratioSortBothPages:true,ratioAllSorts:true,expandableRatingGuide:true,minimumRatingSample:true,ratioCoverage:true,scriptErrors:errors}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server){server.kill();await new Promise(resolve=>server.once('exit',resolve));}fs.rmSync(fixture,{recursive:true,force:true});});
