"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline'),assert=require('assert');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-failure-transfer-')),servers=[];
let browser;
async function start(role){
  const root=path.join(fixture,role);fs.mkdirSync(root);
  const server=spawn(path.join(project,'.venv-desktop/Scripts/python.exe'),['-X','utf8','-u',path.join(__dirname,'visual_failure_transfer_server.py'),root,role],{windowsHide:true});servers.push(server);
  let stderr='';server.stderr.on('data',chunk=>stderr+=String(chunk));
  return await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture timeout: '+stderr)),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+' '+stderr));});
  });
}
(async()=>{
  fs.mkdirSync(path.join(project,'logs'),{recursive:true});
  const source=await start('source'),target=await start('target');
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1000}}),errors=[];
  await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  await page.goto(source+'/maintenance#favorites-section');await page.waitForFunction(()=>!document.querySelector('#favorites-export').disabled);
  assert((await page.locator('#favorites-export').textContent()).includes('失败记录'));
  const downloadPromise=page.waitForEvent('download');await page.locator('#favorites-export').click();
  const download=await downloadPromise,file=path.join(fixture,'shared.ehfavorites.json');await download.saveAs(file);
  const raw=fs.readFileSync(file,'utf8'),payload=JSON.parse(raw);
  assert.equal(payload.version,3);assert.equal(payload.records.length,2);assert.equal(payload.failures.length,3);
  assert(!raw.includes('test-private-proxy-marker')&&!raw.includes('test-private-access-marker'));
  await page.locator('#favorites-share-generated').waitFor({state:'visible'});
  assert((await page.locator('#favorites-share-title').inputValue()).includes('失败 3 条'));
  assert((await page.locator('#favorites-share-body').inputValue()).includes('失败时间范围'));
  const receiver=await context.newPage();receiver.on('pageerror',error=>errors.push(error.message));
  await receiver.goto(target+'/maintenance#favorites-section');await receiver.waitForFunction(()=>!document.querySelector('#favorites-export').disabled);
  const records=async()=>await(await receiver.request.get(target+'/api/records?collection=failed')).json();
  const inspect=async filename=>{
    await receiver.locator('#favorites-file').setInputFiles(filename);await receiver.locator('#favorites-check').click();
    await receiver.locator('#favorites-preview').waitFor({state:'visible'});
  };
  const merge=async()=>{
    await receiver.locator('#favorites-confirm').check();await receiver.locator('#favorites-import').click();
    await receiver.waitForFunction(()=>document.querySelector('#favorites-feedback').textContent.startsWith('收藏数合并完成')&&!document.querySelector('#favorites-export').disabled);
  };
  const original=await records();assert.equal(original.items.length,1);
  await inspect(file);
  for(const [id,expected] of Object.entries({'favorites-total':'2','favorites-added':'1','favorites-kept':'1','favorites-failed-total':'3','favorites-failed-added':'1','favorites-failed-updated':'1','favorites-failed-kept':'1'}))assert.equal(await receiver.locator('#'+id).textContent(),expected,id);
  assert(await receiver.locator('#favorites-import').isDisabled());assert.equal((await records()).items[0].failed_at,original.items[0].failed_at);
  await receiver.locator('#favorites-preview').screenshot({path:path.join(project,'logs/failure-transfer-preview-desktop.png')});
  await receiver.setViewportSize({width:390,height:844});
  assert(await receiver.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile overflow');
  await receiver.locator('#favorites-preview').screenshot({path:path.join(project,'logs/failure-transfer-preview-mobile.png')});
  await receiver.setViewportSize({width:1440,height:1000});await merge();
  const imported=await records();assert.deepEqual(imported.items.map(item=>item.gid).sort((a,b)=>a-b),[2,999]);
  assert.equal(imported.items.find(item=>item.gid===2).favorite_count,60);
  assert.equal(imported.items.find(item=>item.gid===999).source_url,'https://e-hentai.org/g/999/abcdef0123/');
  const state=await(await receiver.request.get(target+'/api/maintenance')).json();assert.equal(state.backup_summary.total,1);
  await inspect(file);assert.equal(await receiver.locator('#favorites-failed-added').textContent(),'0');assert.equal(await receiver.locator('#favorites-failed-kept').textContent(),'3');
  await merge();assert.deepEqual((await records()).items.map(item=>[item.gid,item.failed_at]),imported.items.map(item=>[item.gid,item.failed_at]));
  const failureOnly=path.join(fixture,'failures-only.ehfavorites.json');
  fs.writeFileSync(failureOnly,JSON.stringify({...payload,records:[],failures:[{...payload.failures[0],gid:888}]}));
  await inspect(failureOnly);assert.equal(await receiver.locator('#favorites-total').textContent(),'0');assert.equal(await receiver.locator('#favorites-failed-added').textContent(),'1');
  await merge();assert.equal((await records()).items.length,3);
  const invalid=path.join(fixture,'invalid.ehfavorites.json');fs.writeFileSync(invalid,JSON.stringify({...payload,failures:[{...payload.failures[0],reason:'invalid'}]}));
  await receiver.locator('#favorites-file').setInputFiles(invalid);await receiver.locator('#favorites-check').click();
  await receiver.waitForFunction(()=>document.querySelector('#favorites-feedback').classList.contains('error')&&!document.querySelector('#favorites-check').disabled);
  assert(await receiver.locator('#favorites-preview').isHidden());assert(await receiver.locator('#favorites-import').isDisabled());assert.equal((await records()).items.length,3);
  await receiver.goto(target+'/records?collection=failed');await receiver.waitForSelector('.saved-card');
  assert.equal(await receiver.locator('.saved-card').count(),3);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,sourceSuccesses:2,sourceFailures:3,separateCounts:true,previewReadOnly:true,newerSuccessProtected:true,refreshFailureKept:true,missingCatalogLink:true,backup:true,idempotent:true,failuresOnly:true,invalidFileRejected:true,mobileWidth:390}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  for(const server of servers)if(server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}
  assert(path.resolve(fixture).startsWith(path.resolve(os.tmpdir())+path.sep+'excatalog-failure-transfer-'));fs.rmSync(fixture,{recursive:true,force:true});
});
