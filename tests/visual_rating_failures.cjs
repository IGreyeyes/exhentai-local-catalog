"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline'),assert=require('assert');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-rating-failures-'));
let server,browser,base;
async function start(){
  server=spawn(path.join(project,'.venv-desktop/Scripts/python.exe'),['-X','utf8','-u',path.join(__dirname,'visual_rating_library_server.py'),fixture,'failures'],{windowsHide:true});
  let stderr='';server.stderr.on('data',chunk=>stderr+=String(chunk));
  base=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture timeout: '+stderr)),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+' '+stderr));});
  });
}
async function stop(){if(server&&server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}}
const calls=()=>JSON.parse(fs.readFileSync(path.join(fixture,'requests.json'),'utf8'));
(async()=>{
  fs.mkdirSync(path.join(project,'logs'),{recursive:true});
  await start();browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    return url.hostname==='127.0.0.1'&&!url.pathname.startsWith('/api/cover/')?route.continue():route.abort();
  });
  const state=async()=>await(await page.request.get(base+'/api/collector')).json();
  const waitState=async predicate=>{
    const deadline=Date.now()+15000;
    while(Date.now()<deadline){
      const current=await state();
      if(predicate(current)){await page.evaluate(()=>window.collectorUI.refresh());return current;}
      await new Promise(resolve=>setTimeout(resolve,60));
    }
    throw new Error('Collection state timeout');
  };
  const open=async()=>{
    await page.goto(base+'/#collector-panel');
    await page.waitForFunction(()=>!document.querySelector('#collector-host').disabled);
  };
  const apply=async()=>{
    await page.locator('#collector-host').selectOption('e-hentai.org');
    await page.locator('#collector-interval').selectOption('0.5');
    await page.locator('#collector-save').click();
    await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.startsWith('设置已应用')&&!document.querySelector('#collector-start').disabled);
  };
  const verify=async()=>{
    await new Promise(resolve=>setTimeout(resolve,600));
    const response=page.waitForResponse(reply=>new URL(reply.url()).pathname==='/api/collector/verify'&&reply.request().method()==='POST');
    await page.locator('#collector-verify').click();
    const reply=await response,data=await reply.json();
    assert.equal(reply.status(),200,JSON.stringify(data));
    assert(!data.no_request,JSON.stringify(data));
    await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.includes('随机验证成功')&&!document.querySelector('#collector-start').disabled);
  };
  const preview=async()=>{
    await page.locator('#collector-start').click();
    await page.waitForFunction(()=>!document.querySelector('#collector-estimate').hidden);
  };
  await open();await page.locator('#collector-mode').selectOption('rating_full');
  assert(await page.locator('#collector-failure-policy').isVisible());
  assert(!await page.locator('#collector-retry-previous-failures').isChecked());
  await apply();await verify();await preview();await page.locator('#collector-confirm').click();
  await waitState(current=>current.job?.failed===3&&current.job?.succeeded>=1);
  await page.locator('#collector-cancel').click();
  const cancelled=await waitState(current=>current.job?.state==='cancelled'&&!current.busy);
  assert(cancelled.job.pending>0);
  await stop();await start();await open();await apply();
  assert(!await page.locator('#collector-retry-previous-failures').isChecked());
  await preview();assert.equal(await page.locator('#estimate-failures').textContent(),'3 条');
  assert.equal(await page.locator('#estimate-pending').textContent(),`${cancelled.job.pending} 条`);
  await page.locator('#collector-estimate').screenshot({path:path.join(project,'logs/rating-failures-preview.png')});
  await verify();const before=calls().length;
  await preview();await page.locator('#collector-confirm').click();
  const completed=await waitState(current=>current.job?.state==='completed_with_errors'&&!current.busy);
  assert.equal(completed.job.reused_failures,3);
  assert(calls().slice(before).every(gid=>![9,4,8].includes(gid)),'Rebuilt plan must not request saved failures');
  await page.waitForFunction(()=>document.querySelector('#collector-job-counts').textContent.includes('本次失败 0'));
  assert((await page.locator('#collector-job-counts').textContent()).includes('沿用失败记录 3（未请求）'));
  await page.locator('#collector-job-errors').evaluate(node=>node.open=true);
  assert((await page.locator('#collector-error-list').textContent()).includes('未重新请求'));
  await page.locator('#collector-job').screenshot({path:path.join(project,'logs/rating-failures-reused.png')});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile page overflow');
  await page.locator('#collector-policy-settings').screenshot({path:path.join(project,'logs/rating-failures-mobile.png')});
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#collector-retry-previous-failures').check();await apply();
  const feedback=await page.locator('#collector-feedback').textContent();
  assert(feedback.includes('开启重新采集'));
  await page.evaluate(()=>window.collectorUI.refresh());
  assert.equal(await page.locator('#collector-feedback').textContent(),feedback,'Status polling must retain the settings result');
  await preview();assert.equal(await page.locator('#estimate-pending').textContent(),'3 条');
  assert.equal(await page.locator('#estimate-failures').textContent(),'0 条');
  await verify();const retryBefore=calls().length;
  await preview();await page.locator('#collector-confirm').click();
  await waitState(current=>current.job?.state==='completed_with_errors'&&!current.busy);
  assert.deepEqual(calls().slice(retryBefore),[9,4,8]);
  await page.waitForFunction(()=>document.querySelector('#collector-job-counts').textContent.includes('本次失败 3'));
  assert((await page.locator('#collector-job-counts').textContent()).includes('沿用失败记录 0'));
  await stop();await start();await open();
  assert(await page.locator('#collector-retry-previous-failures').isChecked(),'Retry preference must survive restart');
  await page.locator('#collector-retry-previous-failures').uncheck();await apply();
  fs.writeFileSync(path.join(fixture,'source-recovered'),'test recovery');
  await verify();const recoveredBefore=calls().length;
  await page.locator('#collector-retry').click();
  await page.waitForFunction(()=>!document.querySelector('#collector-estimate').hidden);
  assert((await page.locator('#estimate-scope').textContent()).includes('重试失败项'));
  await page.locator('#collector-confirm').click();
  await waitState(current=>current.job?.state==='completed'&&!current.busy);
  assert.deepEqual(calls().slice(recoveredBefore),[9,4,8]);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,cancelAndRestartSkipsFailures:true,previewAndProgressDistinguishReused:true,retrySwitch:true,savedPreference:true,explicitRetry:true,pollingRetainsFeedback:true,mobileWidth:390}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();await stop();
  assert(path.resolve(fixture).startsWith(path.resolve(os.tmpdir())+path.sep+'excatalog-rating-failures-'));
  fs.rmSync(fixture,{recursive:true,force:true});
});
