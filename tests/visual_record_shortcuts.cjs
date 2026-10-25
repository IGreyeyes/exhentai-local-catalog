"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline'),assert=require('assert');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-record-shortcuts-'));
let server,browser,base,releaseWrite;
async function start(){
  server=spawn(path.join(project,'.venv-desktop/Scripts/python.exe'),['-X','utf8','-u',path.join(__dirname,'visual_record_shortcuts_server.py'),fixture],{windowsHide:true});
  let stderr='';server.stderr.on('data',chunk=>stderr+=String(chunk));
  base=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture timeout: '+stderr)),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+' '+stderr));});
  });
}
const cover='<svg xmlns="http://www.w3.org/2000/svg" width="176" height="235"><rect width="176" height="235" fill="#e4ecdf"/><path d="M40 65h96v105H40z" fill="#8ca882"/><path d="M55 85h66M55 105h66M55 125h40" stroke="white" stroke-width="5"/></svg>';
(async()=>{
  fs.mkdirSync(path.join(project,'logs'),{recursive:true});
  await start();browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  let queries=0,covers=0,holdNext=false,failNext=false;
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{const url=new URL(request.url());if(url.pathname==='/api/records')queries++;if(/^\/api\/cover\/\d+$/.test(url.pathname))covers++;});
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.hostname!=='127.0.0.1')return route.abort();
    if(/^\/api\/cover\/\d+$/.test(url.pathname))return route.fulfill({contentType:'image/svg+xml',body:cover});
    if(/^\/api\/cover\/\d+\/status$/.test(url.pathname))return route.fulfill({json:{status:'ok'}});
    if(url.pathname==='/api/records/state'){
      if(holdNext){holdNext=false;await new Promise(resolve=>releaseWrite=resolve);releaseWrite=null;}
      if(failNext){failNext=false;return route.fulfill({status:500,json:{error:'模拟保存失败，请重试。'}});}
    }
    return route.continue();
  });
  const ready=()=>page.waitForFunction(()=>document.querySelector('#records-list[aria-busy="false"] .saved-card'));
  const saved=card=>card.locator('.saved-reading-controls[aria-busy="false"]').waitFor();
  const records=async query=>await(await page.request.get(base+'/api/records?'+(query||''))).json();
  const summary=async()=>{
    const actual=(await records()).summary;
    const visible=await page.locator('#record-state-overview button').evaluateAll(buttons=>Object.fromEntries(buttons.map(button=>[button.querySelector('span').textContent,Number(button.querySelector('strong').textContent.replaceAll(',',''))])));
    for(const [state,label] of Object.entries({none:'未分类',planned:'待看',reading:'在看',watched:'看过',ignored:'不看'}))assert.equal(visible[label],actual.states[state]);
    assert.equal(visible['已点开'],actual.opened);
  };
  await page.goto(base+'/records?page=2');await ready();
  for(const view of ['minimal','compact','extended','thumbnails']){
    await page.locator('#record-view').selectOption(view);await page.waitForFunction(()=>!document.querySelector('#record-view').disabled);
    const card=page.locator('.saved-card').nth(2),button=card.locator('[data-reading-state="watched"]');
    await button.scrollIntoViewIfNeeded();
    const before=await page.evaluate(()=>{window.retainedCards=[...document.querySelectorAll('#records-list .saved-card')];return {scroll:scrollY};});
    const requests=queries;
    await button.click();await saved(card);assert.equal(await button.getAttribute('aria-pressed'),'true');
    assert.equal(queries,requests,'Marking must not query the saved list');
    assert(await page.evaluate(()=>retainedCards.every((card,index)=>card===document.querySelectorAll('#records-list .saved-card')[index])),'Card nodes must be retained');
    assert(Math.abs(await page.evaluate(()=>scrollY)-before.scroll)<2,'Marking must retain scroll position');
    assert.equal(await page.locator('#records-page').inputValue(),'2');
    await button.click();await saved(card);assert.equal(await button.getAttribute('aria-pressed'),'false');
    await card.locator('[data-reading-state="ignored"]').click();await saved(card);
    assert.equal(await card.locator('[data-reading-state="ignored"]').getAttribute('aria-pressed'),'true');
    await button.click();await saved(card);assert.equal(await card.locator('[data-reading-state="ignored"]').getAttribute('aria-pressed'),'false');
    await card.locator('select').selectOption('reading');await saved(card);assert.equal(await card.locator('select').inputValue(),'reading');
    await summary();
    await card.screenshot({path:path.join(project,'logs',`record-shortcuts-${view}.png`)});
    await page.setViewportSize({width:390,height:844});await card.scrollIntoViewIfNeeded();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile page overflow');
    assert(await card.evaluate(card=>card.scrollWidth<=card.clientWidth+1),'Mobile card overflow');
    await card.screenshot({path:path.join(project,'logs',`record-shortcuts-${view}-mobile.png`)});
    await page.setViewportSize({width:1440,height:1000});
  }
  await page.locator('#record-view').selectOption('extended');await page.waitForFunction(()=>!document.querySelector('#record-view').disabled);
  const card=page.locator('.saved-card').nth(3),other=page.locator('.saved-card').nth(4),gid=await card.getAttribute('data-gid');
  const previous=await card.locator('select').inputValue();failNext=true;
  await card.locator('select').selectOption('ignored');await saved(card);
  assert.equal(await card.locator('select').inputValue(),previous,'Failed save must restore the stored state');
  assert((await page.locator('#records-error').textContent()).includes('模拟保存失败'));await summary();
  holdNext=true;
  await card.locator('[data-reading-state="watched"]').click();
  await page.waitForFunction(gid=>document.querySelector(`[data-gid="${gid}"] .saved-reading-controls`).getAttribute('aria-busy')==='true',gid);
  assert(await other.locator('[data-reading-state="ignored"]').isEnabled(),'Other works remain available while saving');
  await other.locator('[data-reading-state="ignored"]').click();await saved(other);
  assert.equal(await other.locator('[data-reading-state="ignored"]').getAttribute('aria-pressed'),'true');
  releaseWrite();await saved(card);await summary();
  // Select works across two pages; aggregate deltas must include the off-page selection.
  await card.locator('.record-select').check();await page.locator('#records-previous').click();await ready();
  await page.locator('.saved-card').first().locator('.record-select').check();
  let requests=queries;
  await page.locator('#bulk-record-state').selectOption('planned');await page.locator('#apply-bulk-state').click();
  await page.waitForFunction(()=>document.querySelector('#selected-record-count').textContent==='已选 0 条');
  assert.equal(queries,requests);await summary();
  // Explicit refresh waits for a pending save before querying the new snapshot.
  holdNext=true;
  await page.locator('.saved-card').first().locator('[data-reading-state="ignored"]').click();
  await page.locator('#refresh-records').click();
  assert.equal(queries,requests,'Refresh should wait for pending writes');
  releaseWrite();await ready();await summary();
  await page.goto(base+'/records?state=none&sort=state_updated_desc&page=2');await ready();
  const filtered=page.locator('.saved-card').nth(1),filteredGid=await filtered.getAttribute('data-gid');requests=queries;
  await filtered.locator('[data-reading-state="watched"]').click();await saved(filtered);
  assert.equal(await filtered.getAttribute('data-gid'),filteredGid);assert.equal(queries,requests);
  assert((await page.locator('#record-action-feedback').textContent()).includes('刷新记录'));await summary();
  // Opening the source link updates its badge without replacing cards or reloading covers.
  await page.route('https://e-hentai.org/**',route=>route.fulfill({body:'Simulated source'}));
  const coverRequests=covers,popupPromise=page.waitForEvent('popup');
  await filtered.locator('.saved-title').click();const popup=await popupPromise;await popup.close();
  await page.waitForFunction(gid=>document.querySelector(`[data-gid="${gid}"] .opened-badge`).textContent==='已点开 1 次',filteredGid);
  assert.equal(queries,requests);assert.equal(covers,coverRequests);assert.equal(await filtered.locator('[data-reading-state="watched"]').getAttribute('aria-pressed'),'true');await summary();
  await page.locator('#refresh-records').click();await ready();assert.equal(await page.locator(`[data-gid="${filteredGid}"]`).count(),0);
  await page.goto(base+'/records?collection=failed');await ready();
  const failed=page.locator('.saved-card').first();
  await failed.locator('[data-reading-state="ignored"]').click();await saved(failed);await summary();
  assert.equal((await records('collection=failed')).items[0].reading_state,'ignored');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,views:4,mobileWidths:[390],inlineSave:true,rollback:true,concurrentWorks:true,crossPageBulk:true,refreshDuringSave:true,filteredPosition:true,openedInline:true,failedWork:true}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(releaseWrite)releaseWrite();
  if(browser)await browser.close();
  if(server&&server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}
  assert(path.resolve(fixture).startsWith(path.resolve(os.tmpdir())+path.sep+'excatalog-record-shortcuts-'));
  fs.rmSync(fixture,{recursive:true,force:true});
});
