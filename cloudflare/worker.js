/* ExCatalog 公共同步 v3。绑定 D1 为 DB；管理员密钥只存于 ADMIN_SECRET Secret。 */
const VERSION = 3;
const FORMAT = "excatalog-cloud-sync";
// 每批最多 5 条，预留 D1 查询及历史写入额度，兼容免费 Worker 的每请求查询额度。
const MAX_BATCH = 5;
const MAX_BODY = 128 * 1024;
const MAX_COUNT = 1_000_000_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const RECORD_FIELDS = ["gid", "favorite_count", "rating_count", "checked_at", "source", "collector_id", "collector_name"];
const SAFE_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
};

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
function json(value, status = 200) {
  return new Response(JSON.stringify(value), {status, headers: {...SAFE_HEADERS, "Content-Type": "application/json; charset=utf-8"}});
}
function exactKeys(value, fields) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
}
function integer(value, low, high) { return Number.isSafeInteger(value) && value >= low && value <= high; }
function boundedName(value) {
  return typeof value === "string" && [...value].length <= 40 && value === value.trim()
    && !/[\x00-\x1f\x7f-\x9f\uD800-\uDFFF]/u.test(value);
}
async function body(request) {
  if (!request.headers.get("Content-Type")?.toLowerCase().startsWith("application/json")) {
    throw new ApiError(415, "请求须使用 application/json。");
  }
  const length = request.headers.get("Content-Length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_BODY)) {
    throw new ApiError(413, "请求超过 128 KiB。");
  }
  const reader = request.body?.getReader();
  if (!reader) throw new ApiError(400, "请求正文不能为空。");
  const parts = []; let size = 0;
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > MAX_BODY) { await reader.cancel(); throw new ApiError(413, "请求超过 128 KiB。"); }
      parts.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.length; }
  try { return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes)); }
  catch { throw new ApiError(400, "请求不是有效的 UTF-8 JSON。"); }
}
function bearer(request) {
  const match = /^Bearer ([A-Za-z0-9_-]{32,256})$/.exec(request.headers.get("Authorization") || "");
  if (!match) throw new ApiError(401, "需要有效的上传凭证或管理员密钥。");
  return match[1];
}
async function digest(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
  return [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
}
async function admin(request, env) {
  if (typeof env.ADMIN_SECRET !== "string" || !/^[A-Za-z0-9_-]{32,256}$/.test(env.ADMIN_SECRET)) {
    throw new ApiError(503, "请在 Worker 设置中添加 ADMIN_SECRET 加密 Secret（32～256 位字母、数字、下划线或连字符）。");
  }
  const left = await digest(bearer(request)), right = await digest(env.ADMIN_SECRET);
  let different = 0;
  for (let i = 0; i < left.length; i++) different |= left.charCodeAt(i) ^ right.charCodeAt(i);
  if (different) throw new ApiError(401, "管理员密钥不正确。");
}
async function meta(env) {
  if (!env.DB) throw new ApiError(503, "请将 D1 数据库绑定为 DB。");
  let row;
  try { row = await env.DB.prepare("SELECT dataset_id,schema_version FROM sync_meta WHERE id=1").first(); }
  catch { throw new ApiError(503, "数据库尚未初始化，请运行 schema.sql。"); }
  if (!row || row.schema_version !== 1) throw new ApiError(503, "数据库结构版本不受支持。");
  try { const ready = await env.DB.prepare("SELECT migration_done FROM upload_controls WHERE id=1").first(); if (!ready?.migration_done) throw Error("incomplete"); }
  catch { throw new ApiError(503, "请先执行 schema-console.sql 升级数据库。"); }
  return row;
}
function record(value, now) {
  if (!exactKeys(value, RECORD_FIELDS)
      || !integer(value.gid, 1, Number.MAX_SAFE_INTEGER)
      || !integer(value.favorite_count, 0, MAX_COUNT)
      || value.rating_count !== null && !integer(value.rating_count, 0, MAX_COUNT)
      || !integer(value.checked_at, 1, now + 300)
      || !["e-hentai.org", "exhentai.org"].includes(value.source)
      || typeof value.collector_id !== "string" || !(value.collector_id === "" && value.collector_name === ""
        || /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.collector_id))
      || !boundedName(value.collector_name)) {
    throw new ApiError(400, "快照字段、数值、采集者信息或抓取时间不正确；不要上传 Cookie、标题或个人资料。");
  }
  return {...value, collector_id: value.collector_id.toLowerCase()};
}

const SOURCE_TOKEN = /^src_[A-Za-z0-9_-]{43}$/;
const CAPS = {ip_minute:60, ip_daily_records:10000, source_minute:30,
  review_daily_records:500, trusted_daily_records:5000, global_minute:300,
  global_daily_records:50000, register_ip_minute:6, register_ip_daily:20,
  register_global_minute:60, register_global_daily:1000, pending:20000, sources:50000};
async function controls(env) { return await env.DB.prepare('SELECT * FROM upload_controls WHERE id=1').first(); }
async function quota(env, key, now, seconds, units, cap) {
  const start = Math.floor(now/seconds)*seconds;
  const row = await env.DB.prepare(`INSERT INTO upload_limits(key,window_start,used) VALUES(?,?,?)
    ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,
      used=CASE WHEN upload_limits.window_start<>excluded.window_start THEN excluded.used ELSE upload_limits.used+excluded.used END
    WHERE upload_limits.window_start<>excluded.window_start OR upload_limits.used+excluded.used<=?
    RETURNING used`).bind(key,start,units,cap).first();
  if (!row) throw new ApiError(429,'上传频率或今日额度已达上限，请稍后重试。');
}
async function uploadGate(request, env, now, registration=false) {
  if (!(await controls(env)).upload_enabled) throw new ApiError(503,'管理员已暂停上传；公共下载仍可使用。');
  if (typeof env.ADMIN_SECRET !== 'string' || !/^[A-Za-z0-9_-]{32,256}$/.test(env.ADMIN_SECRET)) {
    throw new ApiError(503,'请先配置管理员密钥再开放上传。');
  }
  // Cloudflare 注入此头；仅保存加盐哈希，不保存或输出 IP。
  const address = request.headers.get('CF-Connecting-IP');
  if (!address || address.length>64) throw new ApiError(400,'缺少平台来源信息。');
  const ip = await digest(env.ADMIN_SECRET+'\n'+address);
  const kind = registration?'register':'upload';
  await quota(env,kind+':global:minute',now,60,1,registration?CAPS.register_global_minute:CAPS.global_minute);
  await quota(env,kind+':ip:minute:'+ip,now,60,1,registration?CAPS.register_ip_minute:CAPS.ip_minute);
  if (registration) {
    await quota(env,'register:global:day',now,86400,1,CAPS.register_global_daily);
    await quota(env,'register:ip:day:'+ip,now,86400,1,CAPS.register_ip_daily);
  }
  return ip;
}
async function register(request, env, now) {
  await uploadGate(request,env,now,true);
  const token = bearer(request);
  if (!SOURCE_TOKEN.test(token)) throw new ApiError(401,'自动上传凭证格式不正确，请升级客户端。');
  const value = await body(request);
  if (!exactKeys(value,['collector_id','collector_name'])) throw new ApiError(400,'登记字段不正确。');
  record({gid:1,favorite_count:0,rating_count:null,checked_at:now,source:'e-hentai.org',...value},now);
  if (!value.collector_id) throw new ApiError(400,'上传来源须登记有效的采集者 ID；记录中的未知采集者可保留为空。');
  const hash = await digest(token);
  await env.DB.prepare(`INSERT INTO upload_sources(id,token_hash,collector_id,name,created_at)
    SELECT ?,?,?,?,? WHERE (SELECT source_count FROM upload_controls WHERE id=1)<${CAPS.sources}
    AND (SELECT upload_enabled FROM upload_controls WHERE id=1)=1 ON CONFLICT(token_hash) DO NOTHING`)
    .bind(crypto.randomUUID(),hash,value.collector_id,value.collector_name,now).run();
  const source = await env.DB.prepare('SELECT id,collector_id,status FROM upload_sources WHERE token_hash=?').bind(hash).first();
  if (!source) throw new ApiError(429,'上传来源容量已满或上传已暂停。');
  if (source.collector_id!==value.collector_id) throw new ApiError(401,'上传凭证身份不匹配。');
  if (source.status==='blocked') throw new ApiError(403,'该上传来源已封禁。');
  return json({version:VERSION,source_id:source.id,status:source.status});
}
const AUTH = `EXISTS(SELECT 1 FROM upload_sources WHERE id=? AND collector_id=? AND status<>'blocked')
  AND (SELECT upload_enabled FROM upload_controls WHERE id=1)=1`;
const ELIGIBLE = `NOT EXISTS(SELECT 1 FROM cloud_public WHERE gid=? AND checked_at>=?)`;
// 明显上涨、跌幅超过一半且差值至少 100、突然归零、评分从已知变未知均待审。
const SUSPICIOUS = `(?>1000000 OR (? IS NOT NULL AND ?>1000000) OR EXISTS(SELECT 1 FROM cloud_public
  WHERE gid=? AND payload IS NOT NULL AND (
    ?>max(json_extract(payload,'$.favorite_count')*5+1000,json_extract(payload,'$.favorite_count')+10000)
    OR (json_extract(payload,'$.favorite_count')>=100 AND ?<json_extract(payload,'$.favorite_count')*0.5
      AND json_extract(payload,'$.favorite_count')-?>=100)
    OR (?=0 AND json_extract(payload,'$.favorite_count')>0)
    OR (json_extract(payload,'$.rating_count') IS NOT NULL AND
      (? IS NULL OR ?>max(json_extract(payload,'$.rating_count')*5+1000,json_extract(payload,'$.rating_count')+10000)
      OR (json_extract(payload,'$.rating_count')>=100 AND ?<json_extract(payload,'$.rating_count')*0.5
        AND json_extract(payload,'$.rating_count')-?>=100)
      OR (?=0 AND json_extract(payload,'$.rating_count')>0))))))`;
function suspiciousArgs(r) { return [r.favorite_count,r.rating_count,r.rating_count,r.gid,
  r.favorite_count,r.favorite_count,r.favorite_count,r.favorite_count,
  r.rating_count,r.rating_count,r.rating_count,r.rating_count,r.rating_count]; }
const UPSERT = `ON CONFLICT(gid) DO UPDATE SET payload=excluded.payload,checked_at=excluded.checked_at,
  source_id=excluded.source_id,action=excluded.action,received_at=excluded.received_at`;
async function contribute(request, env, now) {
  const ip = await uploadGate(request,env,now);
  const token = bearer(request);
  if (!SOURCE_TOKEN.test(token)) throw new ApiError(401,'请由新版客户端自动登记上传身份。');
  const source = await env.DB.prepare('SELECT id,collector_id,status FROM upload_sources WHERE token_hash=?')
    .bind(await digest(token)).first();
  if (!source) throw new ApiError(401,'上传身份尚未登记。');
  if (source.status==='blocked') throw new ApiError(403,'该上传来源已封禁。');
  const payload = await body(request);
  if (!exactKeys(payload,['version','records']) || payload.version!==VERSION || !Array.isArray(payload.records)
      || !payload.records.length || payload.records.length>MAX_BATCH) throw new ApiError(400,'每批须包含 1～5 条 version 3 快照。');
  const records = payload.records.map(r=>record(r,now));
  if (new Set(records.map(r=>r.gid)).size!==records.length) {
    throw new ApiError(400,'同批作品 ID 不能重复。');
  }
  await quota(env,'upload:source:minute:'+source.id,now,60,1,CAPS.source_minute);
  await quota(env,'upload:global:day',now,86400,records.length,CAPS.global_daily_records);
  await quota(env,'upload:ip:day:'+ip,now,86400,records.length,CAPS.ip_daily_records);
  await quota(env,'upload:source:day:'+source.id,now,86400,records.length,
    source.status==='trusted'?CAPS.trusted_daily_records:CAPS.review_daily_records);
  const statements = [env.DB.prepare(`INSERT OR REPLACE INTO upload_guard VALUES(1,CASE WHEN ${AUTH} AND (SELECT pending_count FROM upload_controls WHERE id=1)+${records.length}<=${CAPS.pending} THEN 1 ELSE 0 END)`)
    .bind(source.id,source.collector_id)];
  for (const r of records) {
    const args = suspiciousArgs(r), raw=JSON.stringify(r);
    const review = `(EXISTS(SELECT 1 FROM upload_sources WHERE id=? AND status='review') OR ${SUSPICIOUS})`;
    statements.push(env.DB.prepare(`INSERT INTO cloud_pending(gid,payload,checked_at,source_id,reason,received_at)
      SELECT ?,?,?,?,?,? WHERE ${ELIGIBLE} AND ${review}
      AND ((SELECT pending_count FROM upload_controls WHERE id=1)<${CAPS.pending}
        OR EXISTS(SELECT 1 FROM cloud_pending WHERE source_id=? AND gid=?))
      ON CONFLICT(source_id,gid) DO UPDATE SET payload=excluded.payload,checked_at=excluded.checked_at,
        reason=excluded.reason,received_at=excluded.received_at WHERE excluded.checked_at>cloud_pending.checked_at
      RETURNING gid`)
      .bind(r.gid,raw,r.checked_at,source.id,source.status==='review'?'新来源待审':'异常变化',now,
        r.gid,r.checked_at,source.id,...args,source.id,r.gid));
    statements.push(env.DB.prepare(`INSERT INTO cloud_public(gid,payload,checked_at,source_id,action,received_at)
      SELECT ?,?,?,?,'snapshot',? WHERE ${ELIGIBLE} AND NOT ${review} ${UPSERT} RETURNING gid`)
      .bind(r.gid,raw,r.checked_at,source.id,now,r.gid,r.checked_at,source.id,...args));
    statements.push(env.DB.prepare(`DELETE FROM cloud_pending WHERE source_id=? AND gid=? AND checked_at<=?
      AND EXISTS(SELECT 1 FROM cloud_public WHERE gid=? AND checked_at>=?)`)
      .bind(source.id,r.gid,r.checked_at,r.gid,r.checked_at));
  }
  // 容量满时整批回滚，不能把未保存的候选记录作为 kept 确认给客户端。
  statements.push(env.DB.prepare(`INSERT OR REPLACE INTO upload_guard VALUES(1,CASE WHEN
    (SELECT pending_count FROM upload_controls WHERE id=1)<=${CAPS.pending} THEN 1 ELSE 0 END)`));
  const results=await env.DB.batch(statements);
  let accepted=0,pending=0;
  // D1 meta.changes can include history/counter trigger writes. Count only the submitted records.
  for(let i=1;i<results.length-1;i+=3){
    const candidates=results[i].results,published=results[i+1].results,gid=records[(i-1)/3].gid;
    if(!Array.isArray(candidates) || !Array.isArray(published) || candidates.length+published.length>1
        || [...candidates,...published].some(row=>row.gid!==gid)) {
      throw new ApiError(503,'上传确认结果暂不可用，请稍后重试；已保存的相同记录不会重复入库。');
    }
    pending+=candidates.length;accepted+=published.length;
  }
  return json({version:VERSION,accepted,pending,kept:records.length-accepted-pending,source_status:source.status});
}
function numberParam(url,key,defaultValue,low=0,high=Number.MAX_SAFE_INTEGER) {
  const values=url.searchParams.getAll(key);
  if(!values.length)return defaultValue;
  if(values.length!==1 || !/^(0|[1-9]\d*)$/.test(values[0]) || !integer(Number(values[0]),low,high)) throw new ApiError(400,'分页或记录参数不正确。');
  return Number(values[0]);
}
function event(row) {return {revision:row.revision,gid:row.gid,action:row.action,snapshot:row.payload?JSON.parse(row.payload):null};}
async function changes(url,env,info) {
  if([...url.searchParams.keys()].some(k=>!['after','until','limit','dataset_id'].includes(k)) || url.searchParams.getAll('dataset_id').length>1) throw new ApiError(400,'下载参数不正确。');
  const after=numberParam(url,'after',0),limit=numberParam(url,'limit',500,1,500);
  const head=(await env.DB.prepare('SELECT COALESCE(MAX(revision),0) AS head FROM cloud_history').first()).head;
  if(after>head || url.searchParams.get('dataset_id') && url.searchParams.get('dataset_id')!==info.dataset_id) {
    return json({error:'公共库已更换或恢复，请重新下载。',reset_required:true,dataset_id:info.dataset_id},409);
  }
  const until=numberParam(url,'until',head,after,head);
  const rows=(await env.DB.prepare('SELECT * FROM cloud_history WHERE revision>? AND revision<=? ORDER BY revision LIMIT ?')
    .bind(after,until,limit+1).all()).results;
  const more=rows.length>limit,records=rows.slice(0,limit).map(event);
  return json({format:FORMAT,version:VERSION,dataset_id:info.dataset_id,cursor:more?records.at(-1).revision:until,
    until,head,has_more:more,records});
}
async function audit(env,action,target,now) {await env.DB.prepare('INSERT INTO cloud_audit(action,target,created_at) VALUES(?,?,?)').bind(action,String(target),now).run();}
async function cleanup(env,now) {
  return await env.DB.prepare('DELETE FROM upload_limits WHERE window_start<?').bind(now-2*86400).run();
}
async function adminApi(request,url,env,now) {
  await admin(request,env);
  const path=url.pathname;
  if(path==='/v3/admin/cleanup' && request.method==='POST') {
    const r=await cleanup(env,now);await audit(env,'cleanup_limits','expired',now);return json({removed:r.meta.changes});
  }
  if(path==='/v3/admin/status' && request.method==='GET') {
    const c=await controls(env);
    const counts=await env.DB.prepare(`SELECT (SELECT count(*) FROM cloud_public WHERE payload IS NOT NULL) AS public_count,
      (SELECT count(*) FROM cloud_history) AS history_count,(SELECT count(*) FROM upload_sources WHERE status='blocked') AS blocked_count`).first();
    const limits=(await env.DB.prepare("SELECT key,used,window_start FROM upload_limits WHERE key IN ('upload:global:day','upload:global:minute','register:global:day')").all()).results;
    const actions=(await env.DB.prepare('SELECT action,target,created_at FROM cloud_audit ORDER BY id DESC LIMIT 30').all()).results;
    return json({...c,...counts,limits,caps:CAPS,actions});
  }
  if(path==='/v3/admin/controls' && request.method==='POST') {
    const v=await body(request);if(!exactKeys(v,['upload_enabled']) || typeof v.upload_enabled!=='boolean')throw new ApiError(400,'上传开关不正确。');
    await env.DB.prepare('UPDATE upload_controls SET upload_enabled=? WHERE id=1').bind(v.upload_enabled?1:0).run();
    await audit(env,v.upload_enabled?'enable_upload':'pause_upload','all',now);return json({upload_enabled:v.upload_enabled});
  }
  if(path==='/v3/admin/sources' && request.method==='GET') {
    const after=url.searchParams.get('after')||'';
    const rows=(await env.DB.prepare('SELECT id,collector_id,name,status,created_at FROM upload_sources WHERE id>? ORDER BY id LIMIT 101').bind(after).all()).results;
    return json({sources:rows.slice(0,100),has_more:rows.length>100,next:rows.length?rows[Math.min(rows.length,100)-1].id:''});
  }
  const source=/^\/v3\/admin\/sources\/([0-9a-f-]{36})$/.exec(path);
  if(source && UUID.test(source[1]) && request.method==='POST') {
    const v=await body(request);if(!exactKeys(v,['status']) || !['review','trusted','blocked'].includes(v.status))throw new ApiError(400,'来源状态不正确。');
    const current=await env.DB.prepare('SELECT status FROM upload_sources WHERE id=?').bind(source[1]).first();
    if(!current)throw new ApiError(404,'来源不存在。');
    const r=await env.DB.prepare('UPDATE upload_sources SET status=? WHERE id=? AND status<>?').bind(v.status,source[1],v.status).run();
    if(r.meta.changes)await audit(env,'source_'+v.status,source[1],now);
    return json({updated:Boolean(r.meta.changes),status:v.status});
  }
  if(path==='/v3/admin/pending' && request.method==='GET') {
    const rows=(await env.DB.prepare(`SELECT p.*,s.name,s.status FROM cloud_pending p JOIN upload_sources s ON s.id=p.source_id
      ORDER BY p.received_at,p.id LIMIT 100`).all()).results;
    return json({pending:rows.map(r=>({...r,snapshot:JSON.parse(r.payload),payload:undefined}))});
  }
  const review=/^\/v3\/admin\/pending\/([1-9]\d*)\/(approve|reject)$/.exec(path);
  if(review && Number.isSafeInteger(Number(review[1])) && request.method==='POST') {
    const id=Number(review[1]);
    if(review[2]==='reject') {
      const r=await env.DB.prepare('DELETE FROM cloud_pending WHERE id=?').bind(id).run();
      if(!r.meta.changes)throw new ApiError(404,'记录已处理。');
    } else {
      const results=await env.DB.batch([
        env.DB.prepare(`INSERT INTO cloud_public(gid,payload,checked_at,source_id,action,received_at)
          SELECT p.gid,p.payload,p.checked_at,p.source_id,'snapshot',? FROM cloud_pending p JOIN upload_sources s ON s.id=p.source_id
          WHERE p.id=? AND s.status<>'blocked' AND NOT EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=p.gid AND c.checked_at>=p.checked_at) ${UPSERT}`).bind(now,id),
        env.DB.prepare(`DELETE FROM cloud_pending WHERE id=? AND EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=cloud_pending.gid AND c.checked_at>=cloud_pending.checked_at)`).bind(id)
      ]);
      if(!results[0].meta.changes)throw new ApiError(409,'来源已封禁或记录已过时；未覆盖公共数据。');
    }
    await audit(env,'pending_'+review[2],id,now);return json({processed:true});
  }
  if(path==='/v3/admin/history' && request.method==='GET') {
    const gid=numberParam(url,'gid',null,1);if(gid===null)throw new ApiError(400,'请填写作品 ID。');
    const before=numberParam(url,'before',Number.MAX_SAFE_INTEGER,1);
    const rows=(await env.DB.prepare('SELECT * FROM cloud_history WHERE gid=? AND revision<? ORDER BY revision DESC LIMIT 101').bind(gid,before).all()).results;
    const current=await env.DB.prepare('SELECT revision FROM cloud_public WHERE gid=?').bind(gid).first();
    return json({current_revision:current?.revision||0,history:rows.slice(0,100).map(r=>({...event(r),source_id:r.source_id,received_at:r.received_at})),has_more:rows.length>100});
  }
  if(path==='/v3/admin/rollback-preview' && request.method==='GET') {
    const source=url.searchParams.get('source_id');if(!source || !UUID.test(source))throw new ApiError(400,'来源编号不正确。');
    const after=numberParam(url,'after',0),from=numberParam(url,'from',0),until=numberParam(url,'until',now,from);
    const rows=(await env.DB.prepare(`SELECT c.gid,c.revision AS expected_revision,c.payload,
      COALESCE((SELECT h.revision FROM cloud_history h LEFT JOIN upload_sources s ON s.id=h.source_id
        WHERE h.gid=c.gid AND h.revision<c.revision AND h.source_id<>?
          AND (s.status IS NULL OR s.status<>'blocked') ORDER BY h.revision DESC LIMIT 1),0) AS restore_revision
      FROM cloud_public c WHERE c.source_id=? AND c.payload IS NOT NULL AND c.gid>?
        AND c.received_at>=? AND c.received_at<=? ORDER BY c.gid LIMIT 6`).bind(source,source,after,from,until).all()).results;
    return json({records:rows.slice(0,5).map(r=>({gid:r.gid,expected_revision:r.expected_revision,
      revision:r.restore_revision,current_snapshot:JSON.parse(r.payload)})),has_more:rows.length>5,next:rows.length?rows[Math.min(rows.length,5)-1].gid:after});
  }
  if(path==='/v3/admin/restore' && request.method==='POST') {
    const v=await body(request);
    if(!exactKeys(v,['gid','revision','expected_revision']) || !integer(v.gid,1,Number.MAX_SAFE_INTEGER)
      || !integer(v.revision,0,Number.MAX_SAFE_INTEGER) || !integer(v.expected_revision,1,Number.MAX_SAFE_INTEGER))throw new ApiError(400,'恢复参数不正确。');
    const selected=v.revision?await env.DB.prepare('SELECT * FROM cloud_history WHERE gid=? AND revision=?').bind(v.gid,v.revision).first():null;
    if(v.revision && !selected)throw new ApiError(404,'历史记录不存在。');
    const payload=selected?.payload||null,checked=payload?JSON.parse(payload).checked_at:0;
    const r=await env.DB.prepare(`UPDATE cloud_public SET payload=?,checked_at=?,source_id=?,action='correction',received_at=?
      WHERE gid=? AND revision=?`).bind(payload,checked,selected?.source_id||'admin',now,v.gid,v.expected_revision).run();
    if(!r.meta.changes)throw new ApiError(409,'公共记录已变化，请刷新后重新选择恢复。');
    await audit(env,'restore',v.gid+':'+v.revision,now);return json({restored:true});
  }
  throw new ApiError(404,'管理接口不存在。');
}

const ADMIN_HTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ExCatalog 同步管理</title><style nonce="NONCE">body{font:16px system-ui;background:#f5f7fb;color:#18243b;max-width:1000px;margin:30px auto;padding:0 20px}section{background:white;border:1px solid #dce3ee;border-radius:12px;padding:20px;margin:20px 0}input,button{font:inherit;padding:10px;border:1px solid #bcc9dd;border-radius:7px;margin:5px 4px 5px 0;max-width:100%;box-sizing:border-box}button{cursor:pointer;background:#eaf0ff}.row{padding:12px 0;border-bottom:1px solid #e2e6ed;overflow-wrap:anywhere}.source-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.source-name{font-weight:600}.status-badge{display:inline-block;font-weight:600;border:1px solid;border-radius:999px;padding:4px 12px}.status-trusted{color:#166534;background:#ecfdf3;border-color:#86d4a2}.status-review{color:#92400e;background:#fff7e7;border-color:#e8bd6a}.status-blocked{color:#b91c1c;background:#fff0f0;border-color:#efa2a2}button:disabled{cursor:default;color:#64748b;background:#edf1f6;border-color:#d5dce6}p{line-height:1.6}#status{white-space:pre-wrap}</style>
<h1>收藏数同步服务管理</h1><p>所有人可提交；新来源待审核，可信来源正常变化自动入库，异常变化仍待审。密钥仅保留在本页输入框。</p>
<section><label for="secret">管理员密钥 ADMIN_SECRET</label><br><input id="secret" type="password" autocomplete="off" size="45"><button id="load">读取管理信息</button><button id="make-secret">本地生成新管理员密钥</button><button id="copy-secret">复制密钥</button><p id="status" role="status"></p></section>
<section><h2>上传开关与用量</h2><p id="metrics"></p><button id="enable">开放上传</button><button id="pause">暂停上传</button><button id="cleanup">清理过期限流计数</button><p>升级后默认暂停。暂停只阻止新上传，公开下载继续可用。备份请使用 Cloudflare D1 导出；历史记录随数据库一起备份。</p><div id="audit"></div></section>
<section><h2>上传来源</h2><p>来源标识显示「已可信」「待审核」或「已封禁」；对应的当前状态按钮不可重复点击。新来源可提交到服务器待审核区，批准后才进入公共库；设为可信后，后续正常数据自动入库，已有候选仍需批准。请核对来源编号再设为可信；封禁不撤回已发布数据。</p><div id="sources"></div><button id="source-next">下一页来源</button><button id="source-first">返回首批来源</button></section>
<section><h2>待审核数据</h2><p>核对源站后批准。设为可信不自动批准已有候选；每次显示最早 100 条，处理后继续读取。</p><div id="pending"></div></section>
<section><h2>按来源和时间范围撤回</h2><input id="rollback-source" placeholder="上传来源编号"><label>起始时间<input id="rollback-from" type="datetime-local"></label><label>结束时间<input id="rollback-until" type="datetime-local"></label><button id="rollback-preview">预览受影响作品</button><button id="rollback-next">预览下一批</button><button id="rollback-apply">撤回本批</button><p>时间采用本机时区，留空表示不限。每批最多 5 部作品，恢复为此前其他未封禁来源的最后一份历史快照；没有历史时撤回。更新的其他来源记录不会被批量撤回。请先封禁错误来源，再预览并核对。</p><div id="rollback"></div></section>
<section><h2>历史恢复与撤回</h2><input id="gid" type="number" min="1" placeholder="作品 ID"><button id="history-load">读取历史</button><button id="history-next">更早的历史</button><button id="retract">撤回当前公共快照</button><p>恢复会发布纠正记录，已升级客户端可撤回错误云端值并保留更新的自采数据。升级前仅能保留迁移时的最新快照。</p><div id="history"></div></section>
<script nonce="NONCE">
const el=id=>document.getElementById(id);let busy=false,sourceAfter='',sourceNext='',historyBefore='',current=0,rollbackRows=[],rollbackNext=0,rollbackAfter=0,rollbackMore=false,rollbackFilter='';
async function call(path,method='GET',data){const secret=el('secret').value;if(!secret)throw Error('请填写管理员密钥。');const r=await fetch(path,{method,headers:{Authorization:'Bearer '+secret,...(data?{'Content-Type':'application/json'}:{})},...(data?{body:JSON.stringify(data)}:{})});const v=await r.json();if(!r.ok)throw Error(v.error||'操作失败');return v;}
function controls(){document.querySelectorAll('button').forEach(b=>b.disabled=busy||b.dataset.stateDisabled==='true');}
async function task(fn){if(busy)return;busy=true;controls();el('status').textContent='处理中…';try{await fn();el('status').textContent='完成。';}catch(e){el('status').textContent=e.message;}finally{busy=false;controls();}}
function text(parent,value){const p=document.createElement('p');p.textContent=value;parent.append(p);}
function button(parent,label,fn,disabled=false){const b=document.createElement('button');b.textContent=label;b.dataset.stateDisabled=String(disabled);b.disabled=busy||disabled;b.onclick=()=>task(fn);parent.append(b);return b;}
const sourceLabels={trusted:'已可信',review:'待审核',blocked:'已封禁'};
const sourceHints={trusted:'后续正常快照自动进入公共库；异常变化仍待审核。',review:'数据可提交到服务器待审核区，管理员批准后才公开共享。',blocked:'已停止该来源后续上传；已发布数据可另行恢复或撤回。'};
const actionLabels={enable_upload:'开放上传',pause_upload:'暂停上传',source_trusted:'来源设为可信',source_review:'来源改为待审核',source_blocked:'来源封禁',pending_approve:'批准待审数据',pending_reject:'拒绝待审数据',restore:'恢复或撤回公共快照',cleanup_limits:'清理过期限流计数'};
async function refresh(){const [m,s,p]=await Promise.all([call('/v3/admin/status'),call('/v3/admin/sources?after='+encodeURIComponent(sourceAfter)),call('/v3/admin/pending')]);el('enable').dataset.stateDisabled=String(Boolean(m.upload_enabled));el('pause').dataset.stateDisabled=String(!m.upload_enabled);el('metrics').textContent=(m.upload_enabled?'上传开放':'上传暂停')+'；公共作品 '+m.public_count+'；候选 '+m.pending_count+'/'+m.caps.pending+'；历史 '+m.history_count+'；来源 '+m.source_count+'；封禁 '+m.blocked_count+'。';for(const q of m.limits)text(el('metrics'),q.key+'：'+q.used+'（窗口起点 '+new Date(q.window_start*1000).toLocaleString()+'）');el('audit').replaceChildren();for(const a of m.actions)text(el('audit'),(actionLabels[a.action]||a.action)+' · '+a.target+' · '+new Date(a.created_at*1000).toLocaleString());el('sources').replaceChildren();sourceNext=s.has_more?s.next:'';for(const v of s.sources){const row=document.createElement('div');row.className='row';const heading=document.createElement('div');heading.className='source-heading';const name=document.createElement('span');name.className='source-name';name.textContent=v.name||'无昵称';const badge=document.createElement('span');badge.className='status-badge status-'+v.status;badge.textContent=sourceLabels[v.status]||'未知状态';heading.append(name,badge);row.append(heading);text(row,'来源编号 '+v.id+' · 采集者 '+v.collector_id);text(row,sourceHints[v.status]||'请重新读取来源状态。');for(const [status,label,currentLabel] of [['trusted','设为可信','已设为可信'],['review','恢复待审核','当前待审核'],['blocked','封禁上传','已封禁上传']])button(row,status===v.status?currentLabel:label,async()=>{if(!confirm('将此来源改为 '+label+'？'))return;await call('/v3/admin/sources/'+v.id,'POST',{status});await refresh();},status===v.status);button(row,'预览该来源的撤回',async()=>{el('rollback-source').value=v.id;rollbackAfter=0;await rollbackPreview();});el('sources').append(row);}if(!s.sources.length)text(el('sources'),'暂无来源。');el('pending').replaceChildren();for(const v of p.pending){const row=document.createElement('div');row.className='row';const r=v.snapshot;text(row,'作品 '+v.gid+' · '+v.reason+' · '+(sourceLabels[v.status]||'未知状态')+' · 来源编号 '+v.source_id);text(row,'收藏 '+r.favorite_count+'；评分人数 '+(r.rating_count??'未知')+'；'+new Date(r.checked_at*1000).toLocaleString());if(v.status!=='blocked')button(row,'核对后批准',async()=>{if(!confirm('已核对源站，批准此快照？'))return;await call('/v3/admin/pending/'+v.id+'/approve','POST');await refresh();});button(row,'拒绝',async()=>{if(!confirm('拒绝此候选？'))return;await call('/v3/admin/pending/'+v.id+'/reject','POST');await refresh();});el('pending').append(row);}if(!p.pending.length)text(el('pending'),'没有待审数据。');controls();}
async function history(){const gid=Number(el('gid').value);const v=await call('/v3/admin/history?gid='+gid+(historyBefore?'&before='+historyBefore:''));current=v.current_revision;historyBefore=v.has_more?v.history.at(-1).revision:'';el('history').replaceChildren();for(const h of v.history){const row=document.createElement('div');row.className='row';text(row,'修订 '+h.revision+' · '+h.action+' · '+(h.snapshot?'收藏 '+h.snapshot.favorite_count+'；评分 '+(h.snapshot.rating_count??'未知'):'已撤回')+' · 来源 '+h.source_id);button(row,'恢复此版本',async()=>{if(!confirm('恢复作品 '+gid+' 到修订 '+h.revision+'？'))return;await call('/v3/admin/restore','POST',{gid,revision:h.revision,expected_revision:current});historyBefore='';await history();await refresh();});el('history').append(row);}}
async function rollbackPreview(){const params=new URLSearchParams({source_id:el('rollback-source').value.trim(),after:String(rollbackAfter)});for(const [id,key] of [['rollback-from','from'],['rollback-until','until']]){if(el(id).value)params.set(key,String(Math.floor(new Date(el(id).value).getTime()/1000)));}const v=await call('/v3/admin/rollback-preview?'+params);rollbackRows=v.records;rollbackNext=v.next;rollbackMore=v.has_more;rollbackFilter=[el('rollback-source').value,el('rollback-from').value,el('rollback-until').value].join('|');el('rollback').replaceChildren();for(const r of rollbackRows)text(el('rollback'),'作品 '+r.gid+' · 当前收藏 '+r.current_snapshot.favorite_count+' · 恢复修订 '+r.revision+(r.revision?'':'（撤回公共值）'));if(!rollbackRows.length)text(el('rollback'),'没有匹配作品。');}
el('rollback-preview').onclick=()=>task(async()=>{rollbackAfter=0;await rollbackPreview();});el('rollback-next').onclick=()=>task(async()=>{if(!rollbackMore)throw Error('没有后续作品。');rollbackAfter=rollbackNext;await rollbackPreview();});el('rollback-apply').onclick=()=>task(async()=>{if(!rollbackRows.length)throw Error('请先预览。');if(rollbackFilter!==[el('rollback-source').value,el('rollback-from').value,el('rollback-until').value].join('|'))throw Error('筛选条件已变化，请重新预览。');if(!confirm('已核对，撤回本批 '+rollbackRows.length+' 部作品？'))return;const rows=rollbackRows;rollbackRows=[];let ok=0,failed=0;for(const r of rows){try{await call('/v3/admin/restore','POST',{gid:r.gid,revision:r.revision,expected_revision:r.expected_revision});ok++;}catch{failed++;}}await rollbackPreview();await refresh();text(el('rollback'),'本次成功 '+ok+' 部，未成功 '+failed+' 部；未成功的作品需重新预览核对。');});
el('cleanup').onclick=()=>task(async()=>{await call('/v3/admin/cleanup','POST',{});await refresh();});el('load').onclick=()=>task(refresh);el('source-next').onclick=()=>task(async()=>{if(!sourceNext)throw Error('已到最后一页。');sourceAfter=sourceNext;await refresh();});el('source-first').onclick=()=>task(async()=>{sourceAfter='';await refresh();});for(const [id,value] of [['enable',true],['pause',false]])el(id).onclick=()=>task(async()=>{if(!confirm(value?'开放所有人的提交？新来源仍待审核。':'暂停所有上传？'))return;await call('/v3/admin/controls','POST',{upload_enabled:value});await refresh();});el('history-load').onclick=()=>task(async()=>{historyBefore='';await history();});el('history-next').onclick=()=>task(async()=>{if(!historyBefore)throw Error('没有更早历史。');await history();});el('retract').onclick=()=>task(async()=>{if(!confirm('撤回当前公共快照？'))return;await call('/v3/admin/restore','POST',{gid:Number(el('gid').value),revision:0,expected_revision:current});historyBefore='';await history();await refresh();});
el('make-secret').onclick=()=>{if(el('secret').value&&!confirm('生成新密钥？不会修改 Cloudflare 已保存的密钥。'))return;const bytes=crypto.getRandomValues(new Uint8Array(48));el('secret').value=btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');el('status').textContent='请复制到 Cloudflare 的 ADMIN_SECRET 加密 Secret，并自行保管。刷新页面会清空。';};el('copy-secret').onclick=()=>task(async()=>{if(!el('secret').value)throw Error('请先输入或生成密钥。');await navigator.clipboard.writeText(el('secret').value);});
</script></html>`;
export default {async fetch(request,env){try{
  const url=new URL(request.url);
  if(url.pathname==='/admin' && request.method==='GET'){
    const nonce=crypto.randomUUID();return new Response(ADMIN_HTML.replaceAll('NONCE',nonce),{headers:{...SAFE_HEADERS,
      'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':`default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`}});
  }
  const info=await meta(env),now=Math.floor(Date.now()/1000);
  if(['/','/health'].includes(url.pathname) && request.method==='GET')return json({service:FORMAT,version:VERSION,ready:true,
    dataset_id:info.dataset_id,read_public:true,upload_scope:'library',preserves_original_collector:true,
    upload_confirmation:'returning_rows',
    upload_requires_contribution_code:false,new_source_requires_review:true,
    upload_enabled:Boolean((await controls(env)).upload_enabled),admin_configured:typeof env.ADMIN_SECRET==='string' && /^[A-Za-z0-9_-]{32,256}$/.test(env.ADMIN_SECRET)});
  if(url.pathname==='/v3/changes' && request.method==='GET')return await changes(url,env,info);
  if(url.pathname==='/v3/register' && request.method==='POST')return await register(request,env,now);
  if(url.pathname==='/v3/contribute' && request.method==='POST')return await contribute(request,env,now);
  if(url.pathname.startsWith('/v3/admin/'))return await adminApi(request,url,env,now);
  if(url.pathname.startsWith('/v1/') || url.pathname.startsWith('/v2/'))throw new ApiError(426,'同步协议已升级，请安装 v1.1.19 或更新的客户端。');
  throw new ApiError(404,'接口不存在或方法不正确。');
}catch(e){return json({error:e instanceof ApiError?e.message:'同步暂时不可用、候选容量已满或上传状态变化，请稍后重试。'},e instanceof ApiError?e.status:503);}},
async scheduled(controller,env){await cleanup(env,Math.floor(Date.now()/1000));}};

