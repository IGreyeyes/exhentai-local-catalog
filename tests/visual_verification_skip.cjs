'use strict';
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline'),assert=require('assert');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-verification-skip-'));
const automaticBatch=process.argv.includes('--automatic-batch');
let server,browser,base;
(async()=>{
  server=spawn('py',['-3.14','-X','utf8','-u',path.join(__dirname,'visual_verification_skip_server.py'),fixture],{windowsHide:true});
  let failure='';server.stderr.on('data',chunk=>failure+=String(chunk));
  base=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Fixture did not start')),15000);readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+' '+failure));});});
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1360,height:980}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/');
  if(!await page.locator('#collector-panel').evaluate(node=>node.open))await page.locator('#collector-panel>summary').click();
  await page.locator('#collector-host').selectOption('e-hentai.org');
  await page.locator('#collector-save').click();
  await page.waitForFunction(()=>!document.querySelector('#collector-verify').disabled);
  const state=async()=>await(await page.request.get(base+'/api/collector')).json();
  await page.locator('#collector-verify').click();
  await page.waitForFunction(()=>!document.querySelector('#collector-skip-unavailable').disabled);
  let status=await state();
  assert.deepStrictEqual(status.fixture_requests,[4]);
  assert.strictEqual(status.job.failed,0);
  assert.strictEqual(status.verification_issue.gid,4);
  assert((await page.locator('#collector-feedback').textContent()).includes('HTTP 404'));
  for(const width of [1360,390]){
    await page.setViewportSize({width,height:980});
    await page.locator('#collector-access-issue').scrollIntoViewIfNeeded();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Viewport overflow');
    await page.screenshot({path:path.join(project,`logs/verification-skip-${width}.png`)});
  }
  await page.waitForTimeout(4100);
  await page.locator('#collector-verify').click();
  await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.includes('验证成功：ID 3'));
  status=await state();
  assert.deepStrictEqual(status.fixture_requests,[4,3]);
  assert.strictEqual(status.verification_issue.gid,4);
  assert.strictEqual(status.job.failed,0);
  if(!automaticBatch){
    await page.locator('#collector-skip-unavailable').click();
    await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.includes('已确认作品 ID 4'));
    status=await state();
    assert.deepStrictEqual(status.fixture_requests,[4,3]);
    assert.strictEqual(status.job.failed,1);
    assert.strictEqual(status.job.current_round,1);
    assert.strictEqual(status.verification_issue,null);
    assert(status.verified,'Confirmation discarded successful source verification');
  }
  await page.locator('#collector-resume').click();
  await page.waitForFunction(()=>!document.querySelector('#collector-estimate').hidden);
  assert.strictEqual((await page.locator('#estimate-pending').textContent()).trim(),automaticBatch?'2 条':'1 条');
  await page.locator('#collector-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#collector-job-state').textContent.includes('有失败项'));
  status=await state();
  assert.deepStrictEqual(status.fixture_requests,automaticBatch?[4,3,4,2]:[4,3,2]);
  assert.strictEqual(status.job.state,'completed_with_errors');
  assert.strictEqual(status.job.failed,1);
  assert.strictEqual(status.verification_issue,null,'Finished source response left a stale confirmation prompt');
  assert.strictEqual(status.verification_deferred,0);
  if(automaticBatch)await page.screenshot({path:path.join(project,'logs/batch-automatic-skip.png')});
  const records=await(await page.request.get(base+'/api/records?collection=failed')).json();
  assert.strictEqual(records.total,1);
  assert.strictEqual(errors.length,0,'UI errors: '+errors.join('; '));
  console.log(JSON.stringify({passed:true,nextVerificationUsesAnotherGallery:true,manualConfirmation:!automaticBatch,automaticBatchSkipsUnavailable:automaticBatch,originalQueueContinues:true,noAutomaticNextRound:true,failureCanBeRetried:true,smallViewport:true}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(server){const ended=new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});if(base){const status=await(await fetch(base+'/api/status')).json();await fetch(base+'/api/maintenance/stop',{method:'POST',headers:{'Content-Type':'application/json','X-Catalog-Token':status.action_token},body:'{}'});}else server.kill();await ended;}
  const target=path.resolve(fixture);if(path.dirname(target)!==path.resolve(os.tmpdir())||!path.basename(target).startsWith('excatalog-verification-skip-'))throw new Error('Unexpected cleanup path');fs.rmSync(target,{recursive:true,force:true,maxRetries:5,retryDelay:150});
});
