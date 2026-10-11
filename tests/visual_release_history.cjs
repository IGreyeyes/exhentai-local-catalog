"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline');
const {spawn,execFileSync}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-release-history-'));
const metadata=JSON.parse(execFileSync('py',['-3.14','-X','utf8','-c','import json;from release_history import bundled_history,announcement_notes;from client_version import APP_VERSION,RELEASE_NOTES;notes,versions=announcement_notes(APP_VERSION,RELEASE_NOTES,bundled_history());print(json.dumps({"version":APP_VERSION,"notes":notes,"versions":versions}))'],{cwd:project,encoding:'utf8',windowsHide:true}));
let server,browser,base;
(async()=>{
  server=spawn('py',['-3.14','-X','utf8','-u',path.join(__dirname,'visual_release_history_server.py'),fixture],{windowsHide:true});
  let errorOutput='';server.stderr.on('data',chunk=>{errorOutput+=String(chunk);});
  base=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Fixture did not start')),15000);readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+' '+errorOutput));});});
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1360,height:980}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/changelog');await page.locator('#history-list button').first().waitFor();
  assert(await page.locator('#history-list button[aria-pressed=true]').getAttribute('data-version')===metadata.version,'Current version is not selected');
  assert(await page.locator('.history-badge').first().textContent()==='当前版本','Current badge missing');
  await page.locator('#history-list button[data-version="1.1.14"]').click();
  assert((await page.locator('#history-meta').textContent()).includes('合并于 v1.1.17'),'Combined version has an invented date');
  await page.locator('#history-list button[data-version="1.1.21"]').click();
  assert((await page.locator('#history-notes').textContent()).includes('一键审批'),'Archived details missing');
  await page.screenshot({path:path.join(project,'logs/release-history-wide.png')});
  await page.locator('#history-refresh').click();await page.waitForFunction(()=>!document.querySelector('#history-refresh').disabled&&document.querySelector('#history-status').textContent.startsWith('已刷新'));
  assert(await page.locator('#history-list button[data-version="1.2.0-beta.10"]').count()===0,'Prerelease shown by default');
  await page.locator('#history-prerelease').check();await page.locator('#history-list button[data-version="1.2.0-beta.10"]').click();
  assert(await page.locator('#history-notes script,#history-notes img,#history-notes a[href^="javascript:"]').count()===0,'Unsafe release markup rendered');
  assert(!await page.evaluate(()=>window.historyInjected),'Release markup executed');
  assert(await page.locator('#history-notes pre').textContent()==='代码示例\n','Code block missing');
  await page.route('**/api/release-history/refresh',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'模拟离线，内置日志仍可查看。'})}));
  await page.locator('#history-refresh').click();await page.waitForFunction(()=>document.querySelector('#history-status').textContent.includes('模拟离线'));
  assert(await page.locator('#history-list button').count()>10,'Offline failure cleared history');
  await page.setViewportSize({width:760,height:900});await page.locator('#history-prerelease').uncheck();
  await page.locator('#history-list button[data-version="1.1.21"]').click();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Small viewport overflow');
  await page.screenshot({path:path.join(project,'logs/release-history-small.png')});
  for(const route of ['/','/records','/maintenance','/contact']){await page.goto(base+route);assert(await page.locator('a.nav-changelog').isVisible(),'History navigation missing on '+route);}
  const context=await browser.newContext({viewport:{width:1360,height:900}});
  await context.exposeFunction('releaseHistoryFixture',operation=>{
    const release={available:true,current_version:'1.1.10',latest_version:metadata.version,notes:metadata.notes,included_versions:metadata.versions};
    if(operation==='info')return {version:'1.1.10',show_notes:false,can_install:true};
    if(operation==='take')return {prompt:true,release};
    return {phase:'completed',release};
  });
  await context.addInitScript(()=>{const call=operation=>window.releaseHistoryFixture(operation);window.pywebview={api:{save_attachment:()=>Promise.resolve({}),client_info:()=>call('info'),start_client_update_check:()=>call('start'),startup_client_update_status:()=>call('status'),take_startup_update_prompt:()=>call('take')}};addEventListener('load',()=>dispatchEvent(new Event('pywebviewready')));});
  const popup=await context.newPage();popup.on('pageerror',error=>errors.push(error.message));await popup.goto(base+'/');await popup.locator('#client-update-dialog[open]').waitFor();
  const headings=await popup.locator('.client-release-notes h3').allTextContents();
  const shownVersions=[...new Set(headings.map(h=>(h.match(/v?(\d+\.\d+\.\d+)/)||[])[1]).filter(Boolean))];
  assert(JSON.stringify(shownVersions)===JSON.stringify([metadata.version]),'Popup included already published versions');
  assert(!(await popup.locator('.client-release-notes').textContent()).includes('历史更新概览'),'Popup includes historical overview');
  await popup.locator('#client-update-dialog').screenshot({path:path.join(project,'logs/cross-version-update.png')});
  assert(errors.length===0,'UI errors: '+errors.join('; '));await context.close();await page.close();
  console.log(JSON.stringify({passed:true,offlineHistory:true,prereleaseFilter:true,safeMarkup:true,announcementVersions:shownVersions.length,sidebarAllPages:true,smallViewport:true}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(server){const ended=new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});if(base){const status=await(await fetch(base+'/api/status')).json();await fetch(base+'/api/maintenance/stop',{method:'POST',headers:{'Content-Type':'application/json','X-Catalog-Token':status.action_token},body:'{}'});}else server.kill();await ended;}
  const target=path.resolve(fixture);if(path.dirname(target)!==path.resolve(os.tmpdir())||!path.basename(target).startsWith('excatalog-release-history-'))throw new Error('Unexpected cleanup path');fs.rmSync(target,{recursive:true,force:true,maxRetries:5,retryDelay:150});
});
