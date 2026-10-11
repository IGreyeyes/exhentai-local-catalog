import test from "node:test";
import assert from "node:assert/strict";
import {DatabaseSync} from "node:sqlite";
import {readFileSync} from "node:fs";
import worker from "../cloudflare/worker.js";

import {D1} from "./cloud_sync_fixture.mjs";

const ADMIN='test-only-admin-secret-not-a-real-credential';
const OWNER='00000000-0000-4000-8000-000000000001',OTHER='00000000-0000-4000-8000-000000000002';
const TOKEN='src_'+'A'.repeat(43),TOKEN2='src_'+'B'.repeat(43);
const now=Math.floor(Date.now()/1000);
const schema=readFileSync(new URL('../cloudflare/schema.sql',import.meta.url),'utf8');
function item(gid=1,favorite_count=100,checked_at=now-100,rating_count=20){return {gid,favorite_count,rating_count,checked_at,source:'e-hentai.org',collector_id:OWNER,collector_name:'测试采集者'};}
function fixture(t){
  const env={DB:new D1(),ADMIN_SECRET:ADMIN};t.after(()=>env.DB.close());
  async function call(path,{method='GET',token,data,headers={}}={}){
    const r=await worker.fetch(new Request('https://sync.example.invalid'+path,{method,headers:{
      'CF-Connecting-IP':'127.0.0.1',...(token?{Authorization:'Bearer '+token}:{}),
      ...(data!==undefined?{'Content-Type':'application/json'}:{}),...headers},...(data!==undefined?{body:JSON.stringify(data)}:{})}),env);
    const result=r.headers.get('Content-Type').includes('json')?await r.json():await r.text();if(r.status===503 && env.DB.lastError) result.test_error=env.DB.lastError;return {status:r.status,result,headers:r.headers};
  }
  const manage=(path,data)=>call('/v3/admin/'+path,{method:data===undefined?'GET':'POST',token:ADMIN,data});
  async function register(token=TOKEN,owner=OWNER){await manage('controls',{upload_enabled:true});const r=await call('/v3/register',{method:'POST',token,data:{collector_id:owner,collector_name:'测试来源'}});assert.equal(r.status,200,JSON.stringify(r.result));return r.result;}
  async function trusted(token=TOKEN,owner=OWNER){const s=await register(token,owner);await manage('sources/'+s.source_id,{status:'trusted'});return s;}
  const upload=(records,token=TOKEN)=>call('/v3/contribute',{method:'POST',token,data:{version:3,records}});
  const changes=()=>call('/v3/changes');
  return {env,call,manage,register,trusted,upload,changes};
}
test('上传昵称更新原来源，保留信任与原采集者；旧登记不会覆盖昵称',async t=>{
  const {env,call,manage,trusted,upload,changes}=fixture(t);
  const first=await trusted();await upload([item(1)]);
  const rename=name=>call('/v3/register',{method:'POST',token:TOKEN,data:{collector_id:OWNER,collector_name:'测试来源',uploader_name:name}});
  const renamed=await rename('测试上传者');assert.equal(renamed.status,200);
  assert.deepEqual(renamed.result,{version:3,source_id:first.source_id,status:'trusted'});
  assert.equal((await manage('sources')).result.sources.find(s=>s.id===first.source_id).name,'测试上传者');
  assert.equal((await changes()).result.records[0].snapshot.collector_name,'测试采集者');
  assert.equal((await call('/v3/register',{method:'POST',token:TOKEN,data:{collector_id:OWNER,collector_name:''}})).status,200);
  assert.equal(env.DB.sqlite.prepare('SELECT name FROM upload_sources WHERE id=?').get(first.source_id).name,'测试上传者');
  assert.equal((await rename('')).status,200);
  assert.equal(env.DB.sqlite.prepare('SELECT name FROM upload_sources WHERE id=?').get(first.source_id).name,'');
});
test('新来源昵称登记、校验、凭证归属与封禁仍生效',async t=>{
  const {env,call,manage,register}=fixture(t);const first=await register();
  const post=(name,owner=OWNER,token=TOKEN)=>call('/v3/register',{method:'POST',token,data:{collector_id:owner,collector_name:'测试来源',uploader_name:name}});
  assert.equal((await post('另一个上传者',OWNER,TOKEN2)).status,200);
  assert.equal(env.DB.sqlite.prepare('SELECT name FROM upload_sources WHERE id<>?').get(first.source_id).name,'另一个上传者');
  assert.equal((await post('无权改名',OTHER)).status,401);
  assert.equal((await post('字'.repeat(41))).status,400);
  assert.equal((await post('中\n文')).status,400);
  // Independent quota windows keep this test focused on authorization rather than request caps.
  env.DB.sqlite.exec('DELETE FROM upload_limits');
  assert.equal((await post(null)).status,400);
  await manage('sources/'+first.source_id,{status:'blocked'});
  assert.equal((await post('封禁后改名')).status,403);
  assert.equal(env.DB.sqlite.prepare('SELECT name FROM upload_sources WHERE id=?').get(first.source_id).name,'测试来源');
});
test('默认暂停；公开读取、管理认证、旧客户端提示升级',async t=>{
  const {env,call,manage}=fixture(t);const h=await call('/health');assert.equal(h.result.version,3);assert.equal(h.result.upload_scope,'library');assert.equal(h.result.preserves_original_collector,true);assert.equal(h.result.upload_requires_contribution_code,false);assert.equal(h.result.upload_enabled,false);
  assert.equal(h.result.upload_confirmation,'returning_rows');
  assert.equal(h.result.trusted_pending_bulk_approval,true);
  assert.equal(h.result.max_upload_batch,50);assert.equal(h.result.upload_strategy,'set_based');
  assert.equal((await call('/v3/register',{method:'POST',token:TOKEN,data:{collector_id:OWNER,collector_name:''}})).status,503);
  assert.equal((await call('/v3/changes')).status,200);assert.equal((await call('/v3/admin/status')).status,401);
  assert.equal((await call('/v1/changes')).status,426);assert.equal((await call('/v1/contribute',{method:'POST'})).status,426);
  assert.equal((await call('/v2/changes')).status,426);assert.equal((await call('/v2/contribute',{method:'POST'})).status,426);
  assert.equal((await manage('status')).result.pending_count,0);delete env.ADMIN_SECRET;
  assert.equal((await manage('controls',{upload_enabled:true})).status,503);assert.equal((await call('/v3/changes')).status,200);
});
test('50 条混合结果计数准确、少于免费计划查询上限，重试不重复写历史',async t=>{
  const {env,trusted,upload,changes,manage}=fixture(t);await trusted();
  await upload(Array.from({length:10},(_,i)=>item(i+1)));
  const records=Array.from({length:50},(_,i)=>({...item(i+1,i>=40?2000000:100,now-100,i%2?null:20),
    ...(i%3===0?{collector_id:'',collector_name:''}:i%3===1?{collector_id:OTHER,collector_name:'测试原作者'}:{})}));
  env.DB.queryCount=0;const r=await upload(records);
  assert.equal(r.status,200,JSON.stringify(r.result));
  assert.deepEqual([r.result.accepted,r.result.pending,r.result.kept],[30,10,10]);
  assert.ok(env.DB.queryCount<50,'Exceeded the free request query limit: '+env.DB.queryCount);
  const before=env.DB.sqlite.prepare('SELECT count(*) n FROM cloud_history').get().n;
  const repeated=await upload(records);assert.deepEqual([repeated.result.accepted,repeated.result.pending,repeated.result.kept],[0,0,50]);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) n FROM cloud_history').get().n,before);
  assert.equal((await manage('status')).result.pending_count,10);
  const snapshots=(await changes()).result.records.map(e=>e.snapshot);
  assert.equal(snapshots.find(r=>r.gid===13).collector_id,'');
  assert.equal(snapshots.find(r=>r.gid===14).collector_id,OTHER);
  assert.equal(snapshots.find(r=>r.gid===12).rating_count,null);
});
test('每日额度持久配置、鉴权与审计，不重置已用量，新来源仍限 500',async t=>{
  const {env,call,manage,trusted,upload,register}=fixture(t);const s=await trusted();
  await upload([item()]);
  const used=env.DB.sqlite.prepare('SELECT used FROM upload_limits WHERE key=?').get('upload:source:day:'+s.source_id).used;
  const policy={trusted_daily_records:20000,ip_daily_records:30000,global_daily_records:100000};
  assert.equal((await call('/v3/admin/policy',{method:'POST',data:policy})).status,401);
  for(const v of [{...policy,trusted_daily_records:499},{...policy,ip_daily_records:1.5},{...policy,extra:1}])assert.equal((await manage('policy',v)).status,400);
  assert.equal((await manage('policy',policy)).status,200);
  const m=(await manage('status')).result;for(const key of Object.keys(policy))assert.equal(m.caps[key],policy[key]);
  assert.equal(env.DB.sqlite.prepare('SELECT used FROM upload_limits WHERE key=?').get('upload:source:day:'+s.source_id).used,used);
  assert.ok(m.actions.some(a=>a.action==='upload_policy'));
  env.DB.sqlite.prepare('UPDATE upload_limits SET used=5000 WHERE key=?').run('upload:source:day:'+s.source_id);
  assert.equal((await upload([item(2)])).status,200);
  const review=await register(TOKEN2,OTHER);
  env.DB.sqlite.prepare('INSERT INTO upload_limits VALUES(?,?,?)').run('upload:source:day:'+review.source_id,Math.floor(now/86400)*86400,499);
  const denied=await upload([item(3),item(4)],TOKEN2);assert.equal(denied.status,429);
  assert.equal(denied.headers.get('X-Upload-Limit-Scope'),'day');assert.equal(denied.headers.get('X-Upload-Remaining'),'1');
  assert.ok(Number(denied.headers.get('Retry-After'))>0&&Number(denied.headers.get('Retry-After'))<=86400);
  assert.equal((await upload([item(3)],TOKEN2)).status,200);
  env.DB.failBatch=true;
  assert.equal((await manage('policy',{...policy,trusted_daily_records:30000})).status,503);
  assert.equal((await manage('status')).result.caps.trusted_daily_records,20000);
});
test('已有 v3 服务自动补建额度表，保留开关、来源和数据',async t=>{
  const {env,trusted,upload,manage,call}=fixture(t);await trusted();await upload([item()]);
  env.DB.sqlite.exec('DROP TABLE upload_policy');
  // A newly deployed Worker receives a fresh binding object.
  const replacement={DB:Object.create(env.DB),ADMIN_SECRET:ADMIN};
  const r=await worker.fetch(new Request('https://sync.example.invalid/health'),replacement);
  const health=await r.json();assert.equal(r.status,200);assert.equal(health.max_upload_batch,50);
  assert.ok(replacement.DB.sqlite.prepare("SELECT name FROM sqlite_master WHERE name='upload_policy'").get());
  assert.equal(health.upload_enabled,true);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) n FROM cloud_public').get().n,1);
});
test('自动身份绑定凭证；相同 UUID 的新凭证不会继承信任',async t=>{
  const {env,register,trusted,call}=fixture(t);const first=await trusted();const second=await register(TOKEN2);
  assert.notEqual(first.source_id,second.source_id);assert.equal(second.status,'review');
  assert.equal((await register()).source_id,first.source_id);
  const wrong=await call('/v3/register',{method:'POST',token:TOKEN,data:{collector_id:OTHER,collector_name:''}});assert.equal(wrong.status,401);
  const stored=env.DB.sqlite.prepare('SELECT token_hash FROM upload_sources WHERE id=?').get(first.source_id);
  assert.equal(stored.token_hash.length,64);assert.notEqual(stored.token_hash,TOKEN);
  assert.ok(!JSON.stringify((await call('/v3/admin/sources',{token:ADMIN})).result).includes(stored.token_hash));
});
test('重复设置同一来源状态不重复更新或记录审计，状态仍可正常切换',async t=>{
  const {register,manage}=fixture(t);const s=await register();
  const path='sources/'+s.source_id;
  assert.equal((await manage(path,{status:'trusted'})).result.updated,true);
  const before=(await manage('status')).result.actions;
  assert.equal((await manage(path,{status:'trusted'})).result.updated,false);
  assert.deepEqual((await manage('status')).result.actions,before);
  assert.equal((await manage(path,{status:'review'})).result.updated,true);
  assert.equal((await manage('sources')).result.sources[0].status,'review');
});
test('新来源全量待审；批准后才公开，设可信不自动批准已有候选',async t=>{
  const {register,upload,changes,manage}=fixture(t);const s=await register();
  let r=await upload([item(1),item(2,0,now-100,null)]);assert.equal(r.result.pending,2,JSON.stringify(r.result));assert.equal((await changes()).result.records.length,0);
  await manage('sources/'+s.source_id,{status:'trusted'});assert.equal((await changes()).result.records.length,0);
  for(const p of (await manage('pending')).result.pending)assert.equal((await manage('pending/'+p.id+'/approve',{})).status,200);
  assert.deepEqual((await changes()).result.records.map(e=>e.snapshot.favorite_count),[100,0]);
});
test('一键审批只处理可信来源，覆盖异常候选并保留未可信及封禁候选',async t=>{
  const f=fixture(t);const a=await f.register();
  await f.upload([item(1),{...item(2),collector_id:'',collector_name:''},item(3,2000000)]);
  await f.manage('sources/'+a.source_id,{status:'trusted'});
  const b=await f.register(TOKEN2,OTHER);await f.upload([item(4),item(5)],TOKEN2);
  const third='src_'+'C'.repeat(43),c=await f.register(third,OTHER);await f.upload([item(6)],third);
  await f.manage('sources/'+c.source_id,{status:'blocked'});
  const preview=(await f.manage('pending/trusted-preview')).result;assert.equal(preview.count,3);
  const r=await f.manage('pending/approve-trusted',{after:0,max_id:preview.max_id});assert.equal(r.status,200);
  assert.deepEqual({approved:r.result.approved,kept:r.result.kept,skipped:r.result.skipped,processed:r.result.processed},
    {approved:3,kept:0,skipped:0,processed:3});
  assert.equal(r.result.has_more,false);assert.equal(r.result.remaining,0);
  const pending=(await f.manage('pending')).result.pending;assert.equal(pending.length,3);
  assert.ok(pending.every(row=>[b.source_id,c.source_id].includes(row.source_id)));
  const events=(await f.changes()).result.records;assert.deepEqual(events.map(row=>row.gid),[1,2,3]);
  assert.equal(events[1].snapshot.collector_id,'');
  assert.ok((await f.manage('status')).result.actions.some(row=>row.action==='pending_approve_trusted_batch'));
});
test('一键审批跨多批覆盖全部范围，后续新增候选留到下次且重复操作不新增历史',async t=>{
  const f=fixture(t),source=await f.register();
  for(let start=1;start<=12;start+=5)await f.upload(Array.from({length:Math.min(5,13-start)},(_,index)=>item(start+index)));
  await f.manage('sources/'+source.source_id,{status:'trusted'});
  const preview=(await f.manage('pending/trusted-preview')).result;assert.equal(preview.count,12);
  await f.upload([item(13,2000000)]);
  let after=0,approved=0,batches=0,more=true;
  while(more){
    const response=await f.manage('pending/approve-trusted',{after,max_id:preview.max_id});assert.equal(response.status,200);
    const r=response.result;assert.ok(r.processed<=5);assert.ok(r.next>after);
    after=r.next;approved+=r.approved;more=r.has_more;batches++;
  }
  assert.equal(approved,12);assert.equal(batches,3);
  assert.deepEqual((await f.manage('pending')).result.pending.map(row=>row.gid),[13]);
  const repeated=await f.manage('pending/approve-trusted',{after:0,max_id:preview.max_id});
  assert.equal(repeated.result.processed,0);assert.equal(repeated.result.remaining,0);
  assert.equal((await f.changes()).result.records.length,12);
});
test('批量审批保留更新的公共记录并清理同等或过时的可信候选',async t=>{
  const f=fixture(t),source=await f.register();await f.upload([item(1)]);
  await f.trusted(TOKEN2,OTHER);await f.upload([{...item(1,120,now-50),collector_id:OTHER}],TOKEN2);
  await f.manage('sources/'+source.source_id,{status:'trusted'});
  const scope=(await f.manage('pending/trusted-preview')).result;
  const r=(await f.manage('pending/approve-trusted',{after:0,max_id:scope.max_id})).result;
  assert.equal(r.approved,0);assert.equal(r.kept,1);assert.equal(r.processed,1);
  assert.equal((await f.manage('pending')).result.pending.length,0);
  const records=(await f.changes()).result.records;assert.equal(records.length,1);
  assert.equal(records[0].snapshot.favorite_count,120);
});
test('批量审批事务执行前来源失去信任时不发布或删除候选',async t=>{
  const f=fixture(t),source=await f.register();await f.upload([item()]);
  await f.manage('sources/'+source.source_id,{status:'trusted'});
  const preview=(await f.manage('pending/trusted-preview')).result;
  const original=f.env.DB.batch.bind(f.env.DB);
  f.env.DB.batch=async statements=>{f.env.DB.sqlite.prepare("UPDATE upload_sources SET status='blocked' WHERE id=?").run(source.source_id);return original(statements);};
  const r=(await f.manage('pending/approve-trusted',{after:0,max_id:preview.max_id})).result;
  assert.equal(r.approved,0);assert.equal(r.processed,0);assert.equal(r.skipped,1);
  assert.equal((await f.changes()).result.records.length,0);
  assert.equal((await f.manage('pending')).result.pending.length,1);
});
test('批量审批读取后候选变化时跳过并保留，重新读取可审批新值',async t=>{
  const f=fixture(t),source=await f.register();await f.upload([item()]);
  await f.manage('sources/'+source.source_id,{status:'trusted'});
  const preview=(await f.manage('pending/trusted-preview')).result,original=f.env.DB.batch.bind(f.env.DB);
  const newer=item(1,120,now-50);
  f.env.DB.batch=async statements=>{f.env.DB.sqlite.prepare('UPDATE cloud_pending SET payload=?,checked_at=?').run(JSON.stringify(newer),newer.checked_at);return original(statements);};
  const r=(await f.manage('pending/approve-trusted',{after:0,max_id:preview.max_id})).result;
  assert.equal(r.skipped,1);assert.equal(r.remaining,1);assert.equal(r.approved,0);
  assert.equal((await f.changes()).result.records.length,0);
  f.env.DB.batch=original;
  assert.equal((await f.manage('pending/approve-trusted',{after:0,max_id:preview.max_id})).result.approved,1);
  assert.equal((await f.changes()).result.records[0].snapshot.favorite_count,120);
});
test('批量审批失败整批回滚，暂停上传时仍可管理审批',async t=>{
  const f=fixture(t),source=await f.register();await f.upload([item()]);
  await f.manage('sources/'+source.source_id,{status:'trusted'});await f.manage('controls',{upload_enabled:false});
  const scope=(await f.manage('pending/trusted-preview')).result;
  const before=(await f.manage('status')).result.actions.length;
  f.env.DB.failBatch=true;
  assert.equal((await f.manage('pending/approve-trusted',{after:0,max_id:scope.max_id})).status,503);
  assert.equal((await f.changes()).result.records.length,0);
  assert.equal((await f.manage('pending')).result.pending.length,1);
  assert.equal((await f.manage('status')).result.actions.length,before);
  f.env.DB.failBatch=false;
  assert.equal((await f.manage('pending/approve-trusted',{after:0,max_id:scope.max_id})).result.approved,1);
});
test('批量审批预览及执行验证管理员身份、字段和进度，空范围可正常返回',async t=>{
  const f=fixture(t);
  assert.equal((await f.call('/v3/admin/pending/trusted-preview')).status,401);
  assert.equal((await f.call('/v3/admin/pending/approve-trusted',{method:'POST',data:{after:0,max_id:1}})).status,401);
  assert.deepEqual((await f.manage('pending/trusted-preview')).result,{count:0,max_id:0});
  for(const value of [{after:-1,max_id:1},{after:0,max_id:0},{after:2,max_id:1},{after:0,max_id:1,cookie:'test-only-placeholder'},{after:true,max_id:1}]){
    assert.equal((await f.manage('pending/approve-trusted',value)).status,400);
  }
  const empty=(await f.manage('pending/approve-trusted',{after:0,max_id:1})).result;
  assert.equal(empty.processed,0);assert.equal(empty.has_more,false);assert.equal(empty.next,1);
});
test('上传确认数量不计入 D1 触发器写入，混合待审和已有记录可重试',async t=>{
  const {trusted,register,upload,manage}=fixture(t);await trusted();
  await upload([item(1),item(5)]);
  await register(TOKEN2,OTHER);
  const records=[item(1),item(2),{...item(3),collector_id:OTHER},
    {...item(4),collector_id:'',collector_name:''},item(5)];
  const first=await upload(records,TOKEN2);assert.equal(first.status,200);
  assert.deepEqual({accepted:first.result.accepted,pending:first.result.pending,kept:first.result.kept},
    {accepted:0,pending:3,kept:2});
  assert.equal(first.result.accepted+first.result.pending+first.result.kept,records.length);
  assert.equal((await manage('pending')).result.pending.length,3);
  const repeated=await upload(records,TOKEN2);
  assert.deepEqual({accepted:repeated.result.accepted,pending:repeated.result.pending,kept:repeated.result.kept},
    {accepted:0,pending:0,kept:5});
  assert.equal((await manage('pending')).result.pending.length,3);
});
test('同批公开接受、待审和保留各按作品计数，重试不增加历史或候选',async t=>{
  const {trusted,upload,manage,changes}=fixture(t);await trusted();await upload([item(1)]);
  const records=[item(1),item(2),item(3,2000000)];
  const first=await upload(records);assert.equal(first.status,200);
  assert.deepEqual({accepted:first.result.accepted,pending:first.result.pending,kept:first.result.kept},
    {accepted:1,pending:1,kept:1});
  const repeated=await upload(records);
  assert.deepEqual({accepted:repeated.result.accepted,pending:repeated.result.pending,kept:repeated.result.kept},
    {accepted:0,pending:0,kept:3});
  assert.equal((await changes()).result.records.length,2);
  assert.equal((await manage('pending')).result.pending.length,1);
});
test('可信正常变更自动发布；旧数据及重复上传不新增历史',async t=>{
  const {trusted,upload,changes}=fixture(t);await trusted();
  assert.equal((await upload([item()])).result.accepted,1);assert.equal((await upload([item()])).result.kept,1);
  assert.equal((await upload([item(1,200,now-101)])).result.kept,1);
  assert.equal((await upload([item(1,90,now-99)])).result.accepted,1);
  const e=(await changes()).result.records;assert.equal(e.length,2);assert.equal(e.at(-1).snapshot.favorite_count,90);
});
test('资料库混合采集者和未知记录保留归属，上传来源独立认证',async t=>{
  const {env,trusted,upload,changes,call}=fixture(t);const source=await trusted();
  const records=[item(1),{...item(2),collector_id:OTHER,collector_name:'原采集者'},
    {...item(3),collector_id:'',collector_name:''}];
  const submitted=await upload(records);assert.equal(submitted.status,200);assert.equal(submitted.result.accepted,3);
  assert.deepEqual((await changes()).result.records.map(e=>e.snapshot),records);
  const rows=env.DB.sqlite.prepare('SELECT source_id FROM cloud_public ORDER BY gid').all();
  assert.ok(rows.every(row=>row.source_id===source.source_id));
  assert.equal((await upload(records)).result.kept,3);
  const before=env.DB.sqlite.prepare('SELECT COUNT(*) AS n FROM cloud_history').get().n;
  assert.equal(before,3);
  const unknownIdentity=await call('/v3/register',{method:'POST',token:TOKEN2,data:{collector_id:'',collector_name:''}});
  assert.equal(unknownIdentity.status,400);
});
test('转存的未知记录按上传来源待审，通过审核后其他用户可下载',async t=>{
  const {register,upload,changes,manage}=fixture(t);const source=await register();
  const snapshot={...item(),collector_id:'',collector_name:''};
  const submitted=await upload([snapshot]);assert.equal(submitted.result.pending,1);
  assert.equal((await changes()).result.records.length,0);
  const candidate=(await manage('pending')).result.pending[0];assert.equal(candidate.source_id,source.source_id);
  assert.deepEqual(candidate.snapshot,snapshot);
  assert.equal((await manage('pending/'+candidate.id+'/approve',{})).status,200);
  assert.deepEqual((await changes()).result.records[0].snapshot,snapshot);
});
test('转存记录不能继承原采集者来源的信任或绕过封禁',async t=>{
  const {trusted,register,upload,manage}=fixture(t);await trusted(TOKEN,OTHER);
  const submitter=await register(TOKEN2,OWNER);
  assert.equal((await upload([{...item(),collector_id:OTHER}],TOKEN2)).result.pending,1);
  await manage('sources/'+submitter.source_id,{status:'blocked'});
  assert.equal((await upload([{...item(2),collector_id:OTHER}],TOKEN2)).status,403);
});
test('双向异常、归零及已知评分变未知进入待审',async t=>{
  const {trusted,upload,manage,changes}=fixture(t);await trusted();
  for(let gid=1;gid<=5;gid++)await upload([item(gid,1000,now-100,500)]);
  const rows=[item(1,20000,now-90),item(2,300,now-90),item(3,0,now-90),item(4,1000,now-90,null),item(5,1000,now-90,0)];
  assert.equal((await upload(rows)).result.pending,5);assert.equal((await manage('pending')).result.pending.length,5);
  assert.equal((await changes()).result.records.length,5);
});
test('新记录的极端评分人数也待审',async t=>{
  const {trusted,upload}=fixture(t);await trusted();assert.equal((await upload([item(1,100,now-100,1000001)])).result.pending,1);
});
test('字段白名单、数字、时间、同批重复和身份严格检查',async t=>{
  const {trusted,upload,call,changes}=fixture(t);await trusted();
  for(const r of [{...item(),cookie:'test-only-placeholder'},{...item(),favorite_count:true},{...item(),rating_count:-1},
    {...item(),checked_at:now+1000},{...item(),collector_id:'invalid'}, {...item(),collector_id:'',collector_name:'无 ID 的昵称'}, {...item(),collector_name:'\n'}, {...item(),source:'example.invalid'}])assert.equal((await upload([r])).status,400);
  assert.equal((await upload([item(),item()])).status,400);assert.equal((await upload(Array.from({length:51},(_,i)=>item(i+1)))).status,400);
  assert.equal((await upload([item()],TOKEN2)).status,401);assert.equal((await changes()).result.records.length,0);
  const r=await call('/v3/contribute',{method:'POST',token:TOKEN,data:{version:3,records:[item()]},headers:{'Content-Length':'200000'}});assert.equal(r.status,413);
});
test('封禁立即拒绝后续提交和待审批准；下载保留已发布历史',async t=>{
  const {trusted,upload,manage,changes}=fixture(t);const s=await trusted();await upload([item()]);await upload([item(2,2000000)]);
  const p=(await manage('pending')).result.pending[0];await manage('sources/'+s.source_id,{status:'blocked'});
  assert.equal((await upload([item(3)])).status,403);assert.equal((await manage('pending/'+p.id+'/approve',{})).status,409);
  assert.equal((await changes()).result.records.length,1);
});
test('暂停开关、管理员身份继续工作，公共下载继续可用',async t=>{
  const {trusted,upload,manage,changes}=fixture(t);await trusted();await upload([item()]);await manage('controls',{upload_enabled:false});
  assert.equal((await upload([item(2)])).status,503);assert.equal((await changes()).status,200);assert.equal((await manage('status')).status,200);
});
test('IP、来源和全站额度分别生效；IP 不落库',async t=>{
  const {env,trusted,upload,manage}=fixture(t);await trusted();
  for(let i=0;i<30;i++)assert.equal((await upload([item()])).status,200);
  assert.equal((await upload([item()])).status,429);
  const keys=env.DB.sqlite.prepare('SELECT key FROM upload_limits').all();assert.ok(!JSON.stringify(keys).includes('127.0.0.1'));
  env.DB.sqlite.exec("DELETE FROM upload_limits");
  env.DB.sqlite.prepare('INSERT INTO upload_limits VALUES(?,?,?)').run('upload:global:day',Math.floor(now/86400)*86400,50000);
  assert.equal((await upload([item(2)])).status,429);assert.equal((await manage('status')).result.public_count,1);
  env.DB.sqlite.exec('DELETE FROM upload_limits');
  // 已生成的 IP 日计数键来自真实生产配额逻辑；提升使用量后验证 IP 上限。
  await upload([item()]);const key=env.DB.sqlite.prepare("SELECT key FROM upload_limits WHERE key LIKE 'upload:ip:day:%'").get().key;
  env.DB.sqlite.prepare('UPDATE upload_limits SET used=10000 WHERE key=?').run(key);assert.equal((await upload([item(2)])).status,429);
});
test('匿名登记配额和待审每日额度生效',async t=>{
  const {env,register,upload,call}=fixture(t);const s=await register();
  env.DB.sqlite.prepare('INSERT INTO upload_limits VALUES(?,?,?)').run('upload:source:day:'+s.source_id,Math.floor(now/86400)*86400,500);
  assert.equal((await upload([item()])).status,429);
  for(let i=0;i<5;i++)await call('/v3/register',{method:'POST',token:'src_'+String(i).repeat(43),data:{collector_id:OWNER,collector_name:''}});
  assert.equal((await call('/v3/register',{method:'POST',token:TOKEN2,data:{collector_id:OWNER,collector_name:''}})).status,429);
});
test('事务失败与候选容量满不会部分写入或确认上传',async t=>{
  const {env,register,upload,changes}=fixture(t);await register();env.DB.failBatch=true;
  assert.equal((await upload([item(),item(2)])).status,503);assert.equal(env.DB.sqlite.prepare('SELECT count(*) n FROM cloud_pending').get().n,0);
  env.DB.failBatch=false;env.DB.sqlite.exec('UPDATE upload_controls SET pending_count=20000');
  assert.equal((await upload([item()])).status,503);assert.equal((await changes()).result.records.length,0);
});
test('历史恢复和撤回生成新修订，保留较旧时间并防止并发误覆盖',async t=>{
  const {trusted,upload,manage,changes}=fixture(t);await trusted();await upload([item()]);await upload([item(1,110,now-90)]);
  const history=(await manage('history?gid=1')).result;assert.equal(history.history.length,2);
  assert.equal((await manage('restore',{gid:1,revision:1,expected_revision:1})).status,409);
  assert.equal((await manage('restore',{gid:1,revision:1,expected_revision:2})).status,200);
  let events=(await changes()).result.records;assert.equal(events.at(-1).action,'correction');assert.equal(events.at(-1).snapshot.checked_at,now-100);
  assert.equal((await manage('restore',{gid:1,revision:0,expected_revision:3})).status,200);events=(await changes()).result.records;assert.equal(events.at(-1).snapshot,null);
  assert.equal((await manage('status')).result.public_count,0);assert.ok((await manage('status')).result.actions.some(a=>a.action==='restore'));
});
test('历史增量分页允许相同作品重复，固定边界不会丢变更',async t=>{
  const {trusted,upload,call}=fixture(t);await trusted();await upload([item(),item(2)]);await upload([item(1,110,now-90)]);
  const first=(await call('/v3/changes?limit=1')).result;await upload([item(3)]);
  const second=(await call('/v3/changes?after='+first.cursor+'&until='+first.until)).result;
  assert.deepEqual(second.records.map(e=>e.gid),[2,1]);assert.equal(second.cursor,first.until);
  assert.deepEqual((await call('/v3/changes?after='+second.cursor)).result.records.map(e=>e.gid),[3]);
  assert.equal((await call('/v3/changes?dataset_id=test-only-old')).status,409);
  for(const q of ['after=-1','limit=501','until=9999','after=0&after=0','cookie=test-only-placeholder'])assert.equal((await call('/v3/changes?'+q)).status,400);
});
test('已有 v1 库迁移保留最新数据、待审和 dataset；重复执行不复活旧候选',async t=>{
  const {env,call,manage}=fixture(t);env.DB.close();
  const sqlite=new DatabaseSync(':memory:');env.DB.sqlite=sqlite;sqlite.exec(schema.split('CREATE TABLE IF NOT EXISTS upload_controls')[0]);
  sqlite.prepare('INSERT INTO contributors(id,name,token_hash,collector_id,created_at) VALUES(?,?,?,?,?)').run(OWNER,'测试旧来源','test-only-legacy-hash',OWNER,now);
  sqlite.prepare('INSERT INTO public_snapshots(gid,favorite_count,rating_count,checked_at,source,collector_id,collector_name,contributor_id,received_at) VALUES(?,?,?,?,?,?,?,?,?)').run(1,100,20,now-100,'e-hentai.org',OWNER,'',OWNER,now);
  sqlite.prepare('INSERT INTO pending_snapshots(gid,favorite_count,rating_count,checked_at,source,collector_id,collector_name,contributor_id,received_at) VALUES(?,?,?,?,?,?,?,?,?)').run(2,2000000,null,now-100,'e-hentai.org',OWNER,'',OWNER,now);
  const dataset=sqlite.prepare('SELECT dataset_id FROM sync_meta').get().dataset_id;sqlite.exec(schema);
  assert.equal((await call('/health')).result.dataset_id,dataset);assert.equal((await call('/health')).result.upload_enabled,false);
  assert.equal((await call('/v3/changes')).result.records[0].snapshot.favorite_count,100);
  const p=(await manage('pending')).result.pending[0];await manage('pending/'+p.id+'/reject',{});sqlite.exec(schema);
  assert.equal((await manage('pending')).result.pending.length,0);assert.equal((await call('/v3/changes')).result.records.length,1);
});
test('管理页无持久存储或注入；控制台 SQL 与标准 SQL 等价',async t=>{
  const {call,env}=fixture(t);const page=await call('/admin');assert.match(page.headers.get('Content-Security-Policy'),/frame-ancestors 'none'/);
  assert.ok(!/localStorage|sessionStorage|console\.log|innerHTML/.test(page.result));assert.match(page.result,/textContent/);
  assert.equal(page.headers.get('Cache-Control'),'no-store');assert.ok(!page.result.includes(ADMIN));
  env.DB.sqlite.exec(readFileSync(new URL('../cloudflare/schema-console.sql',import.meta.url),'utf8'));
  assert.equal((await call('/health')).status,200);
});
test('按来源撤回预览只选择当前受影响作品，排除其他来源的新值和时间范围外数据',async t=>{
  const {trusted,upload,manage}=fixture(t);const a=await trusted();const b=await trusted(TOKEN2,OTHER);
  await upload([item(1),item(2)]);
  await upload([{...item(1,120,now-80),collector_id:OTHER}],TOKEN2);
  await upload([item(1,130,now-70),item(2,110,now-70)]);
  await upload([{...item(2,120,now-60),collector_id:OTHER}],TOKEN2);
  const preview=(await manage('rollback-preview?source_id='+a.source_id)).result;
  assert.deepEqual(preview.records.map(r=>r.gid),[1]);assert.equal(preview.records[0].revision,3);
  assert.equal((await manage('rollback-preview?source_id='+a.source_id+'&until='+now+'&from='+(now+100))).status,400);
  const r=preview.records[0];assert.equal((await manage('restore',{gid:r.gid,revision:r.revision,expected_revision:r.expected_revision})).status,200);
  assert.equal((await manage('rollback-preview?source_id='+a.source_id)).result.records.length,0);
  assert.ok(b.source_id);
});
test('清理过期限流计数保留有效日额度，定时清理无需管理员凭证',async t=>{
  const {env,register,manage}=fixture(t);await register();
  env.DB.sqlite.prepare('INSERT INTO upload_limits VALUES(?,?,?)').run('test-only-expired',now-3*86400,100);
  assert.equal((await manage('cleanup',{})).result.removed,1);
  assert.ok(env.DB.sqlite.prepare('SELECT * FROM upload_limits WHERE key=?').get('register:global:day'));
  env.DB.sqlite.prepare('INSERT INTO upload_limits VALUES(?,?,?)').run('test-only-expired',now-3*86400,100);
  await worker.scheduled({},env);
  assert.equal(env.DB.sqlite.prepare('SELECT * FROM upload_limits WHERE key=?').get('test-only-expired'),undefined);
});
