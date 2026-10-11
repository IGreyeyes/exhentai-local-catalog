import assert from 'node:assert/strict';
import worker from '../cloudflare/worker.js';
import {D1} from './cloud_sync_fixture.mjs';

// Isolated D1-compatible SQLite with simulated time; no production service or records.
const env={DB:new D1(),ADMIN_SECRET:'test-only-admin-secret-not-a-real-credential'};
const first='src_'+'A'.repeat(43),second='src_'+'B'.repeat(43);
const owners=['00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002'];
const realNow=Date.now,base=Math.floor(realNow()/1000);let clock=base;
Date.now=()=>clock*1000;
async function call(path,data,token=env.ADMIN_SECRET){
  const response=await worker.fetch(new Request('https://sync.example.invalid'+path,{method:data===undefined?'GET':'POST',
    headers:{Authorization:'Bearer '+token,'Content-Type':'application/json','CF-Connecting-IP':'127.0.0.1'},
    ...(data===undefined?{}:{body:JSON.stringify(data)})}),env);
  const result=await response.json();assert.equal(response.status,200,JSON.stringify(result));return result;
}
const item=(gid,owner)=>({gid,favorite_count:100,rating_count:20,checked_at:base-100,source:'e-hentai.org',collector_id:owner,collector_name:'测试原采集者'});
try{
  await call('/v3/admin/controls',{upload_enabled:true});
  await call('/v3/admin/policy',{trusted_daily_records:20000,ip_daily_records:30000,global_daily_records:100000});
  for(const [token,owner] of [[first,owners[0]],[second,owners[1]]]){
    const source=await call('/v3/register',{collector_id:owner,collector_name:'测试上传者'},token);
    await call('/v3/admin/sources/'+source.source_id,{status:'trusted'});
  }
  for(let start=1;start<=10000;start+=50){await call('/v3/contribute',{version:3,records:Array.from({length:50},(_,i)=>item(start+i,owners[0]))},first);clock+=3;}
  const records=[...Array.from({length:8000},(_,i)=>item(i+1,owners[1])),...Array.from({length:7000},(_,i)=>item(i+10001,owners[1]))];
  const needed=[];let checked=0,checks=0,skipped=0,uploads=0;
  for(let offset=0;offset<records.length;offset+=500){
    const batch=records.slice(offset,offset+500),byId=new Map(batch.map(r=>[r.gid,r]));
    const result=await call('/v3/upload-check',{version:3,records:batch.map(r=>({gid:r.gid,checked_at:r.checked_at}))},second);
    for(const entry of result.records){if(entry.skip)skipped++;else needed.push(byId.get(entry.gid));}
    checked+=batch.length;checks++;clock+=3;
  }
  for(let offset=0;offset<needed.length;offset+=50){
    const batch=needed.slice(offset,offset+50);const result=await call('/v3/contribute',{version:3,records:batch},second);
    assert.equal(result.accepted,batch.length);uploads++;clock+=3;
  }
  const used=env.DB.sqlite.prepare("SELECT used FROM upload_limits WHERE key LIKE 'upload:source:day:%' ORDER BY used").all().map(r=>r.used);
  assert.deepEqual(used,[7000,10000]);assert.equal(skipped,8000);assert.equal(checks,30);assert.equal(uploads,140);
  assert.equal(env.DB.sqlite.prepare('SELECT COUNT(*) n FROM cloud_public').get().n,17000);
  console.log(JSON.stringify({passed:true,userARecords:10000,userBRecords:checked,publicDuplicatesSkipped:skipped,
    summariesRequests:checks,fullRecordRequests:uploads,uploadRecordsChargedToB:7000,simulatedTime:true,measuresInternetSpeed:false}));
}finally{Date.now=realNow;env.DB.close();}
