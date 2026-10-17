"use strict";
const path=require('path'),fs=require('fs'),readline=require('readline');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  try{
    for(const mode of ['success','limited']){
      const fixture=fs.mkdtempSync(path.join(project,'logs/excatalog-cooldown-'));
      const server=spawn(path.join(project,'.venv-desktop/Scripts/python.exe'),['-X','utf8','-u',path.join(__dirname,'visual_cooldown_server.py'),fixture,mode],{windowsHide:true});
      let stderr='';server.stderr.on('data',chunk=>{stderr+=String(chunk);});
      const page=await browser.newPage({viewport:{width:1360,height:980}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      try{
        const base=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Fixture timeout: '+stderr)),15000);readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+' '+stderr));});});
        await page.goto(base);
        const status=async()=>(await page.request.get(base+'/api/collector')).json();
        await page.waitForFunction(()=>document.querySelector('#collector-panel').open);
        await page.waitForFunction(()=>!document.querySelector('#collector-recover').disabled);
        const before=await status();
        assert(before.fixture_requests.length===0,'Page load sent a source request');
        assert(await page.locator('#collector-verify').isDisabled(),'Ordinary verification bypassed cooldown');
        assert((await page.locator('#collector-cooldown-note').textContent()).includes('每分钟最多请求 1 条'),'Recovery guidance missing');
        await page.locator('#collector-cooldown-note').scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(project,`logs/cooldown-${mode}-before.png`)});
        await page.locator('#collector-recover').click();
        await page.waitForFunction(()=>!document.querySelector('#collector-feedback').textContent.startsWith('正在'));
        const after=await status();
        assert(after.fixture_requests.length===1,'Recovery made more than one source request');
        assert(after.job.state==='paused'&&after.job.pending===3,'Recovery changed or resumed the saved queue');
        assert(JSON.stringify(before.fixture_cached_snapshot)===JSON.stringify(after.fixture_cached_snapshot),'Recovery rewrote cached data');
        if(mode==='success'){
          assert(after.cooldown_until===0&&after.job.cooldown_until===0,'Recovery did not clear persistent cooldown');
          assert(await page.locator('#collector-recover').isHidden(),'Successful recovery still shows cooldown');
          assert((await page.locator('#collector-feedback').textContent()).includes('冷却已解除'),'Success feedback missing');
          assert(!await page.locator('#collector-confirm').isEnabled(),'Recovery allowed collecting without a new preview');
        }else{
          assert(after.cooldown_until>=before.cooldown_until,'Failed recovery shortened cooldown');
          assert(after.cooldown_probe_after>Date.now()/1000,'Recovery interval was not saved');
          assert(await page.locator('#collector-recover').isDisabled(),'Immediate repeat recovery was enabled');
          const token=(await (await page.request.get(base+'/api/status')).json()).action_token;
          const repeat=await page.request.post(base+'/api/collector/verify',{headers:{'X-Catalog-Token':token,'Origin':base},data:{use_job:true,recover_cooldown:true}});
          assert(repeat.status()===400,'API bypassed the recovery interval');
          assert((await status()).fixture_requests.length===1,'Rejected repeat still requested the source');
        }
        await page.setViewportSize({width:760,height:900});
        await page.locator(mode==='success'?'#collector-feedback':'#collector-cooldown-note').scrollIntoViewIfNeeded();
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Recovery controls overflow narrow viewport');
        await page.screenshot({path:path.join(project,`logs/cooldown-${mode}-after.png`)});
        assert(errors.length===0,'Browser errors: '+errors.join('; '));
      }finally{
        await page.close();
        server.kill();
        await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
        assert(path.resolve(fixture).startsWith(path.join(project,'logs/excatalog-cooldown-')),'Unexpected fixture cleanup path');
        fs.rmSync(fixture,{recursive:true,force:true,maxRetries:20,retryDelay:100});
      }
    }
  }finally{await browser.close();}
  console.log('Cooldown UI and API recovery passed: one source request, cached data and queue preserved, success clears cooldown, failure remains throttled, narrow layout fits.');
})().catch(error=>{console.error(error);process.exitCode=1;});
