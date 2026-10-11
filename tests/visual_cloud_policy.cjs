const path=require('path'),readline=require('readline'),{spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=require('assert/strict');let server,browser,base;
(async()=>{
  server=spawn(process.execPath,[path.join(__dirname,'visual_cloud_policy_server.mjs')],{windowsHide:true});
  base=await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(Error('Policy fixture did not start')),10000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timeout);resolve(line);}});});
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1360,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  async function load(){await page.goto(base+'/admin');await page.locator('#secret').fill('test-only-admin-secret-not-a-real-credential');
    await page.locator('#load').click();await page.waitForFunction(()=>document.getElementById('status').textContent==='完成。');}
  await load();assert.equal(await page.locator('#policy-trusted').inputValue(),'5000');
  assert.equal(await page.locator('#policy-interval').inputValue(),'14');
  for(const [id,value] of [['policy-trusted','20000'],['policy-ip','30000'],['policy-global','100000'],['policy-interval','7']])await page.locator('#'+id).fill(value);
  await page.locator('#policy-save').click();await page.waitForFunction(()=>document.getElementById('status').textContent==='完成。');
  await load();assert.equal(await page.locator('#policy-trusted').inputValue(),'20000');assert.equal(await page.locator('#policy-ip').inputValue(),'30000');
  assert.equal(await page.locator('#policy-interval').inputValue(),'7');
  assert.ok((await page.locator('#audit').textContent()).includes('修改上传策略'));
  await page.locator('#policy-interval').fill('0');await page.locator('#policy-save').click();
  await page.waitForFunction(()=>document.getElementById('status').textContent==='完成。');
  await load();assert.equal(await page.locator('#policy-interval').inputValue(),'0');
  await page.locator('section').filter({has:page.locator('#policy-save')}).screenshot({path:path.join(__dirname,'../logs/cloud-upload-policy.png')});
  await page.setViewportSize({width:760,height:900});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,quotaGuiSaved:true,defaultInterval14:true,intervalSaved:true,zeroIntervalSaved:true,reloaded:true,audit:true,smallViewport:true}));
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();try{if(base)await fetch(base+'/__stop');else if(server)server.kill();}catch{if(server)server.kill();}});
