"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline');
const {spawn,execFileSync}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-startup-'));
const metadata=JSON.parse(execFileSync('py',['-3.14','-X','utf8','-c','import json,client_version as v;print(json.dumps({"version":v.APP_VERSION,"notes":v.RELEASE_NOTES}))'],{cwd:project,encoding:'utf8',windowsHide:true}));
let server,browser,baseUrl;

async function scenario(base,{available=false,unread=false,offline=false,delayed=false,guide=false,downloadFailure=false,checkFailure=false}={}){
  const context=await browser.newContext({viewport:{width:1360,height:900}});
  const releasesUrl='https://github.com/IGreyeyes/exhentai-local-catalog/releases';
  const release={available,latest_version:'1.2.0',notes:'- **新版改进**：启动后自动提示更新。',release_url:releasesUrl+'/tag/v1.2.0'};
  const state={started:false,checks:0,prompted:false,notesRead:!unread,downloads:0,manualChecks:0,phase:delayed?'checking':offline?'failed':'completed',guideSeen:false};
  const status=()=>({phase:state.phase,release:offline?null:release,error:offline?'模拟离线':''});
  await context.exposeFunction('startupFixtureCall',operation=>{
    if(operation==='info')return {version:metadata.version,notes:metadata.notes,releases_url:releasesUrl,show_notes:!state.notesRead,upload_guide_url:guide&&!state.guideSeen?'/maintenance?upload-guide=1#cloud-sync-section':'',can_install:true};
    if(operation==='ack'){state.notesRead=true;return {acknowledged:true,upload_guide_url:guide&&!state.guideSeen?'/maintenance?upload-guide=1#cloud-sync-section':''};}
    if(operation==='guide'){state.guideSeen=true;return {acknowledged:true};}
    if(operation==='start'){if(!state.started){state.started=true;state.checks++;}return status();}
    if(operation==='status')return status();
    if(operation==='take'){if(state.phase==='completed'&&available&&!state.prompted){state.prompted=true;return {prompt:true,release};}return {prompt:false};}
    if(operation==='manual'){state.manualChecks++;return checkFailure&&state.manualChecks===1?{error:'HTTP Error 403: rate limit exceeded'}:{...release,...(checkFailure?{check_message:'已通过本项目公开发布页完成更新检查。'}:{})};}
    if(operation==='download'){state.downloads++;return {phase:'downloading'};}
    if(operation==='progress')return downloadFailure&&state.downloads===1?{phase:'error',percent:0,message:'下载更新包失败：GitHub 连接中断，已自动重试 3 次。请启用系统代理或手动下载，保留 data、logs、backups 和作品目录压缩包。'}:{phase:'ready',percent:100,message:'新版已经准备好'};
    return {};
  });
  await context.addInitScript(()=>{
    const call=operation=>window.startupFixtureCall(operation);
    window.pywebview={api:{save_attachment:()=>Promise.resolve({}),client_info:()=>call('info'),acknowledge_client_notes:()=>call('ack'),acknowledge_cloud_upload_guide:()=>call('guide'),
      start_client_update_check:()=>call('start'),startup_client_update_status:()=>call('status'),take_startup_update_prompt:()=>call('take'),
      check_client_update:()=>call('manual'),prepare_client_update:()=>call('download'),client_update_status:()=>call('progress')}};
    addEventListener('load',()=>dispatchEvent(new Event('pywebviewready')));
  });
  const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/');await page.locator('#client-check-update').waitFor();
  if(delayed){await page.goto(base+'/records');await page.locator('#client-check-update').waitFor();state.phase='completed';}
  if(unread){
    await page.locator('#client-update-dialog[open]').waitFor();
    assert((await page.locator('#client-update-title').textContent()).includes('已更新到'),'Unread notes were replaced by the startup update prompt');
    assert(!state.prompted&&state.downloads===0,'Startup update interrupted unread notes');
    await page.locator('#client-notes-read').click();
  }
  if(guide){
    await page.waitForURL('**/maintenance?upload-guide=1#cloud-sync-section');
    await page.locator('#cloud-upload-guide').waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.querySelector('#cloud-sync-save').disabled);
    assert(!await page.locator('#cloud-sync-upload').isChecked(),'Upload was enabled by default');
    assert(!await page.locator('#cloud-sync-enabled').isChecked(),'Download was enabled by default');
    assert(!await page.locator('#cloud-sync-consent').isChecked(),'Consent was preselected');
    assert((await page.locator('.maintenance-card').first().getAttribute('id'))==='cloud-sync-section','Upload section is not first');
    await page.waitForFunction(async()=>!(await window.pywebview.api.client_info()).upload_guide_url);
    assert(state.guideSeen,'Guide visit was not remembered');
    assert(!await page.locator('#client-update-dialog').evaluate(e=>e.open),'Update prompt covered the upload guide');
    await page.locator('#cloud-upload-guide-done').click();
  }
  if(available){
    await page.waitForFunction(()=>document.querySelector('#client-update-title').textContent==='发现新版 v1.2.0'&&document.querySelector('#client-update-dialog').open);
    assert((await page.locator('#client-notes-read').textContent())==='稍后更新','Defer update action missing');
    assert(state.downloads===0,'Startup check downloaded a package without confirmation');
    if(unread)await page.locator('#client-update-dialog').screenshot({path:path.join(project,'logs/startup-update-v'+metadata.version+'.png')});
    await page.locator('#client-notes-read').click();
  }else{
    await page.waitForFunction(()=>!document.querySelector('#client-check-update').disabled);
    assert(!await page.locator('#client-update-dialog').evaluate(e=>e.open),'No update or network failure displayed an unsolicited dialog');
  }
  for(const route of ['/records','/maintenance','/']){
    await page.goto(base+route);await page.locator('#client-check-update').waitFor();
    await page.waitForFunction(()=>!document.querySelector('#client-check-update').disabled);
    assert(!await page.locator('#client-update-dialog').evaluate(e=>e.open),'Startup prompt repeated on navigation');
  }
  assert(state.checks===1,'More than one startup network check: '+state.checks);
  await page.locator('#client-check-update').click();await page.waitForFunction(()=>document.querySelector('#client-update-dialog').open&&!document.querySelector('#client-check-update').disabled);
  assert(state.manualChecks===1,'Manual check no longer works');
  if(checkFailure){
    await page.locator('#client-manual-download').waitFor({state:'visible'});
    assert(await page.locator('#client-manual-download').getAttribute('href')===releasesUrl,'Check failure did not link the official releases page');
    assert((await page.locator('#client-update-title').textContent()).includes('暂时无法检查'),'Failed check still claims to be checking');
    assert((await page.locator('#client-update-message').textContent()).includes('访问限额'),'Rate limit should use a Chinese explanation');
    await page.locator('#client-retry-check').waitFor({state:'visible'});
    await page.setViewportSize({width:760,height:560});
    await page.locator('#client-update-dialog').screenshot({path:path.join(project,'logs/update-rate-limit-v'+metadata.version+'.png')});
    await page.locator('#client-retry-check').click();
    await page.waitForFunction(()=>document.querySelector('#client-update-title').textContent.startsWith('当前版本')&&!document.querySelector('#client-check-update').disabled);
    assert(state.manualChecks===2,'Retry did not start another check');
    assert(!await page.locator('#client-retry-check').isVisible(),'Retry action remained after successful check');
    assert(!await page.locator('#client-manual-download').isVisible(),'Stale manual link remained after success');
    assert((await page.locator('#client-update-message').textContent()).includes('公开发布页'),'Fallback result not explained');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Update error overflows a narrow window');
  }
  if(available&&!checkFailure){
    await page.locator('#client-install-update').click();
    if(downloadFailure){
      await page.locator('#client-manual-download').waitFor({state:'visible'});
      assert(await page.locator('#client-manual-download').getAttribute('href')===release.release_url,'Download failure did not link the selected release');
      assert(await page.locator('#client-manual-download').getAttribute('target')==='_blank','Manual download should open externally');
      assert(!await page.locator('#client-install-update').isDisabled(),'Download cannot be retried after failure');
      await page.setViewportSize({width:760,height:560});
      await page.locator('#client-manual-download').scrollIntoViewIfNeeded();
      await page.locator('#client-update-dialog').screenshot({path:path.join(project,'logs/update-download-error-v'+metadata.version+'.png')});
      await page.locator('#client-install-update').click();
      await page.waitForFunction(()=>document.querySelector('#client-install-update').textContent==='安装并重新打开');
      assert(state.downloads===2,'Retry did not start a second download');
      assert(!await page.locator('#client-manual-download').isVisible(),'Stale error link remained after retry');
    }else{
      await page.waitForFunction(()=>document.querySelector('#client-install-update').textContent==='安装并重新打开');
      assert(state.downloads===1,'Confirmed update did not begin a download');
    }
  }
  assert(errors.length===0,'Script errors: '+errors.join('; '));
  await context.close();
  return {available,unread,offline,delayed,guide,downloadFailure,checkFailure,guideSeen:state.guideSeen,startupChecks:state.checks,manualChecks:state.manualChecks,downloads:state.downloads};
}

async function main(){
  server=spawn('py',['-3.14','-X','utf8','-u',path.join(__dirname,'visual_collector_server.py'),fixture,'features'],{windowsHide:true});
  const base=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture did not start')),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>reject(new Error('Fixture exited: '+code)));
  });
  baseUrl=base;
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const checks=[];
  for(const options of [{},{available:true},{available:true,unread:true},{offline:true},{available:true,delayed:true},{available:true,unread:true,guide:true},{guide:true},{available:true,downloadFailure:true},{checkFailure:true}])checks.push(await scenario(base,options));
  console.log(JSON.stringify({passed:true,scenarios:checks}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(server){
    const ended=new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
    if(baseUrl){
      const status=await (await fetch(baseUrl+'/api/status',{signal:AbortSignal.timeout(5000)})).json();
      const stop=await fetch(baseUrl+'/api/maintenance/stop',{method:'POST',headers:{'Content-Type':'application/json','X-Catalog-Token':status.action_token},body:'{}',signal:AbortSignal.timeout(5000)});
      if(!stop.ok)throw new Error('Fixture service did not accept shutdown');
    }else server.kill();
    await ended;
  }
  const cleanupTarget=path.resolve(fixture);
  if(path.dirname(cleanupTarget)!==path.resolve(os.tmpdir())||!path.basename(cleanupTarget).startsWith('excatalog-startup-'))throw new Error('Unexpected fixture cleanup path');
  fs.rmSync(cleanupTarget,{recursive:true,force:true,maxRetries:5,retryDelay:150});
});
