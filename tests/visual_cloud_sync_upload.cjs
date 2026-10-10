"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-library-upload-'));
let server,browser,baseUrl;
async function main(){
  server=spawn('py',['-3.14','-X','utf8','-u',path.join(__dirname,'visual_cloud_sync_server.py'),fixture,'230','mixed'],{windowsHide:true});
  let startupError='';server.stderr.on('data',chunk=>{startupError+=String(chunk);});
  baseUrl=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture did not start')),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited: '+code+' '+startupError));});
  });
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1360,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(baseUrl+'/maintenance');
  await page.waitForFunction(()=>document.querySelector('#cloud-sync-progress').textContent.includes('服务器已确认 230 条'),{},{timeout:30000});
  const state=await (await fetch(baseUrl+'/api/cloud-sync')).json();
  assert(state.upload_total===230&&state.upload_confirmed===230&&state.upload_remaining===0,'Mixed library upload was incomplete');
  assert(state.session_upload_processed===230,'Mixed library records were omitted or duplicated');
  const scope=await page.locator('label').filter({has:page.locator('#cloud-sync-upload')}).textContent();
  assert(scope.includes('全部成功收藏数'),'Upload label still says self captures only');
  const consent=await page.locator('label').filter({has:page.locator('#cloud-sync-consent')}).textContent();
  assert(consent.includes('自己、他人及采集者未知'),'Consent does not describe the full upload scope');
  assert(await page.locator('#cloud-sync-upload').isChecked(),'Saved upload preference was lost');
  assert((await page.locator('#cloud-sync-progress').textContent()).includes('待上传 0 条'),'Pending count is missing');
  await page.locator('#cloud-sync-section').screenshot({path:path.join(project,'logs/library-upload-wide.png')});
  await page.setViewportSize({width:760,height:900});
  await page.locator('#cloud-sync-section').screenshot({path:path.join(project,'logs/library-upload-small.png')});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Small viewport overflows horizontally');
  await page.locator('#cloud-sync-now').click();
  await page.waitForFunction(()=>!document.querySelector('#cloud-sync-now').disabled&&document.querySelector('#cloud-sync-message').textContent.includes('同步完成'));
  const repeated=await (await fetch(baseUrl+'/api/cloud-sync')).json();
  assert(repeated.session_upload_processed===230,'Repeated sync resent confirmed records');
  assert(errors.length===0,'Script errors: '+errors.join('; '));
  console.log(JSON.stringify({passed:true,mixedLibraryRecords:230,submitted:repeated.session_upload_processed,confirmed:repeated.upload_confirmed,pending:repeated.upload_remaining,repeatDidNotResend:true,smallViewport:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(server){
    const ended=new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
    if(baseUrl){
      const status=await (await fetch(baseUrl+'/api/status',{signal:AbortSignal.timeout(5000)})).json();
      const stopped=await fetch(baseUrl+'/api/maintenance/stop',{method:'POST',headers:{'Content-Type':'application/json','X-Catalog-Token':status.action_token},body:'{}',signal:AbortSignal.timeout(5000)});
      if(!stopped.ok)throw new Error('Fixture service did not accept shutdown');
    }else server.kill();
    await ended;
  }
  const target=path.resolve(fixture);
  if(path.dirname(target)!==path.resolve(os.tmpdir())||!path.basename(target).startsWith('excatalog-library-upload-'))throw new Error('Unexpected fixture cleanup path');
  fs.rmSync(target,{recursive:true,force:true,maxRetries:5,retryDelay:150});
});
