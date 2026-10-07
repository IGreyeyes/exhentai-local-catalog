"use strict";
const path=require('path'),fs=require('fs'),os=require('os'),readline=require('readline');
const {spawn,execFileSync}=require('child_process');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const fixture=fs.mkdtempSync(path.join(os.tmpdir(),'excatalog-features-'));
const project=path.resolve(__dirname,'..'),output=path.join(project,'logs');
const metadata=JSON.parse(execFileSync('py',['-3.14','-X','utf8','-c','import json,client_version as v;print(json.dumps({"version":v.APP_VERSION,"notes":v.RELEASE_NOTES}))'],{cwd:project,encoding:'utf8',windowsHide:true}));
fs.mkdirSync(output,{recursive:true});
let server,browser;

async function main(){
  server=spawn('py',['-3.14','-X','utf8','-u',path.join(__dirname,'visual_collector_server.py'),fixture,'features'],{windowsHide:true});
  const base=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),15000);
    readline.createInterface({input:server.stdout}).on('line',line=>{if(line.startsWith('http://')){clearTimeout(timer);resolve(line);}});
    server.once('exit',code=>reject(new Error('Fixture exited: '+code)));
  });
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  const stylesheet=await page.request.get(base+'/client-updates.css');
  assert(stylesheet.status()===200&&stylesheet.headers()['content-type'].startsWith('text/css'),'Update stylesheet was not served as CSS');
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/records');await page.locator('.saved-card').first().waitFor();
  await page.locator('.record-select').first().check();
  for(const view of ['minimal','minimal-tags','compact','extended','thumbnails']){
    await page.selectOption('#record-view',view);
    assert(await page.locator('.saved-card').count()===6,'Record count changed in '+view);
    assert((await page.locator('#selected-record-count').textContent()).includes('1'),'Selection lost in '+view);
    const metrics=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth-innerWidth,cardOverflow:[...document.querySelectorAll('.saved-card')].some(card=>card.scrollWidth>card.clientWidth+1),category:[...document.querySelectorAll('.gallery-category')].map(e=>getComputedStyle(e).backgroundColor)}));
    assert(metrics.overflow<=1&&!metrics.cardOverflow,'Layout overflow in '+view);
    assert(new Set(metrics.category).size>=5,'Category colors are indistinguishable');
    await page.locator('#records-heading').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'features-'+view+'.png')});
  }
  await page.reload();await page.locator('.saved-card').first().waitFor();assert(await page.inputValue('#record-view')==='thumbnails','View was not remembered');
  await page.setViewportSize({width:390,height:844});
  for(const view of ['minimal','minimal-tags','compact','extended','thumbnails']){
    await page.selectOption('#record-view',view);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile overflow in '+view);
  }
  await page.goto(base+'/');await page.locator('#collector-panel').evaluate(e=>e.open=true);
  assert(await page.inputValue('#collector-interval')==='4','Default interval is not 4');
  const warnings=[];
  const reject=async dialog=>{warnings.push(dialog.message());await dialog.dismiss();};
  page.on('dialog',reject);await page.selectOption('#collector-interval','1');page.off('dialog',reject);
  assert(await page.inputValue('#collector-interval')==='4','Cancelled risk warning did not restore 4');
  const accept=async dialog=>{warnings.push(dialog.message());await dialog.accept();};
  page.on('dialog',accept);
  for(const seconds of ['3.5','3','2.5','2','1.5','1']){await page.selectOption('#collector-interval',seconds);assert(await page.inputValue('#collector-interval')===seconds,'Fast interval not selectable');}
  page.off('dialog',accept);assert(warnings.length===7&&warnings.every(text=>text.includes('封禁')),'Missing fast interval warning');
  await page.reload();assert(await page.inputValue('#collector-interval')==='4','Fast interval survived page reload');

  await page.addInitScript(({version,notes})=>{
    window.pywebview={api:{save_attachment:async()=>({}),
      client_info:async()=>({version,notes,show_notes:localStorage.getItem('test-client-notes')!=='read',can_install:true}),
      acknowledge_client_notes:async()=>{localStorage.setItem('test-client-notes','read');return {acknowledged:true};},
      check_client_update:async()=>({available:true,latest_version:'1.2.0',notes:'# 测试新版更新说明\n\n- **清晰的分项**：支持 `代码文字`。\n- <img src=x onerror=alert(1)> 应当作为普通文字显示。\n\n说明段落。'}),
      prepare_client_update:async()=>({phase:'downloading'}),client_update_status:async()=>({phase:'ready',percent:100,message:'新版已准备好'}),
      install_client_update:async()=>({installing:true})}};
    addEventListener('load',()=>dispatchEvent(new Event('pywebviewready')));
  },metadata);
  await page.setViewportSize({width:1440,height:1000});await page.reload();
  await page.locator('#client-update-dialog[open]').waitFor();
  assert((await page.locator('#client-notes-read').textContent())==='已读','Read notes button missing');
  assert(await page.locator('.client-note-list li').count()===4,'Update notes are not a structured list');
  assert(await page.locator('.client-release-notes strong').count()===4,'Update highlights are not bold');
  const modal=await page.locator('#client-update-dialog').evaluate(e=>({width:e.getBoundingClientRect().width,font:getComputedStyle(e.querySelector('.client-release-notes')).fontSize,display:getComputedStyle(e).display,overflow:e.scrollWidth>e.clientWidth+1}));
  assert(modal.width>=620&&modal.width<=680&&modal.font==='14px'&&modal.display==='flex'&&!modal.overflow,'Update dialog styles missing: '+JSON.stringify(modal));
  await page.screenshot({path:path.join(output,'update-notification-v'+metadata.version+'.png')});
  await page.locator('#client-update-dialog').screenshot({path:path.join(output,'update-notification-v'+metadata.version+'-detail.png')});
  await page.setViewportSize({width:390,height:480});
  assert(await page.locator('#client-update-dialog').evaluate(e=>{const box=e.getBoundingClientRect(),button=e.querySelector('#client-notes-read').getBoundingClientRect();return box.width<=innerWidth-20&&box.height<=innerHeight-20&&button.bottom<=box.bottom&&e.scrollWidth<=e.clientWidth+1;}),'Update modal or read button overflows small window');
  await page.screenshot({path:path.join(output,'update-notification-v'+metadata.version+'-small.png')});
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#client-notes-read').click();await page.reload();await page.locator('#client-check-update').waitFor();
  assert(!await page.locator('#client-update-dialog').evaluate(e=>e.open),'Read notes shown again');
  await page.locator('#client-check-update').click();await page.locator('#client-install-update').waitFor({state:'visible'});
  assert(await page.locator('.client-release-notes h3').count()===1&&await page.locator('.client-release-notes code').count()===1,'Release headings or inline code are not rendered');
  assert(await page.locator('.client-release-notes img,.client-release-notes script').count()===0,'Release HTML was executed');
  await page.locator('#client-install-update').click();await page.waitForFunction(()=>document.getElementById('client-install-update').textContent==='安装并重新打开');
  await page.screenshot({path:path.join(output,'features-client-update.png')});
  await page.locator('#client-install-update').click();assert((await page.locator('.client-update-message').textContent()).includes('保存'),'Install state missing');
  assert(errors.length===0,'Browser script errors: '+errors.join('; '));
  console.log(JSON.stringify({passed:true,views:5,mobileViews:5,riskWarnings:warnings.length,rememberedView:true,notesAcknowledged:true,updateButtons:true,updateStyles:true,structuredNotes:true,smallDialog:true,safeReleaseText:true,scriptErrors:errors}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server){server.kill();await new Promise(resolve=>server.once('exit',resolve));}fs.rmSync(fixture,{recursive:true,force:true});});
