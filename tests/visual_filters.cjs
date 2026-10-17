"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline');
const {spawn}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(project,'logs/excatalog-filters-'));
let server,browser,base;
async function start(){
  server=spawn(path.join(project,'.venv-desktop/Scripts/python.exe'),['-X','utf8','-u',path.join(__dirname,'visual_filters_server.py'),fixture],{windowsHide:true});
  let stderr='';server.stderr.on('data',chunk=>{stderr+=String(chunk);});
  base=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture timeout: '+stderr)),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>{clearTimeout(timer);reject(new Error('Fixture exited '+code+' '+stderr));});
  });
}
async function stop(){if(server&&server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}}
async function choose(page,id,value,api){
  const pending=page.waitForResponse(response=>response.url().includes(api)&&new URL(response.url()).searchParams.get('category')===(value||null));
  await page.locator(id).selectOption(value);
  const response=await pending,data=await response.json();assert(response.ok(),'Filter API failed');
  await page.waitForFunction(()=>!document.querySelector('#results[aria-busy="true"],#records-list[aria-busy="true"]'));
  return data;
}
(async()=>{
  await start();browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    return url.hostname==='127.0.0.1'&&!url.pathname.startsWith('/api/cover/')?route.continue():route.abort();
  });
  await page.goto(base+'/?tag=language%3Aenglish&sort=newest');
  await page.waitForSelector('#results .saved-card');
  let data=await choose(page,'#search-category','Manga','/api/search?');
  assert(data.total===60&&data.page===1&&data.items.every(item=>item.category==='Manga'),'Search did not filter all matches');
  assert(await page.locator('#category').inputValue()==='Manga','Top category is out of sync');
  await page.locator('#next').click();
  await page.waitForFunction(()=>new URL(location.href).searchParams.get('page')==='2');
  assert(await page.locator('#search-category').inputValue()==='Manga','Paging lost type');
  data=await choose(page,'#search-category','Artist CG','/api/search?');
  assert(data.total===7&&data.page===1,'Changing type did not reset search pagination');
  await page.goBack();
  await page.waitForFunction(()=>document.querySelector('#search-category').value==='Manga'&&document.querySelector('#page-input').value==='2');
  data=await choose(page,'#search-category','Cosplay','/api/search?');assert(data.total===0,'Empty type returned works');
  data=await choose(page,'#search-category','','/api/search?');assert(data.total===97,'All types did not restore search matches');
  await page.locator('#collector-panel').evaluate(node=>{node.open=true;});
  await page.waitForFunction(()=>!document.querySelector('#collector-interval').disabled);
  assert(JSON.stringify(await page.locator('#collector-interval option').evaluateAll(nodes=>nodes.map(node=>node.value)))==='["1","1.5","2","2.5","3"]','Unexpected interval options');
  await page.locator('#collector-interval').selectOption('1.5');
  await page.locator('#collector-save').click();
  await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.startsWith('设置已应用'));
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#collector-interval').value==='1.5');
  await page.goto(base+'/records?tag=language%3Aenglish&sort=favorites_desc');
  await page.waitForSelector('#records-list .saved-card');
  data=await choose(page,'#record-category','Manga','/api/records?');
  assert(data.total===60&&data.items.every(item=>item.category==='Manga'),'Records type is not combined with tag');
  await page.locator('#records-next').click();
  await page.waitForFunction(()=>new URL(location.href).searchParams.get('page')==='2');
  await page.locator('#select-record-page').check();
  data=await choose(page,'#record-category','Artist CG','/api/records?');
  assert(data.total===6&&data.page===1,'Record type did not filter before pagination');
  assert((await page.locator('#selected-record-count').textContent()).includes('0'),'Changing type kept hidden selections');
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#record-category').value==='Artist CG'&&document.querySelector('#records-list').getAttribute('aria-busy')==='false');
  await page.locator('#record-view').selectOption('compact');
  assert(await page.locator('#record-category').inputValue()==='Artist CG','View change lost type');
  await page.locator('#records-heading').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(project,'logs/filters-records-desktop.png')});
  await page.setViewportSize({width:760,height:950});
  await page.screenshot({path:path.join(project,'logs/filters-records-narrow.png')});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Records controls overflow');
  await page.locator('#clear-record-filters').click();
  await page.waitForFunction(()=>document.querySelector('#record-category').value===''&&document.querySelector('#records-list').getAttribute('aria-busy')==='false');
  assert(!new URL(page.url()).searchParams.has('category'),'Clear filters kept type in URL');
  await stop();await start();
  await page.goto(base+'/?tag=language%3Aenglish&category=Artist+CG#collector-panel');
  await page.waitForFunction(()=>document.querySelector('#collector-interval').value==='1.5'&&!document.querySelector('#collector-start').disabled);
  await page.locator('#collector-verify').click();
  await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.includes('随机验证成功'));
  await page.locator('#collector-start').click();
  await page.waitForFunction(()=>!document.querySelector('#collector-confirm').disabled);
  await page.locator('#collector-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#collector-feedback').textContent.includes('计划已保存'));
  await page.waitForFunction(()=>document.querySelector('#collector-job-state').textContent.includes('完成'),null,{timeout:15000});
  assert(await page.locator('#collector-interval').inputValue()==='1.5','Completion reset interval');
  assert(!(await page.locator('#collector-config-state').textContent()).includes('已修改'),'Completion marked settings dirty');
  await page.locator('#results-heading').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(project,'logs/filters-search-narrow.png')});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Search controls overflow');
  assert(errors.length===0,'Browser errors: '+errors.join('; '));
  console.log('PASS: search and records type/tag filtering, pagination, URL history, reset, view changes, interval save/reload/restart/completion, and narrow layouts.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();await stop();
  assert(path.resolve(fixture).startsWith(path.join(project,'logs/excatalog-filters-')),'Unexpected fixture cleanup path');
  fs.rmSync(fixture,{recursive:true,force:true,maxRetries:20,retryDelay:100});
});
