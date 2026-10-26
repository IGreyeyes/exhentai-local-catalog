"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline'),assert=require('assert');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-rating-library-'));
let server,browser,base;
async function start(){
  server=spawn(path.join(project,'.venv-desktop/Scripts/python.exe'),['-X','utf8','-u',path.join(__dirname,'visual_rating_library_server.py'),fixture],{windowsHide:true});
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
  let searches=0;
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/search')searches++;});
  await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    return url.hostname==='127.0.0.1'&&!url.pathname.startsWith('/api/cover/')?route.continue():route.abort();
  });
  const state=async()=>await(await page.request.get(base+'/api/collector')).json();
  const open=async()=>{
    await page.goto(base+'/#collector-panel');
    await page.waitForFunction(()=>!document.querySelector('#collector-host').disabled);
  };
  const apply=async()=>{
    await page.locator('#collector-host').selectOption('e-hentai.org');
    await page.locator('#collector-save').click();
    await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.startsWith('设置已应用')&&!document.querySelector('#collector-start').disabled);
  };
  const preview=async label=>{
    await page.locator('#collector-start').click();
    await page.waitForFunction(()=>!document.querySelector('#collector-estimate').hidden);
    assert((await page.locator('#estimate-scope').textContent()).includes(label));
  };
  const verify=async()=>{
    await page.locator('#collector-verify').click();
    await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.includes('随机验证成功')&&!document.querySelector('#collector-start').disabled);
  };
  await open();assert(await page.locator('#collector-start').isDisabled());
  await page.locator('#collector-mode').selectOption('rating_full');
  assert(await page.locator('#collector-rating-priority').isChecked());assert(await page.locator('#collector-rating-priority').isDisabled());
  assert(await page.locator('#collector-batch-size').isDisabled());
  await apply();assert(await page.locator('#collector-verify').isEnabled());
  await preview('整个作品目录');
  assert.equal(await page.locator('#estimate-total').textContent(),'12 条');
  assert.equal(await page.locator('#estimate-pending').textContent(),'11 条');
  assert(await page.locator('#collector-confirm').isDisabled(),'Access verification is required');
  assert.equal(searches,0,'Library mode must work without a search');
  assert(!fs.existsSync(path.join(fixture,'requests.json')),'Preview must not request the source');
  await page.locator('#collector-policy-settings').screenshot({path:path.join(project,'logs/rating-library-desktop.png')});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile page overflow');
  await page.locator('#collector-policy-settings').screenshot({path:path.join(project,'logs/rating-library-mobile.png')});
  await page.setViewportSize({width:1440,height:1000});
  await verify();const samples=calls().length;
  await preview('整个作品目录');assert.equal(await page.locator('#collector-confirm').textContent(),'确认按评分全库采集');
  await page.locator('#collector-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#collector-job-state').textContent==='采集中');
  await page.locator('#collector-pause').click();
  await page.waitForFunction(()=>document.querySelector('#collector-start').textContent==='继续采集'&&!document.querySelector('#collector-start').disabled);
  const paused=await state(),processed=calls().slice(samples),expected=[9,4,8,3,12,7,2,11,6,10,5];
  assert.deepEqual(processed,expected.slice(0,processed.length));
  assert.equal(paused.job.collection_mode,'rating_full');assert(paused.job.pending>0);
  await stop();await start();await open();
  await page.waitForFunction(()=>document.querySelector('#collector-mode').value==='rating_full');
  await apply();await verify();const resumeSamples=calls().length;
  await preview('继续任务');await page.locator('#collector-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#collector-job-state').textContent==='采集完成');
  assert.deepEqual(calls().slice(resumeSamples),expected.slice(processed.length));
  assert.equal((await state()).job.id,paused.job.id);
  assert.equal(searches,0);
  // A narrow search remains independent from the next full-library plan.
  await page.goto(base+'/?tag=other%3Asample#collector-panel');
  await page.waitForSelector('#results .saved-card');
  assert((await page.locator('#results-summary').textContent()).includes('共 5 条'));
  await apply();const before=calls().length;
  await preview('整个作品目录');
  assert.equal(await page.locator('#estimate-total').textContent(),'12 条');
  assert.equal(await page.locator('#estimate-pending').textContent(),'0 条');
  await page.locator('#collector-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.includes('无需发送请求'));
  assert.equal(calls().length,before);
  await page.locator('#collector-mode').selectOption('rounds');
  assert(await page.locator('#collector-batch-size').isEnabled());assert(await page.locator('#collector-rating-priority').isEnabled());
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,noSearch:true,previewOnlyLocal:true,verificationRequired:true,globalRatingOrder:true,pauseAndRestart:true,searchScopeIndependent:true,cachedZeroRequests:true,mobileWidth:390}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();await stop();
  assert(path.resolve(fixture).startsWith(path.resolve(os.tmpdir())+path.sep+'excatalog-rating-library-'));
  fs.rmSync(fixture,{recursive:true,force:true});
});
