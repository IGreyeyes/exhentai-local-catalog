const fs=require('fs'),path=require('path'),readline=require('readline');
const {spawn,execFileSync}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),output=path.join(project,'logs');
fs.mkdirSync(output,{recursive:true});
// Keep the disposable fixture beside the ignored screenshots for inspection.
const fixture=fs.mkdtempSync(path.join(output,'excatalog-share-'));
const version=execFileSync('py',['-3.14','-c','from client_version import APP_VERSION;print(APP_VERSION)'],{cwd:project,encoding:'utf8',windowsHide:true}).trim();
const stamp=seconds=>new Date(seconds*1000+8*3600000).toISOString().slice(0,19).replace('T',' ');
let server,browser;
async function main(){
  fs.mkdirSync(output,{recursive:true});
  server=spawn('py',['-3.14','-X','utf8','-u',path.join(__dirname,'visual_collector_server.py'),fixture,'features'],{windowsHide:true});
  const base=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>reject(new Error('Fixture exited: '+code)));
  });
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1000},permissions:['clipboard-read','clipboard-write']});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/maintenance#favorites-section');
  await page.waitForFunction(()=>!document.querySelector('#favorites-export').disabled);
  assert(!await page.locator('#favorites-share-guide').evaluate(node=>node.open),'Tutorial must start collapsed');
  await page.locator('#favorites-share-guide>summary').click();
  assert(await page.locator('.favorites-share-steps>li').count()===8,'Expected eight instructions');
  const links=await page.locator('#favorites-share-guide a').evaluateAll(nodes=>nodes.map(node=>({href:node.href,target:node.target,rel:node.rel})));
  assert(links.some(link=>link.href.includes('/categories/%E6%94%B6'))&&links.some(link=>link.href.includes('/discussions/new?category=')),'Share category and posting links missing');
  assert(links.every(link=>link.target==='_blank'&&link.rel.includes('noopener')),'External link isolation missing');
  const downloadPromise=page.waitForEvent('download');await page.locator('#favorites-export').click();
  const download=await downloadPromise,file=path.join(fixture,'export.json');await download.saveAs(file);
  const payload=JSON.parse(fs.readFileSync(file,'utf8'));
  await page.locator('#favorites-share-generated').waitFor({state:'visible'});
  const title=await page.inputValue('#favorites-share-title'),body=await page.inputValue('#favorites-share-body');
  assert(payload.version===2&&Object.keys(payload).length===4,'Transfer format changed');
  assert(payload.records.length===6&&title.includes('6 条')&&body.includes('v'+version),'Wrong count or version');
  const times=payload.records.map(item=>item.checked_at);
  assert(body.includes(stamp(Math.min(...times)))&&body.includes(stamp(Math.max(...times)))&&body.includes('UTC+8'),'Capture range does not describe exported file');
  assert(await page.locator('#favorites-share-guide').evaluate(node=>node.open),'Success did not expand guide');
  await page.locator('#favorites-share-copy-title').click();
  assert(await page.evaluate(()=>navigator.clipboard.readText())===title,'Title clipboard differs');
  await page.locator('#favorites-share-copy-body').click();
  await page.waitForFunction(()=>document.querySelector('#favorites-share-copy-status').textContent.startsWith('正文已复制'));
  assert((await page.evaluate(()=>navigator.clipboard.readText())).replace(/\r\n/g,'\n')===body,'Body clipboard differs');
  await page.locator('#favorites-share-guide').screenshot({path:path.join(output,'favorites-share-guide-v'+version+'.png')});
  for(const width of [1000,390]){
    await page.setViewportSize({width,height:900});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Share guide overflows at '+width);
  }
  await page.locator('#favorites-share-guide>summary').click();
  await page.locator('#favorites-share-guide').screenshot({path:path.join(output,'favorites-share-collapsed-v'+version+'.png')});
  await page.setViewportSize({width:1440,height:1000});
  await page.addInitScript(()=>{
    window.shareExportMode='success';
    window.pywebview={api:{save_attachment:async endpoint=>{
      if(window.shareExportMode==='cancel')return {cancelled:true};
      if(window.shareExportMode==='failure')return {error:'模拟保存失败'};
      const status=await (await fetch('/api/status')).json();
      const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Catalog-Token':status.action_token},body:'{}'});
      const info=JSON.parse(response.headers.get('X-Favorite-Share-Info'));
      if(window.shareExportMode==='empty'){info.record_count=0;info.oldest_checked_at=null;info.newest_checked_at=null;}
      return {cancelled:false,record_count:info.record_count,share_info:window.shareExportMode==='legacy'?null:info};
    }}};
  });
  await page.reload();await page.waitForFunction(()=>!document.querySelector('#favorites-export').disabled);
  assert(!await page.locator('#favorites-share-generated').isVisible(),'Reload kept stale file metadata');
  for(const mode of ['success','cancel','success','failure','empty','legacy']){
    await page.evaluate(mode=>window.shareExportMode=mode,mode);
    await page.locator('#favorites-export').click();
    await page.waitForFunction(()=>!document.querySelector('#favorites-export').disabled);
    if(mode==='success')assert(await page.inputValue('#favorites-share-title')===title,'Desktop metadata path differs');
    if(['cancel','failure','legacy'].includes(mode)){
      assert(!await page.locator('#favorites-share-generated').isVisible(),'Stale summary after '+mode);
      assert(await page.inputValue('#favorites-share-body')==='','Stale text after '+mode);
    }
    if(mode==='empty')assert((await page.inputValue('#favorites-share-body')).includes('无成功收藏数记录'),'Empty file invented capture dates');
  }
  await page.evaluate(()=>window.shareExportMode='success');await page.locator('#favorites-export').click();
  await page.waitForFunction(()=>!document.querySelector('#favorites-export').disabled);
  await page.evaluate(()=>{Object.defineProperty(navigator,'clipboard',{value:{writeText:async()=>{throw new Error('simulated denial');}},configurable:true});document.execCommand=()=>false;});
  await page.locator('#favorites-share-copy-body').click();
  assert((await page.locator('#favorites-share-copy-status').textContent()).includes('Ctrl+C'),'Manual clipboard fallback missing');
  assert(await page.locator('#favorites-share-body').evaluate(node=>node.selectionStart===0&&node.selectionEnd===node.value.length),'Manual fallback did not select whole text');
  assert(errors.length===0,'Browser script errors: '+errors.join('; '));
  console.log(JSON.stringify({passed:true,recordCount:payload.records.length,exactExportRange:true,copyTitle:true,copyBody:true,manualCopyFallback:true,cancellation:true,saveFailure:true,emptyFile:true,legacyBackend:true,reloadClearsMetadata:true,desktopBridge:true,responsiveWidths:[1440,1000,390],scriptErrors:errors}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(server&&server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}
});
