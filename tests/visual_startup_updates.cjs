"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline');
const {spawn,execFileSync}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-startup-'));
const metadata=JSON.parse(execFileSync('py',['-3.14','-X','utf8','-c','import json,client_version as v;print(json.dumps({"version":v.APP_VERSION,"notes":v.RELEASE_NOTES}))'],{cwd:project,encoding:'utf8',windowsHide:true}));
let server,browser,baseUrl;

async function scenario(base,{available=false,unread=false,offline=false,delayed=false}={}){
  const context=await browser.newContext({viewport:{width:1360,height:900}});
  const release={available,latest_version:'1.2.0',notes:'- **新版改进**：启动后自动提示更新。'};
  const state={started:false,checks:0,prompted:false,notesRead:!unread,downloads:0,manualChecks:0,phase:delayed?'checking':offline?'failed':'completed'};
  const status=()=>({phase:state.phase,release:offline?null:release,error:offline?'模拟离线':''});
  await context.exposeFunction('startupFixtureCall',operation=>{
    if(operation==='info')return {version:metadata.version,notes:metadata.notes,show_notes:!state.notesRead,can_install:true};
    if(operation==='ack'){state.notesRead=true;return {acknowledged:true};}
    if(operation==='start'){if(!state.started){state.started=true;state.checks++;}return status();}
    if(operation==='status')return status();
    if(operation==='take'){if(state.phase==='completed'&&available&&!state.prompted){state.prompted=true;return {prompt:true,release};}return {prompt:false};}
    if(operation==='manual'){state.manualChecks++;return release;}
    if(operation==='download'){state.downloads++;return {phase:'downloading'};}
    if(operation==='progress')return {phase:'ready',percent:100,message:'新版已经准备好'};
    return {};
  });
  await context.addInitScript(()=>{
    const call=operation=>window.startupFixtureCall(operation);
    window.pywebview={api:{save_attachment:()=>Promise.resolve({}),client_info:()=>call('info'),acknowledge_client_notes:()=>call('ack'),
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
  if(available){
    await page.locator('#client-install-update').click();await page.waitForFunction(()=>document.querySelector('#client-install-update').textContent==='安装并重新打开');
    assert(state.downloads===1,'Confirmed update did not begin a download');
  }
  assert(errors.length===0,'Script errors: '+errors.join('; '));
  await context.close();
  return {available,unread,offline,delayed,startupChecks:state.checks,manualChecks:state.manualChecks,downloads:state.downloads};
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
  for(const options of [{},{available:true},{available:true,unread:true},{offline:true},{available:true,delayed:true}])checks.push(await scenario(base,options));
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
