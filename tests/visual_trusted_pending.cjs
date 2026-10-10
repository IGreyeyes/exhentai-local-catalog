"use strict";
const http=require('http'),path=require('path');
const {chromium}=require(require.resolve('playwright',{paths:[path.resolve(path.dirname(process.execPath),'..')]}));
const assert=(condition,message)=>{if(!condition)throw new Error(message);};
const project=path.resolve(__dirname,'..'),admin='test-only-admin-secret-not-a-real-credential';
let browser;
async function scenario(mode){
  const {D1}=await import('./cloud_sync_fixture.mjs'),{default:worker}=await import('../cloudflare/worker.js');
  const db=new D1(),env={DB:db,ADMIN_SECRET:admin},now=Math.floor(Date.now()/1000);
  const trusted='00000000-0000-4000-8000-000000000001',review='00000000-0000-4000-8000-000000000002',blocked='00000000-0000-4000-8000-000000000003';
  for(const [id,status] of [[trusted,'trusted'],[review,'review'],[blocked,'blocked']]){
    db.sqlite.prepare('INSERT INTO upload_sources VALUES(?,?,?,?,?,?)').run(id,'test-only-hash-'+status,trusted,'测试来源 '+status,status,now);
  }
  const snapshot=gid=>({gid,favorite_count:gid===3?2000000:100,rating_count:null,checked_at:now-100,source:'e-hentai.org',collector_id:'',collector_name:''});
  for(let gid=1;gid<=15;gid++){
    db.sqlite.prepare('INSERT INTO cloud_pending(gid,payload,checked_at,source_id,reason,received_at) VALUES(?,?,?,?,?,?)')
      .run(gid,JSON.stringify(snapshot(gid)),now-100,gid<=13?trusted:gid===14?review:blocked,'新来源待审',now);
  }
  const latest={...snapshot(13),favorite_count:120,checked_at:now-50};
  db.sqlite.prepare('INSERT INTO cloud_public(gid,payload,checked_at,source_id,received_at) VALUES(?,?,?,?,?)').run(13,JSON.stringify(latest),now-50,trusted,now);
  let requests=0,releaseSecond,enteredSecond;
  const secondEntered=new Promise(resolve=>{enteredSecond=resolve;});
  const secondGate=new Promise(resolve=>{releaseSecond=resolve;});
  const server=http.createServer(async(req,res)=>{
    try{
      const chunks=[];for await(const chunk of req)chunks.push(chunk);
      const body=Buffer.concat(chunks),url='http://127.0.0.1'+req.url;
      if(req.url==='/v3/admin/pending/approve-trusted'){
        requests++;
        if(requests===2&&mode==='stop'){enteredSecond();await secondGate;}
        if(requests===2&&mode==='failure'){
          res.writeHead(503,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'模拟批次失败'}));return;
        }
      }
      const response=await worker.fetch(new Request(url,{method:req.method,headers:req.headers,...(body.length?{body}: {})}),env);
      res.writeHead(response.status,Object.fromEntries(response.headers));res.end(Buffer.from(await response.arrayBuffer()));
    }catch(error){res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:error.message}));}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const page=await browser.newPage({viewport:{width:1100,height:950}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let dialogs=0,accept=true;
  page.on('dialog',async dialog=>{dialogs++;if(accept)await dialog.accept();else await dialog.dismiss();});
  try{
    await page.goto('http://127.0.0.1:'+server.address().port+'/admin');
    await page.locator('#secret').fill(admin);await page.locator('#load').click();
    await page.waitForFunction(()=>document.querySelectorAll('#pending .row').length===15&&!document.querySelector('#load').disabled);
    if(mode==='normal'){
      accept=false;await page.locator('#pending-approve-trusted').click();
      await page.waitForFunction(()=>document.querySelector('#pending-bulk-progress').textContent==='已取消批量审批。');
      assert(requests===0,'Cancel submitted an approval request');accept=true;
    }
    await page.locator('#pending-approve-trusted').click();
    if(mode==='stop'){
      let waitTimer;
      try{await Promise.race([secondEntered,new Promise((_,reject)=>{waitTimer=setTimeout(()=>reject(new Error('Second batch never started')),10000);})]);}
      finally{clearTimeout(waitTimer);}
      await page.locator('#pending-bulk-stop').click();releaseSecond();
      await page.waitForFunction(()=>document.querySelector('#pending-bulk-progress').textContent.startsWith('已停止后续批次：')&&!document.querySelector('#load').disabled);
      assert(requests===2,'Stop allowed another batch');
      assert(db.sqlite.prepare('SELECT COUNT(*) AS n FROM cloud_pending WHERE source_id=?').get(trusted).n===3,'Stop did not preserve unfinished trusted candidates');
      await page.locator('#pending-approve-trusted').click();
    }else if(mode==='failure'){
      await page.waitForFunction(()=>document.querySelector('#pending-bulk-progress').textContent.startsWith('批量审批中断：')&&!document.querySelector('#load').disabled);
      assert(db.sqlite.prepare('SELECT COUNT(*) AS n FROM cloud_pending WHERE source_id=?').get(trusted).n===8,'Failure did not preserve remaining candidates');
      await page.locator('#load').click();
      await page.waitForFunction(()=>!document.querySelector('#load').disabled);
      await page.locator('#pending-approve-trusted').click();
    }
    await page.waitForFunction(()=>document.querySelector('#pending-bulk-progress').textContent.startsWith('本次批量审批完成：')&&!document.querySelector('#load').disabled);
    const pending=db.sqlite.prepare('SELECT gid FROM cloud_pending ORDER BY gid').all().map(row=>row.gid);
    assert(JSON.stringify(pending)==='[14,15]','Untrusted or blocked candidates were processed');
    assert(db.sqlite.prepare('SELECT COUNT(*) AS n FROM cloud_history').get().n===13,'Repeated approvals changed history');
    assert(JSON.parse(db.sqlite.prepare('SELECT payload FROM cloud_public WHERE gid=13').get().payload).favorite_count===120,'Newer public record was overwritten');
    if(mode==='normal'){
      assert(dialogs===2,'Main operation asked for more than one confirmation');
      assert((await page.locator('#pending-bulk-progress').textContent()).includes('已批准 12 条'),'Approval progress did not show candidate counts');
      await page.locator('#pending-approve-trusted').locator('..').screenshot({path:path.join(project,'logs/trusted-approval-section.png')});
      await page.setViewportSize({width:375,height:900});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Small admin viewport overflows');
      await page.locator('#pending-bulk-progress').screenshot({path:path.join(project,'logs/trusted-approval-progress.png')});
      await page.locator('#pending-approve-trusted').click();
      await page.waitForFunction(()=>document.querySelector('#pending-bulk-progress').textContent==='当前没有已可信来源的待审核数据。');
    }
    assert(errors.length===0,'Admin script errors: '+errors.join('; '));
    return {mode,passed:true,remainingUntrusted:pending.length,publicWorks:13,requests};
  }finally{
    releaseSecond();await page.close();await new Promise(resolve=>server.close(resolve));db.close();
  }
}
(async()=>{
  browser=await chromium.launch({headless:true,channel:'chrome'});
  const results=[];for(const mode of ['normal','stop','failure'])results.push(await scenario(mode));
  console.log(JSON.stringify({passed:true,scenarios:results}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();});
