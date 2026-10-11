import assert from 'node:assert/strict';
import worker from '../cloudflare/worker.js';
import {D1} from './cloud_sync_fixture.mjs';

// Isolated SQLite and simulated time. This measures request/query counts, not Internet speed.
const env={DB:new D1(),ADMIN_SECRET:'test-only-admin-secret-not-a-real-credential'};
const token='src_'+'A'.repeat(43),owner='00000000-0000-4000-8000-000000000001';
const realNow=Date.now,base=Math.floor(realNow()/1000);let clock=base;
Date.now=()=>clock*1000;
async function call(path,data,bearer=env.ADMIN_SECRET){
  const r=await worker.fetch(new Request('https://sync.example.invalid'+path,{method:'POST',
    headers:{Authorization:'Bearer '+bearer,'Content-Type':'application/json','CF-Connecting-IP':'127.0.0.1'},body:JSON.stringify(data)}),env);
  const result=await r.json();assert.equal(r.status,200,JSON.stringify(result));return result;
}
try{
  await call('/v3/admin/controls',{upload_enabled:true});
  await call('/v3/admin/policy',{trusted_daily_records:20000,ip_daily_records:30000,global_daily_records:100000});
  const source=await call('/v3/register',{collector_id:owner,collector_name:'测试上传者'},token);
  await call('/v3/admin/sources/'+source.source_id,{status:'trusted'});
  let requests=0,confirmed=0,maxQueries=0;
  for(let start=1;start<=16505;start+=50){
    const records=Array.from({length:Math.min(50,16506-start)},(_,i)=>({gid:start+i,favorite_count:100,rating_count:null,
      checked_at:base-100,source:'e-hentai.org',collector_id:(start+i)%2?owner:'',collector_name:(start+i)%2?'测试原采集者':''}));
    env.DB.queryCount=0;
    const result=await call('/v3/contribute',{version:3,records},token);
    maxQueries=Math.max(maxQueries,env.DB.queryCount);
    assert.equal(result.accepted,records.length);confirmed+=result.accepted;requests++;clock+=3;
  }
  assert.equal(confirmed,16505);assert.equal(requests,331);assert.ok(maxQueries<50);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) n FROM cloud_public').get().n,16505);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) n FROM cloud_history').get().n,16505);
  console.log(JSON.stringify({passed:true,records:confirmed,previousFiveRecordRequests:Math.ceil(16505/5),fiftyRecordRequests:requests,
    maxD1QueriesPerRequest:maxQueries,simulatedTime:true,measuresInternetSpeed:false}));
}finally{Date.now=realNow;env.DB.close();}
