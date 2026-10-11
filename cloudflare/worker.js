/* ExCatalog 公共同步 v3。绑定 D1 为 DB；管理员密钥只存于 ADMIN_SECRET Secret。 */
const VERSION = 3;
const FORMAT = "excatalog-cloud-sync";
// 使用集合 SQL，每批 50 条的日额度与数据一同提交，保留免费计划的查询余量。
const MAX_BATCH = 50;
const MAX_ADMIN_BATCH = 5;
const MAX_BULK_REVIEW_BATCH = 50;
const MAX_UPLOAD_CHECK_BATCH = 500;
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
  constructor(status, message, headers = {}) { super(message); this.status = status; this.headers = headers; }
}
function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {status, headers: {...SAFE_HEADERS, ...headers, "Content-Type": "application/json; charset=utf-8"}});
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
const POLICY_SQL = `CREATE TABLE IF NOT EXISTS upload_policy (
  id INTEGER PRIMARY KEY CHECK(id=1),trusted_daily_records INTEGER NOT NULL DEFAULT 5000,
  ip_daily_records INTEGER NOT NULL DEFAULT 10000,global_daily_records INTEGER NOT NULL DEFAULT 50000,
  upload_min_interval_days INTEGER NOT NULL DEFAULT 14 CHECK(upload_min_interval_days BETWEEN 0 AND 365))`;
const policyReady = new WeakMap();
async function controls(env) {
  if (!policyReady.has(env.DB)) {
    const pending = (async()=>{
      await env.DB.prepare(POLICY_SQL).run();
      const columns=await env.DB.prepare('PRAGMA table_info(upload_policy)').all();
      if(!columns.results.some(column=>column.name==='upload_min_interval_days')){
        try{await env.DB.prepare('ALTER TABLE upload_policy ADD COLUMN upload_min_interval_days INTEGER NOT NULL DEFAULT 14 CHECK(upload_min_interval_days BETWEEN 0 AND 365)').run();}
        catch(error){
          // Another isolate may have completed the same migration.
          const updated=await env.DB.prepare('PRAGMA table_info(upload_policy)').all();
          if(!updated.results.some(column=>column.name==='upload_min_interval_days'))throw error;
        }
      }
    })().catch(error=>{policyReady.delete(env.DB);throw error;});
    policyReady.set(env.DB,pending);
  }
  await policyReady.get(env.DB);
  return await env.DB.prepare(`SELECT c.*,COALESCE(p.trusted_daily_records,5000) AS trusted_daily_records,
    COALESCE(p.ip_daily_records,10000) AS ip_daily_records,COALESCE(p.global_daily_records,50000) AS global_daily_records,
    COALESCE(p.upload_min_interval_days,14) AS upload_min_interval_days
    FROM upload_controls c LEFT JOIN upload_policy p ON p.id=c.id WHERE c.id=1`).first();
}
async function quota(env, key, now, seconds, units, cap) {
  const start = Math.floor(now/seconds)*seconds;
  const row = await env.DB.prepare(`INSERT INTO upload_limits(key,window_start,used) VALUES(?,?,?)
    ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,
      used=CASE WHEN upload_limits.window_start<>excluded.window_start THEN excluded.used ELSE upload_limits.used+excluded.used END
    WHERE upload_limits.window_start<>excluded.window_start OR upload_limits.used+excluded.used<=?
    RETURNING used`).bind(key,start,units,cap).first();
  if (!row) {
    const current = await env.DB.prepare('SELECT used FROM upload_limits WHERE key=? AND window_start=?').bind(key,start).first();
    throw new ApiError(429,'上传频率或今日额度已达上限，已保留队列。',{
      'Retry-After':String(Math.max(1,start+seconds-now)),
      'X-Upload-Limit-Scope':seconds===86400?'day':'minute',
      'X-Upload-Remaining':String(Math.max(0,cap-(current?.used||0)))});
  }
}
async function uploadGate(request, env, now, registration=false) {
  const policy = await controls(env);
  if (!policy.upload_enabled) throw new ApiError(503,'管理员已暂停上传；公共下载仍可使用。');
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
  return {ip,policy};
}
async function register(request, env, now) {
  await uploadGate(request,env,now,true);
  const token = bearer(request);
  if (!SOURCE_TOKEN.test(token)) throw new ApiError(401,'自动上传凭证格式不正确，请升级客户端。');
  const value = await body(request);
  const named = Object.hasOwn(value || {}, 'uploader_name');
  if (!exactKeys(value,named?['collector_id','collector_name','uploader_name']:['collector_id','collector_name'])) throw new ApiError(400,'登记字段不正确。');
  if (named && !boundedName(value.uploader_name)) throw new ApiError(400,'上传者昵称最多 40 个字符，不能包含首尾空白、换行或控制字符。');
  record({gid:1,favorite_count:0,rating_count:null,checked_at:now,source:'e-hentai.org',
    collector_id:value.collector_id,collector_name:value.collector_name},now);
  if (!value.collector_id) throw new ApiError(400,'上传来源须登记有效的采集者 ID；记录中的未知采集者可保留为空。');
  const hash = await digest(token);
  await env.DB.prepare(`INSERT INTO upload_sources(id,token_hash,collector_id,name,created_at)
    SELECT ?,?,?,?,? WHERE (SELECT source_count FROM upload_controls WHERE id=1)<${CAPS.sources}
    AND (SELECT upload_enabled FROM upload_controls WHERE id=1)=1 ON CONFLICT(token_hash) DO NOTHING`)
    .bind(crypto.randomUUID(),hash,value.collector_id,named?value.uploader_name:value.collector_name,now).run();
  const source = await env.DB.prepare('SELECT id,collector_id,status FROM upload_sources WHERE token_hash=?').bind(hash).first();
  if (!source) throw new ApiError(429,'上传来源容量已满或上传已暂停。');
  if (source.collector_id!==value.collector_id) throw new ApiError(401,'上传凭证身份不匹配。');
  if (source.status==='blocked') throw new ApiError(403,'该上传来源已封禁。');
  if (named) {
    const updated = await env.DB.prepare(`UPDATE upload_sources SET name=? WHERE id=? AND token_hash=?
      AND collector_id=? AND status<>'blocked' RETURNING status`)
      .bind(value.uploader_name,source.id,hash,value.collector_id).first();
    if (!updated) throw new ApiError(403,'该上传来源已封禁。');
    source.status = updated.status;
  }
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
function skipPublic(checkedAt,days){
  return `(p.payload IS NOT NULL AND (p.checked_at>=${checkedAt} OR ${checkedAt}-p.checked_at<${days*86400}))`;
}
async function uploadCheck(request,env,now){
  const {policy}=await uploadGate(request,env,now);
  const token=bearer(request);if(!SOURCE_TOKEN.test(token))throw new ApiError(401,'上传凭证格式不正确。');
  const source=await env.DB.prepare('SELECT id,collector_id,status FROM upload_sources WHERE token_hash=?').bind(await digest(token)).first();
  if(!source)throw new ApiError(401,'上传身份尚未登记。');
  if(source.status==='blocked')throw new ApiError(403,'该上传来源已封禁。');
  const v=await body(request);
  if(!exactKeys(v,['version','records'])||v.version!==VERSION||!Array.isArray(v.records)||!v.records.length
    || v.records.length>MAX_UPLOAD_CHECK_BATCH||v.records.some(r=>!exactKeys(r,['gid','checked_at'])
      || !integer(r.gid,1,Number.MAX_SAFE_INTEGER)||!integer(r.checked_at,1,now+300))
    || new Set(v.records.map(r=>r.gid)).size!==v.records.length)throw new ApiError(400,'每批差异检查须含 1～500 条作品 ID 与采集时间。');
  await quota(env,'check:source:minute:'+source.id,now,60,1,30);
  const rows=(await env.DB.prepare(`SELECT json_extract(j.value,'$.gid') AS gid,
    CASE WHEN ${skipPublic("json_extract(j.value,'$.checked_at')",policy.upload_min_interval_days)} THEN 1 ELSE 0 END AS skip,
    COALESCE(p.revision,0) AS revision,m.dataset_id,s.status AS source_status
    FROM json_each(?) j CROSS JOIN sync_meta m CROSS JOIN upload_sources s
    LEFT JOIN cloud_public p ON p.gid=json_extract(j.value,'$.gid')
    WHERE m.id=1 AND s.id=? AND s.collector_id=? AND s.status<>'blocked'`)
    .bind(JSON.stringify(v.records),source.id,source.collector_id).all()).results;
  if(rows.length!==v.records.length)throw new ApiError(403,'来源已封禁或公共库正在变化，请重新检查。');
  return json({version:VERSION,dataset_id:rows[0].dataset_id,source_id:source.id,status:rows[0].source_status,
    records:rows.map(r=>({gid:r.gid,skip:Boolean(r.skip),revision:r.revision}))});
}
async function uploadState(url,env){
  if([...url.searchParams.keys()].some(k=>!['after','until','dataset_id'].includes(k))
    || url.searchParams.getAll('dataset_id').length>1)throw new ApiError(400,'公共校验参数不正确。');
  const after=numberParam(url,'after',0),dataset=url.searchParams.get('dataset_id')||'';
  if(dataset&&!/^[0-9a-f]{32}$/.test(dataset))throw new ApiError(400,'公共库编号不正确。');
  const state=await env.DB.prepare(`SELECT dataset_id,(SELECT COALESCE(MAX(revision),0) FROM cloud_history) AS head
    FROM sync_meta WHERE id=1`).first();
  if(!dataset||dataset!==state.dataset_id||after>state.head)
    return json({version:VERSION,dataset_id:state.dataset_id,reset:true,cursor:state.head,until:state.head,has_more:false,corrections:[]});
  const until=numberParam(url,'until',state.head,after,state.head);
  const rows=(await env.DB.prepare(`SELECT gid,revision FROM cloud_history
    WHERE action='correction' AND revision>? AND revision<=? ORDER BY revision LIMIT 501`).bind(after,until).all()).results;
  const corrections=rows.slice(0,500),more=rows.length>500;
  return json({version:VERSION,dataset_id:state.dataset_id,reset:false,cursor:more?corrections.at(-1).revision:until,
    until,has_more:more,corrections});
}
async function contribute(request, env, now) {
  const {ip,policy} = await uploadGate(request,env,now);
  const token = bearer(request);
  if (!SOURCE_TOKEN.test(token)) throw new ApiError(401,'请由新版客户端自动登记上传身份。');
  const source = await env.DB.prepare('SELECT id,collector_id,status FROM upload_sources WHERE token_hash=?')
    .bind(await digest(token)).first();
  if (!source) throw new ApiError(401,'上传身份尚未登记。');
  if (source.status==='blocked') throw new ApiError(403,'该上传来源已封禁。');
  const payload = await body(request);
  if (!exactKeys(payload,['version','records']) || payload.version!==VERSION || !Array.isArray(payload.records)
      || !payload.records.length || payload.records.length>MAX_BATCH) throw new ApiError(400,'每批须包含 1～50 条 version 3 快照。');
  const records = payload.records.map(r=>record(r,now));
  if (new Set(records.map(r=>r.gid)).size!==records.length) {
    throw new ApiError(400,'同批作品 ID 不能重复。');
  }
  await quota(env,'upload:source:minute:'+source.id,now,60,1,CAPS.source_minute);
  const raw = JSON.stringify(records);
  const incoming = `WITH incoming AS (SELECT value AS payload,
    json_extract(value,'$.gid') AS gid,json_extract(value,'$.checked_at') AS checked_at,
    json_extract(value,'$.favorite_count') AS favorite_count,json_extract(value,'$.rating_count') AS rating_count
    FROM json_each(?)) `;
  const oldFavorite = "json_extract(p.payload,'$.favorite_count')";
  const oldRating = "json_extract(p.payload,'$.rating_count')";
  const anomaly = (next,old)=>`(${next}>max(${old}*5+1000,${old}+10000)
    OR (${old}>=100 AND ${next}<${old}*0.5 AND ${old}-${next}>=100)
    OR (${next}=0 AND ${old}>0))`;
  const suspicious = `(r.favorite_count>1000000 OR (r.rating_count IS NOT NULL AND r.rating_count>1000000)
    OR (p.payload IS NOT NULL AND (${anomaly('r.favorite_count',oldFavorite)}
      OR (${oldRating} IS NOT NULL AND (r.rating_count IS NULL OR ${anomaly('r.rating_count',oldRating)})))))`;
  const review = `(s.status='review' OR ${suspicious}
    OR (p.gid IS NOT NULL AND p.action='correction' AND r.checked_at<=p.received_at))`;
  const eligible = `(p.gid IS NULL OR p.checked_at<r.checked_at)`;
  const from = `FROM incoming r LEFT JOIN cloud_public p ON p.gid=r.gid
    JOIN upload_sources s ON s.id=? AND s.collector_id=? AND s.status<>'blocked'`;
  const skip=skipPublic('r.checked_at',policy.upload_min_interval_days);
  const start=Math.floor(now/86400)*86400;
  const dailyLimits=[['upload:global:day',policy.global_daily_records],['upload:ip:day:'+ip,policy.ip_daily_records],
    ['upload:source:day:'+source.id,source.status==='trusted'?policy.trusted_daily_records:CAPS.review_daily_records]];
  // Count against the current public snapshot inside the write transaction, including
  // legacy clients and a second uploader that passed preflight before the first one.
  const dailyStatements=dailyLimits.map(([key])=>env.DB.prepare(incoming+`INSERT INTO upload_limits(key,window_start,used)
    SELECT ?,?,count(*) FROM incoming r LEFT JOIN cloud_public p ON p.gid=r.gid WHERE NOT ${skip} HAVING count(*)>0
    ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,
      used=CASE WHEN upload_limits.window_start<>excluded.window_start THEN excluded.used ELSE upload_limits.used+excluded.used END`)
    .bind(raw,key,start));
  const dailyGuard=dailyLimits.map(()=>`NOT EXISTS(SELECT 1 FROM upload_limits WHERE key=? AND window_start=? AND used>?)`).join(' AND ');
  const statements = [
    env.DB.prepare(`INSERT OR REPLACE INTO upload_guard VALUES(1,CASE WHEN ${AUTH} THEN 1 ELSE 0 END)`)
      .bind(source.id,source.collector_id),
    ...dailyStatements,
    env.DB.prepare(incoming+`INSERT OR REPLACE INTO upload_guard VALUES(1,CASE WHEN
      NOT EXISTS(SELECT 1 FROM incoming r LEFT JOIN cloud_public p ON p.gid=r.gid WHERE NOT ${skip})
      OR (${dailyGuard}) THEN 1 ELSE 0 END)`)
      .bind(raw,...dailyLimits.flatMap(([key,cap])=>[key,start,cap])),
    env.DB.prepare(incoming+`INSERT INTO cloud_pending(gid,payload,checked_at,source_id,reason,received_at)
      SELECT r.gid,r.payload,r.checked_at,s.id,CASE WHEN p.action='correction' AND r.checked_at<=p.received_at
        THEN '恢复或撤回后的旧数据' WHEN s.status='review' THEN '新来源待审' ELSE '异常变化' END,?
      ${from} WHERE ${eligible} AND NOT ${skip} AND ${review}
      ON CONFLICT(source_id,gid) DO UPDATE SET payload=excluded.payload,checked_at=excluded.checked_at,
        reason=excluded.reason,received_at=excluded.received_at WHERE excluded.checked_at>cloud_pending.checked_at
      RETURNING gid`).bind(raw,now,source.id,source.collector_id),
    env.DB.prepare(incoming+`INSERT INTO cloud_public(gid,payload,checked_at,source_id,action,received_at)
      SELECT r.gid,r.payload,r.checked_at,s.id,'snapshot',? ${from}
      WHERE ${eligible} AND NOT ${skip} AND NOT ${review} ${UPSERT} RETURNING gid`)
      .bind(raw,now,source.id,source.collector_id),
    env.DB.prepare(`DELETE FROM cloud_pending WHERE source_id=? AND EXISTS(
      SELECT 1 FROM json_each(?) j JOIN cloud_public p ON p.gid=json_extract(j.value,'$.gid')
      WHERE cloud_pending.gid=p.gid AND cloud_pending.checked_at<=json_extract(j.value,'$.checked_at')
      AND p.checked_at>=json_extract(j.value,'$.checked_at'))`).bind(source.id,raw),
    env.DB.prepare(`INSERT OR REPLACE INTO upload_guard VALUES(1,CASE WHEN
      (SELECT pending_count FROM upload_controls WHERE id=1)<=${CAPS.pending} THEN 1 ELSE 0 END)`),
    env.DB.prepare(incoming+`SELECT r.gid,p.revision,m.dataset_id FROM incoming r
      LEFT JOIN cloud_public p ON p.gid=r.gid CROSS JOIN sync_meta m WHERE m.id=1 AND ${skip}`).bind(raw)
  ];
  let results;
  try{results=await env.DB.batch(statements);}
  catch(error){
    const rows=(await env.DB.prepare('SELECT key,used FROM upload_limits WHERE window_start=? AND key IN (?,?,?)')
      .bind(start,...dailyLimits.map(([key])=>key)).all()).results;
    const needed=await env.DB.prepare(incoming+`SELECT count(*) AS n FROM incoming r LEFT JOIN cloud_public p ON p.gid=r.gid WHERE NOT ${skip}`).bind(raw).first();
    const remaining=Math.min(...dailyLimits.map(([key,cap])=>Math.max(0,cap-(rows.find(row=>row.key===key)?.used||0))));
    if(needed.n>remaining)throw new ApiError(429,'今日上传额度已达上限，已保留队列。',{
      'Retry-After':String(Math.max(1,start+86400-now)),'X-Upload-Limit-Scope':'day','X-Upload-Remaining':String(remaining)});
    throw error;
  }
  const candidates=results[5].results,published=results[6].results;
  const submitted=new Set(records.map(r=>r.gid));
  if(!Array.isArray(candidates)||!Array.isArray(published)
      || [...candidates,...published].some(row=>!submitted.has(row.gid))
      || new Set([...candidates,...published].map(row=>row.gid)).size!==candidates.length+published.length) {
    throw new ApiError(503,'上传确认结果暂不可用，请稍后重试；已保存的相同记录不会重复入库。');
  }
  const accepted=published.length,pending=candidates.length;
  const written=new Set([...candidates,...published].map(row=>row.gid));
  const skips=results[9].results.filter(row=>!written.has(row.gid));
  if(skips.some(row=>!submitted.has(row.gid)||!integer(row.revision,1,Number.MAX_SAFE_INTEGER))
      || new Set(skips.map(row=>row.gid)).size!==skips.length)throw new ApiError(503,'免上传确认暂不可用，请稍后重试。');
  return json({version:VERSION,accepted,pending,kept:records.length-accepted-pending,source_status:source.status,
    ...(skips.length?{upload_skips:skips.map(row=>({gid:row.gid,revision:row.revision})),dataset_id:skips[0].dataset_id,
      upload_min_interval_days:policy.upload_min_interval_days}:{})});
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
function pendingAnomaly(alias='p') {
  const favorite=`json_extract(${alias}.payload,'$.favorite_count')`,rating=`json_extract(${alias}.payload,'$.rating_count')`;
  const oldFavorite="json_extract(c.payload,'$.favorite_count')",oldRating="json_extract(c.payload,'$.rating_count')";
  const change=(next,old)=>`(${next}>max(${old}*5+1000,${old}+10000)
    OR (${old}>=100 AND ${next}<${old}*0.5 AND ${old}-${next}>=100) OR (${next}=0 AND ${old}>0))`;
  return `(${favorite}>1000000 OR (${rating} IS NOT NULL AND ${rating}>1000000)
    OR EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=${alias}.gid AND c.action='correction'
      AND ${alias}.checked_at<=c.received_at AND ${alias}.checked_at>c.checked_at)
    OR EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=${alias}.gid AND c.payload IS NOT NULL
      AND (${change(favorite,oldFavorite)} OR (${oldRating} IS NOT NULL
        AND (${rating} IS NULL OR ${change(rating,oldRating)})))))`;
}
async function pendingFingerprint(row) { return await digest(row.source_id+'\n'+row.payload); }
async function reviewBatch(env,rows,action,includeAnomalies,scope,now) {
  if(!rows.length)return {approved:0,kept:0,rejected:0,skipped:0,processed:0};
  const expected=JSON.stringify(rows.map(r=>({id:r.id,payload:r.payload,checked_at:r.checked_at,source_id:r.source_id})));
  const matches=`EXISTS(SELECT 1 FROM json_each(?) e WHERE json_extract(e.value,'$.id')=p.id
    AND json_extract(e.value,'$.payload')=p.payload AND json_extract(e.value,'$.checked_at')=p.checked_at
    AND json_extract(e.value,'$.source_id')=p.source_id)`;
  const safe=`(?=1 OR NOT ${pendingAnomaly()})`;
  const statements=[];
  if(action==='approve')statements.push(env.DB.prepare(`INSERT INTO cloud_public(gid,payload,checked_at,source_id,action,received_at)
    SELECT p.gid,p.payload,p.checked_at,p.source_id,'snapshot',? FROM cloud_pending p
    JOIN upload_sources s ON s.id=p.source_id WHERE ${matches} AND s.status<>'blocked' AND ${safe}
    AND NOT EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=p.gid AND c.checked_at>=p.checked_at)
    ORDER BY p.checked_at DESC,p.id ${UPSERT} WHERE excluded.checked_at>cloud_public.checked_at RETURNING gid`)
    .bind(now,expected,includeAnomalies?1:0));
  statements.push(env.DB.prepare(`DELETE FROM cloud_pending AS p WHERE ${matches} AND ${safe}
    ${action==='approve'?`AND EXISTS(SELECT 1 FROM upload_sources s WHERE s.id=p.source_id AND s.status<>'blocked')
      AND EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=p.gid AND c.checked_at>=p.checked_at)`:''}
    RETURNING id`).bind(expected,includeAnomalies?1:0));
  statements.push(env.DB.prepare('INSERT INTO cloud_audit(action,target,created_at) VALUES(?,?,?)')
    .bind('pending_bulk_'+action,JSON.stringify({scope,ids:rows.map(r=>r.id)}),now));
  const results=await env.DB.batch(statements);
  const published=action==='approve'?results[0].results:[],removed=results[action==='approve'?1:0].results;
  const ids=new Set(rows.map(r=>r.id)),gids=new Set(rows.map(r=>r.gid));
  if(!Array.isArray(published)||!Array.isArray(removed)||published.length>removed.length
    || new Set(published.map(r=>r.gid)).size!==published.length
    || new Set(removed.map(r=>r.id)).size!==removed.length
    || published.some(r=>!gids.has(r.gid))||removed.some(r=>!ids.has(r.id))) {
    throw new ApiError(503,'批量审核确认暂不可用，请重新读取后继续。');
  }
  return {approved:published.length,kept:action==='approve'?removed.length-published.length:0,
    rejected:action==='reject'?removed.length:0,skipped:rows.length-removed.length,processed:removed.length};
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
    return json({...c,...counts,limits,caps:{...CAPS,trusted_daily_records:c.trusted_daily_records,
      ip_daily_records:c.ip_daily_records,global_daily_records:c.global_daily_records,
      upload_min_interval_days:c.upload_min_interval_days},actions});
  }
  if(path==='/v3/admin/policy' && request.method==='POST') {
    const v=await body(request);
    const hasInterval=Object.hasOwn(v||{},'upload_min_interval_days');
    if(!exactKeys(v,hasInterval?['trusted_daily_records','ip_daily_records','global_daily_records','upload_min_interval_days']
        :['trusted_daily_records','ip_daily_records','global_daily_records'])
        || !integer(v.trusted_daily_records,500,100000)
        || !integer(v.ip_daily_records,500,500000)
        || !integer(v.global_daily_records,500,1000000)
        || hasInterval&&!integer(v.upload_min_interval_days,0,365))throw new ApiError(400,'配置须为范围内的整数：可信来源 500～100000，单 IP 500～500000，全站 500～1000000；公共更新间隔 0～365 天。');
    const current=await controls(env);
    await env.DB.batch([
      env.DB.prepare(`INSERT INTO upload_policy(id,trusted_daily_records,ip_daily_records,global_daily_records,upload_min_interval_days)
        VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        trusted_daily_records=excluded.trusted_daily_records,ip_daily_records=excluded.ip_daily_records,
        global_daily_records=excluded.global_daily_records,
        upload_min_interval_days=${hasInterval?'excluded.upload_min_interval_days':'upload_policy.upload_min_interval_days'}`)
        .bind(v.trusted_daily_records,v.ip_daily_records,v.global_daily_records,
          hasInterval?v.upload_min_interval_days:current.upload_min_interval_days),
      env.DB.prepare('INSERT INTO cloud_audit(action,target,created_at) VALUES(?,?,?)')
        .bind('upload_policy',JSON.stringify(v),now)
    ]);
    return json(v);
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
    const rows=(await env.DB.prepare(`SELECT p.*,s.name,s.status,${pendingAnomaly()} AS anomaly FROM cloud_pending p JOIN upload_sources s ON s.id=p.source_id
      ORDER BY p.received_at,p.id LIMIT 100`).all()).results;
    return json({pending:await Promise.all(rows.map(async r=>({...r,anomaly:Boolean(r.anomaly),snapshot:JSON.parse(r.payload),
      fingerprint:await pendingFingerprint(r),payload:undefined})))});
  }
  if(path==='/v3/admin/pending/bulk-preview' && request.method==='GET') {
    if([...url.searchParams.keys()].some(key=>!['source_id','include_anomalies'].includes(key))
      || url.searchParams.getAll('source_id').length!==1||url.searchParams.getAll('include_anomalies').length>1)
      throw new ApiError(400,'批量审核范围不正确。');
    const id=url.searchParams.get('source_id'),flag=url.searchParams.get('include_anomalies')||'0';
    if(!UUID.test(id||'')||!['0','1'].includes(flag))throw new ApiError(400,'来源编号或异常选项不正确。');
    const source=await env.DB.prepare('SELECT id,name,status FROM upload_sources WHERE id=?').bind(id).first();
    if(!source)throw new ApiError(404,'上传来源不存在。');
    const counts=await env.DB.prepare(`SELECT COUNT(*) AS total,COALESCE(SUM(CASE WHEN ${pendingAnomaly()} THEN 1 ELSE 0 END),0) AS anomalies,
      COUNT(CASE WHEN ?=1 OR NOT ${pendingAnomaly()} THEN 1 END) AS count,
      COALESCE(MAX(CASE WHEN ?=1 OR NOT ${pendingAnomaly()} THEN p.id END),0) AS max_id
      FROM cloud_pending p WHERE p.source_id=?`).bind(flag==='1'?1:0,flag==='1'?1:0,id).first();
    return json({...counts,source,include_anomalies:flag==='1',batch_size:MAX_BULK_REVIEW_BATCH});
  }
  if(path==='/v3/admin/pending/review-source' && request.method==='POST') {
    const v=await body(request);
    if(!exactKeys(v,['source_id','include_anomalies','action','after','max_id'])||typeof v.source_id!=='string'||!UUID.test(v.source_id)
      || typeof v.include_anomalies!=='boolean'||!['approve','reject'].includes(v.action)
      || !integer(v.max_id,1,Number.MAX_SAFE_INTEGER)||!integer(v.after,0,v.max_id))
      throw new ApiError(400,'按来源批量审核字段或进度不正确。');
    const source=await env.DB.prepare('SELECT status FROM upload_sources WHERE id=?').bind(v.source_id).first();
    if(!source)throw new ApiError(404,'上传来源不存在。');
    if(v.action==='approve'&&source.status==='blocked')throw new ApiError(409,'来源已封禁，请重新核对；候选保留，可单独拒绝。');
    const found=(await env.DB.prepare(`SELECT p.id,p.gid,p.payload,p.checked_at,p.source_id FROM cloud_pending p
      WHERE p.source_id=? AND p.id>? AND p.id<=? AND (?=1 OR NOT ${pendingAnomaly()}) ORDER BY p.id LIMIT ?`)
      .bind(v.source_id,v.after,v.max_id,v.include_anomalies?1:0,MAX_BULK_REVIEW_BATCH+1).all()).results;
    const rows=found.slice(0,MAX_BULK_REVIEW_BATCH);
    const result=await reviewBatch(env,rows,v.action,v.include_anomalies,v.source_id,now);
    const remaining=(await env.DB.prepare(`SELECT COUNT(*) AS count FROM cloud_pending p
      WHERE p.source_id=? AND p.id<=? AND (?=1 OR NOT ${pendingAnomaly()})`)
      .bind(v.source_id,v.max_id,v.include_anomalies?1:0).first()).count;
    return json({...result,next:rows.length?rows.at(-1).id:v.max_id,
      has_more:found.length>MAX_BULK_REVIEW_BATCH,remaining});
  }
  if(path==='/v3/admin/pending/review-selected' && request.method==='POST') {
    const v=await body(request);
    if(!exactKeys(v,['items','include_anomalies','action'])||typeof v.include_anomalies!=='boolean'
      || !['approve','reject'].includes(v.action)||!Array.isArray(v.items)||!v.items.length||v.items.length>MAX_BULK_REVIEW_BATCH
      || v.items.some(r=>!exactKeys(r,['id','fingerprint'])||!integer(r.id,1,Number.MAX_SAFE_INTEGER)
        || typeof r.fingerprint!=='string'||!/^[0-9a-f]{64}$/.test(r.fingerprint))
      || new Set(v.items.map(r=>r.id)).size!==v.items.length)throw new ApiError(400,'勾选批量审核字段不正确，每批最多 50 条。');
    const found=(await env.DB.prepare(`SELECT p.id,p.gid,p.payload,p.checked_at,p.source_id FROM cloud_pending p
      WHERE p.id IN (SELECT json_extract(value,'$.id') FROM json_each(?))
      AND (?=1 OR NOT ${pendingAnomaly()}) ORDER BY p.id`).bind(JSON.stringify(v.items),v.include_anomalies?1:0).all()).results;
    const expected=new Map(v.items.map(r=>[r.id,r.fingerprint])),rows=[];
    for(const row of found)if(expected.get(row.id)===await pendingFingerprint(row))rows.push(row);
    const result=await reviewBatch(env,rows,v.action,v.include_anomalies,'selected',now);
    return json({...result,skipped:result.skipped+v.items.length-rows.length});
  }
  if(path==='/v3/admin/pending/trusted-preview' && request.method==='GET') {
    const scope=await env.DB.prepare(`SELECT COUNT(*) AS count,COALESCE(MAX(p.id),0) AS max_id
      FROM cloud_pending p JOIN upload_sources s ON s.id=p.source_id WHERE s.status='trusted'`).first();
    return json(scope);
  }
  if(path==='/v3/admin/pending/approve-trusted' && request.method==='POST') {
    const value=await body(request);
    if(!exactKeys(value,['after','max_id']) || !integer(value.max_id,1,Number.MAX_SAFE_INTEGER)
        || !integer(value.after,0,value.max_id))throw new ApiError(400,'批量审批范围或进度不正确。');
    const found=(await env.DB.prepare(`SELECT p.id,p.gid,p.payload,p.checked_at FROM cloud_pending p
      JOIN upload_sources s ON s.id=p.source_id WHERE s.status='trusted' AND p.id>? AND p.id<=?
      ORDER BY p.id LIMIT ?`).bind(value.after,value.max_id,MAX_ADMIN_BATCH+1).all()).results;
    const rows=found.slice(0,MAX_ADMIN_BATCH),statements=[];
    for(const row of rows){
      statements.push(env.DB.prepare(`INSERT INTO cloud_public(gid,payload,checked_at,source_id,action,received_at)
        SELECT p.gid,p.payload,p.checked_at,p.source_id,'snapshot',? FROM cloud_pending p
        JOIN upload_sources s ON s.id=p.source_id WHERE p.id=? AND p.payload=? AND p.checked_at=?
        AND s.status='trusted' AND NOT EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=p.gid AND c.checked_at>=p.checked_at)
        ${UPSERT} RETURNING gid`).bind(now,row.id,row.payload,row.checked_at));
      statements.push(env.DB.prepare(`DELETE FROM cloud_pending WHERE id=? AND payload=? AND checked_at=?
        AND EXISTS(SELECT 1 FROM upload_sources s WHERE s.id=cloud_pending.source_id AND s.status='trusted')
        AND EXISTS(SELECT 1 FROM cloud_public c WHERE c.gid=cloud_pending.gid AND c.checked_at>=cloud_pending.checked_at)
        RETURNING id`).bind(row.id,row.payload,row.checked_at));
    }
    let approved=0,processed=0;
    if(rows.length){
      statements.push(env.DB.prepare('INSERT INTO cloud_audit(action,target,created_at) VALUES(?,?,?)')
        .bind('pending_approve_trusted_batch',rows.map(row=>row.id).join(','),now));
      const results=await env.DB.batch(statements);
      for(let index=0;index<rows.length;index++){
        const published=results[index*2].results,removed=results[index*2+1].results,row=rows[index];
        if(!Array.isArray(published) || !Array.isArray(removed) || published.length>1 || removed.length>1
            || published.length>removed.length || published.some(item=>item.gid!==row.gid)
            || removed.some(item=>item.id!==row.id))throw new ApiError(503,'批量审批确认暂不可用，请重新读取后继续。');
        approved+=published.length;processed+=removed.length;
      }
    }
    const remaining=(await env.DB.prepare(`SELECT COUNT(*) AS count FROM cloud_pending p
      JOIN upload_sources s ON s.id=p.source_id WHERE s.status='trusted' AND p.id<=?`).bind(value.max_id).first()).count;
    return json({approved,kept:processed-approved,skipped:rows.length-processed,processed,
      next:rows.length?rows.at(-1).id:value.max_id,has_more:found.length>MAX_ADMIN_BATCH,remaining});
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
<title>ExCatalog 同步管理</title><style nonce="NONCE">body{font:16px system-ui;background:#f5f7fb;color:#18243b;max-width:1000px;margin:30px auto;padding:0 20px}section{background:white;border:1px solid #dce3ee;border-radius:12px;padding:20px;margin:20px 0}input,button{font:inherit;padding:10px;border:1px solid #bcc9dd;border-radius:7px;margin:5px 4px 5px 0;max-width:100%;box-sizing:border-box}button{cursor:pointer;background:#eaf0ff}.row{padding:12px 0;border-bottom:1px solid #e2e6ed;overflow-wrap:anywhere}.source-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.source-name{font-weight:600}.status-badge{display:inline-block;font-weight:600;border:1px solid;border-radius:999px;padding:4px 12px}.status-trusted{color:#166534;background:#ecfdf3;border-color:#86d4a2}.status-review{color:#92400e;background:#fff7e7;border-color:#e8bd6a}.status-blocked{color:#b91c1c;background:#fff0f0;border-color:#efa2a2}button:disabled{cursor:default;color:#64748b;background:#edf1f6;border-color:#d5dce6}p{line-height:1.6}#audit{overflow-wrap:anywhere}#status{white-space:pre-wrap}</style>
<h1>收藏数同步服务管理</h1><p>所有人可提交；新来源待审核，可信来源正常变化自动入库，异常变化仍待审。密钥仅保留在本页输入框。</p>
<section><label for="secret">管理员密钥 ADMIN_SECRET</label><br><input id="secret" type="password" autocomplete="off" size="45"><button id="load">读取管理信息</button><button id="make-secret">本地生成新管理员密钥</button><button id="copy-secret">复制密钥</button><p id="status" role="status"></p></section>
<section><h2>上传开关与用量</h2><p id="metrics"></p><button id="enable">开放上传</button><button id="pause">暂停上传</button><button id="cleanup">清理过期限流计数</button><p>升级后默认暂停。暂停只阻止新上传，公开下载继续可用。备份请使用 Cloudflare D1 导出；历史记录随数据库一起备份。</p><div id="audit"></div></section>
<section id="upload-policy"><h2>每日上传额度与更新间隔</h2><p>每个可信来源、单 IP 与全站额度同时生效，按 UTC 日期重置（北京时间 08:00）。新来源仍限 500 条/日；调整配置保留今日已用量，不改变来源审核状态。</p>
<label for="policy-trusted">每个可信来源<input id="policy-trusted" type="number" min="500" max="100000" step="1"></label>
<label for="policy-ip">单 IP<input id="policy-ip" type="number" min="500" max="500000" step="1"></label>
<label for="policy-global">全站<input id="policy-global" type="number" min="500" max="1000000" step="1"></label>
<label for="policy-interval">公共更新间隔（天）<input id="policy-interval" type="number" min="0" max="365" step="1"></label>
<p>默认 14 天：本机采集时间比已公开快照晚不足此间隔时免上传，不扣日上传条数；达到间隔才提交。设为 0 天恢复有更新就提交。比较采集时间，不是上传时间或等待天数。新作品与仅待审核、已撤回的记录仍正常提交。间隔越长，公共收藏数更新越慢。</p>
<button id="policy-save">保存上传策略</button><p>上万条资料库可按容量将可信来源设为 20000、单 IP 30000、全站 100000；最终可上传数量受三者共同限制。默认额度仍为 5000、10000、50000。修改间隔后，新客户端下次检查策略时重查免上传记录，也可点击「立即同步」。</p></section>
<section><h2>上传来源</h2><p>来源标识显示「已可信」「待审核」或「已封禁」；对应的当前状态按钮不可重复点击。新来源可提交到服务器待审核区，批准后才进入公共库；设为可信后，后续正常数据自动入库，已有候选仍需批准。请核对来源编号再设为可信；封禁不撤回已发布数据。</p><div id="sources"></div><button id="source-next">下一页来源</button><button id="source-first">返回首批来源</button></section>
<section id="pending-section"><div id="pending-review-controls"><h2>待审核数据</h2><p>按来源批量处理可覆盖全部候选，不限于下方最早 100 条。新批量操作默认保留异常候选；勾选后才包含异常。核对后批准不会改变来源的可信状态，封禁来源只能拒绝。</p>
<button id="pending-approve-trusted">一键审批可信来源的待审核数据</button><button id="pending-bulk-stop" disabled>停止后续批次</button>
<h3>按上传来源批量处理</h3><label for="pending-source">来源编号</label><input id="pending-source" size="40" autocomplete="off" placeholder="从上传来源选择，或粘贴来源编号">
<label><input id="pending-include-anomalies" type="checkbox">包含异常候选（需单独核对）</label><br>
<button id="pending-source-preview">查看该来源范围</button><button id="pending-source-approve">批准该来源全部候选</button><button id="pending-source-reject">拒绝该来源全部候选</button>
<p id="pending-source-info">请先选择来源并查看范围。按来源操作会再次读取范围并确认一次，再以每批最多 50 条自动继续。</p>
<h3>处理勾选记录</h3><label><input id="pending-select-page" type="checkbox">全选本页候选（最多 100 条）</label>
<button id="pending-selected-approve">批准勾选记录</button><button id="pending-selected-reject">拒绝勾选记录</button><span id="pending-selected-count">已勾选 0 条</span>
<p>勾选操作检查页面读取时的数据版本，发生变化的候选保留待审；按来源操作固定候选编号上限，执行期间新增候选留到下一次。所有批量操作显示结果、支持停止，已完成批次保留。原可信来源一键审批仍包含异常候选。</p>
<p id="pending-bulk-progress" role="status" aria-live="polite"></p></div><div id="pending"></div></section>
<section><h2>按来源和时间范围撤回</h2><input id="rollback-source" placeholder="上传来源编号"><label>起始时间<input id="rollback-from" type="datetime-local"></label><label>结束时间<input id="rollback-until" type="datetime-local"></label><button id="rollback-preview">预览受影响作品</button><button id="rollback-next">预览下一批</button><button id="rollback-apply">撤回本批</button><p>时间采用本机时区，留空表示不限。每批最多 5 部作品，恢复为此前其他未封禁来源的最后一份历史快照；没有历史时撤回。更新的其他来源记录不会被批量撤回。请先封禁错误来源，再预览并核对。</p><div id="rollback"></div></section>
<section><h2>历史恢复与撤回</h2><input id="gid" type="number" min="1" placeholder="作品 ID"><button id="history-load">读取历史</button><button id="history-next">更早的历史</button><button id="retract">撤回当前公共快照</button><p>恢复会发布纠正记录，已升级客户端可撤回错误云端值并保留更新的自采数据。升级前仅能保留迁移时的最新快照。</p><div id="history"></div></section>
<script nonce="NONCE">
const el=id=>document.getElementById(id);let busy=false,sourceAfter='',sourceNext='',historyBefore='',current=0,rollbackRows=[],rollbackNext=0,rollbackAfter=0,rollbackMore=false,rollbackFilter='';
let bulkRunning=false,bulkStop=false,sourceScope=null;const pendingSelections=new Map();let pendingRows=[];
async function call(path,method='GET',data){const secret=el('secret').value;if(!secret)throw Error('请填写管理员密钥。');const r=await fetch(path,{method,headers:{Authorization:'Bearer '+secret,...(data?{'Content-Type':'application/json'}:{})},...(data?{body:JSON.stringify(data)}:{})});const v=await r.json();if(!r.ok)throw Error(v.error||'操作失败');return v;}
function controls(){document.querySelectorAll('button').forEach(b=>b.disabled=busy||b.dataset.stateDisabled==='true');el('pending-bulk-stop').disabled=!bulkRunning||bulkStop;document.querySelectorAll('[data-pending-select],#pending-select-page,#pending-source,#pending-include-anomalies').forEach(input=>input.disabled=busy);}
async function task(fn){if(busy)return;busy=true;controls();el('status').textContent='处理中…';try{await fn();el('status').textContent='完成。';}catch(e){el('status').textContent=e.message;}finally{busy=false;controls();}}
function text(parent,value){const p=document.createElement('p');p.textContent=value;parent.append(p);}
function button(parent,label,fn,disabled=false){const b=document.createElement('button');b.textContent=label;b.dataset.stateDisabled=String(disabled);b.disabled=busy||disabled;b.onclick=()=>task(fn);parent.append(b);return b;}
const sourceLabels={trusted:'已可信',review:'待审核',blocked:'已封禁'};
const sourceHints={trusted:'后续正常快照自动进入公共库；异常变化仍待审核。',review:'数据可提交到服务器待审核区，管理员批准后才公开共享。',blocked:'已停止该来源后续上传；已发布数据可另行恢复或撤回。'};
const actionLabels={enable_upload:'开放上传',pause_upload:'暂停上传',source_trusted:'来源设为可信',source_review:'来源改为待审核',source_blocked:'来源封禁',pending_approve:'批准待审数据',pending_reject:'拒绝待审数据',pending_approve_trusted_batch:'批量审批可信来源候选',restore:'恢复或撤回公共快照',cleanup_limits:'清理过期限流计数',upload_policy:'修改上传策略',pending_bulk_approve:'批量批准候选',pending_bulk_reject:'批量拒绝候选'};
function updatePendingControls(){
  const chosen=[...pendingSelections.values()],valid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(el('pending-source').value.trim());
  const include=el('pending-include-anomalies').checked;
  el('pending-selected-count').textContent='已勾选 '+chosen.length+' 条，其中异常 '+chosen.filter(v=>v.anomaly).length+' 条，封禁 '+chosen.filter(v=>v.status==='blocked').length+' 条';
  el('pending-selected-approve').dataset.stateDisabled=String(!chosen.some(v=>v.status!=='blocked'&&(include||!v.anomaly)));
  el('pending-selected-reject').dataset.stateDisabled=String(!chosen.some(v=>include||!v.anomaly));
  el('pending-source-preview').dataset.stateDisabled=String(!valid);
  el('pending-source-approve').dataset.stateDisabled=String(!valid||sourceScope?.source.status==='blocked');
  el('pending-source-reject').dataset.stateDisabled=String(!valid);
  el('pending-select-page').checked=pendingRows.length>0&&chosen.length===pendingRows.length;
  el('pending-select-page').indeterminate=chosen.length>0&&chosen.length<pendingRows.length;
  controls();
}
function renderPending(rows){
  pendingRows=rows;pendingSelections.clear();el('pending').replaceChildren();
  for(const v of rows){
    const row=document.createElement('div');row.className='row';
    const label=document.createElement('label'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.dataset.pendingSelect=String(v.id);
    checkbox.onchange=()=>{if(checkbox.checked)pendingSelections.set(v.id,v);else pendingSelections.delete(v.id);updatePendingControls();};
    label.append(checkbox,document.createTextNode('勾选作品 '+v.gid));row.append(label);
    text(row,(v.anomaly?'异常候选 · ':'')+v.reason+' · '+(sourceLabels[v.status]||'未知状态')+' · '+(v.name||'无昵称')+' · 来源编号 '+v.source_id);
    const r=v.snapshot;text(row,'收藏 '+r.favorite_count+'；评分人数 '+(r.rating_count??'未知')+'；'+new Date(r.checked_at*1000).toLocaleString());
    if(v.status!=='blocked')button(row,'核对后批准',async()=>{if(!confirm('已核对源站，批准此快照？'))return;await call('/v3/admin/pending/'+v.id+'/approve','POST');await refresh();});
    button(row,'拒绝',async()=>{if(!confirm('拒绝此候选？'))return;await call('/v3/admin/pending/'+v.id+'/reject','POST');await refresh();});el('pending').append(row);
  }
  if(!rows.length)text(el('pending'),'没有待审数据。');updatePendingControls();
}
async function previewSource(){
  const id=el('pending-source').value.trim(),include=el('pending-include-anomalies').checked;
  sourceScope=await call('/v3/admin/pending/bulk-preview?source_id='+encodeURIComponent(id)+'&include_anomalies='+(include?'1':'0'));
  const v=sourceScope;el('pending-source-info').textContent=(v.source.name||'无昵称')+' · '+(sourceLabels[v.source.status]||'未知状态')+'：共 '+v.total+' 条候选，其中异常 '+v.anomalies+' 条；本次范围 '+v.count+' 条。';
  updatePendingControls();return v;
}
function bulkDescription(v){return '已批准 '+v.approved+' 条，保留已有公共数据并清理 '+v.kept+' 条，已拒绝 '+v.rejected+' 条，因变化、异常或封禁保留 '+v.skipped+' 条。';}
async function runReview(next){
  const progress=el('pending-bulk-progress'),totals={approved:0,kept:0,rejected:0,skipped:0};
  bulkRunning=true;bulkStop=false;controls();let more=true,remaining=0;
  try{
    while(more&&!bulkStop){
      const result=await next();for(const key of Object.keys(totals))totals[key]+=result[key]||0;
      more=result.has_more;remaining=result.remaining||0;progress.textContent='分批审核中：'+bulkDescription(totals);
    }
    progress.textContent=(bulkStop&&more?'已停止后续批次：':'本次批量审核完成：')+bulkDescription(totals)
      +(remaining?' 本次范围内还有 '+remaining+' 条，可重新核对后继续。':'');
    bulkRunning=false;controls();await refresh();
  }catch(error){progress.textContent='批量审核中断：'+bulkDescription(totals)+' 已完成批次保留，请重新读取后继续。';throw error;}
  finally{bulkRunning=false;controls();}
}
async function reviewSource(action){
  const progress=el('pending-bulk-progress'),scope=await previewSource();
  if(!scope.count){progress.textContent='本次范围没有候选；可检查来源或包含异常选项。';return;}
  if(action==='approve'&&scope.source.status==='blocked')throw Error('来源已封禁，候选只能拒绝。');
  const verb=action==='approve'?'批准':'拒绝';
  if(!confirm('确认'+verb+'来源「'+(scope.source.name||'无昵称')+'」的 '+scope.count+' 条候选？\\n来源编号：'+scope.source.id+'\\n'+(scope.include_anomalies?'本次包含异常候选。':'异常候选保留。')+'\\n批准不会改变来源可信状态；较新公共数据保留，执行期间新增候选留到下一次。')){progress.textContent='已取消批量审核。';return;}
  let after=0;await runReview(async()=>{const result=await call('/v3/admin/pending/review-source','POST',{
    source_id:scope.source.id,include_anomalies:scope.include_anomalies,action,after,max_id:scope.max_id});after=result.next;return result;});
}
async function reviewSelected(action){
  const chosen=[...pendingSelections.values()],include=el('pending-include-anomalies').checked,progress=el('pending-bulk-progress');
  if(!chosen.length)return;
  if(!confirm('确认'+(action==='approve'?'批准':'拒绝')+'勾选的 '+chosen.length+' 条候选？\\n'+(include?'本次包含异常候选。':'异常候选保留。')+'\\n数据变化的候选保留；封禁来源不会获批，批准不改变来源可信状态。')){progress.textContent='已取消批量审核。';return;}
  let offset=0;await runReview(async()=>{const items=chosen.slice(offset,offset+50).map(v=>({id:v.id,fingerprint:v.fingerprint}));
    const result=await call('/v3/admin/pending/review-selected','POST',{items,include_anomalies:include,action});offset+=items.length;
    return {...result,has_more:offset<chosen.length,remaining:chosen.length-offset};});
}
el('pending-select-page').onchange=()=>{pendingSelections.clear();const checked=el('pending-select-page').checked;
  for(const row of pendingRows)if(checked)pendingSelections.set(row.id,row);
  document.querySelectorAll('[data-pending-select]').forEach(input=>input.checked=checked);updatePendingControls();};
for(const id of ['pending-source','pending-include-anomalies'])el(id).addEventListener('input',()=>{sourceScope=null;el('pending-source-info').textContent='范围已修改，请重新查看。';updatePendingControls();});
el('pending-source-preview').onclick=()=>task(previewSource);
el('pending-source-approve').onclick=()=>task(()=>reviewSource('approve'));
el('pending-source-reject').onclick=()=>task(()=>reviewSource('reject'));
el('pending-selected-approve').onclick=()=>task(()=>reviewSelected('approve'));
el('pending-selected-reject').onclick=()=>task(()=>reviewSelected('reject'));
updatePendingControls();
async function approveTrusted(){
  const progress=el('pending-bulk-progress');progress.textContent='正在读取可信来源的待审数量…';
  const scope=await call('/v3/admin/pending/trusted-preview');
  if(!scope.count){progress.textContent='当前没有已可信来源的待审核数据。';return;}
  if(!confirm('确认批量审批已可信来源的 '+scope.count+' 条现有待审候选？包括异常候选；未可信及封禁来源保持待审，较新的公共数据保留。')){progress.textContent='已取消批量审批。';return;}
  let after=0,approved=0,kept=0,skipped=0,remaining=scope.count,more=true;
  bulkRunning=true;bulkStop=false;controls();
  const describe=()=> '已批准 '+approved+' 条，保留已有公共版本并清理 '+kept+' 条，因状态或记录变化保留 '+skipped+' 条。';
  try{
    while(more&&!bulkStop){
      const result=await call('/v3/admin/pending/approve-trusted','POST',{after,max_id:scope.max_id});
      approved+=result.approved;kept+=result.kept;skipped+=result.skipped;remaining=result.remaining;
      after=result.next;more=result.has_more;progress.textContent='分批审批中：'+describe();
    }
    progress.textContent=(bulkStop&&more?'已停止后续批次：':'本次批量审批完成：')+describe()
      +(remaining?' 本次范围内还有 '+remaining+' 条可信候选，可重新读取后继续。':'');
    bulkRunning=false;controls();await refresh();
  }catch(error){progress.textContent='批量审批中断：'+describe()+' 已完成批次保留，请重新读取管理信息后继续。';throw error;}
  finally{bulkRunning=false;controls();}
}
el('policy-save').onclick=()=>task(async()=>{if(['policy-trusted','policy-ip','policy-global','policy-interval'].some(id=>!el(id).reportValidity()||el(id).value===''))throw Error('请填写范围内的整数额度和更新间隔。');await call('/v3/admin/policy','POST',{trusted_daily_records:Number(el('policy-trusted').value),ip_daily_records:Number(el('policy-ip').value),global_daily_records:Number(el('policy-global').value),upload_min_interval_days:Number(el('policy-interval').value)});await refresh();});
el('pending-approve-trusted').onclick=()=>task(approveTrusted);
el('pending-bulk-stop').onclick=()=>{bulkStop=true;controls();el('pending-bulk-progress').textContent='已请求停止；当前批次完成后停止，已完成的审批保留。';};
async function refresh(){const [m,s,p]=await Promise.all([call('/v3/admin/status'),call('/v3/admin/sources?after='+encodeURIComponent(sourceAfter)),call('/v3/admin/pending')]);for(const [id,key] of [['policy-trusted','trusted_daily_records'],['policy-ip','ip_daily_records'],['policy-global','global_daily_records'],['policy-interval','upload_min_interval_days']])el(id).value=m.caps[key];el('enable').dataset.stateDisabled=String(Boolean(m.upload_enabled));el('pause').dataset.stateDisabled=String(!m.upload_enabled);el('metrics').textContent=(m.upload_enabled?'上传开放':'上传暂停')+'；公共作品 '+m.public_count+'；候选 '+m.pending_count+'/'+m.caps.pending+'；历史 '+m.history_count+'；来源 '+m.source_count+'；封禁 '+m.blocked_count+'。';for(const q of m.limits)text(el('metrics'),q.key+'：'+q.used+'（窗口起点 '+new Date(q.window_start*1000).toLocaleString()+'）');el('audit').replaceChildren();for(const a of m.actions)text(el('audit'),(actionLabels[a.action]||a.action)+' · '+a.target+' · '+new Date(a.created_at*1000).toLocaleString());el('sources').replaceChildren();sourceNext=s.has_more?s.next:'';for(const v of s.sources){const row=document.createElement('div');row.className='row';const heading=document.createElement('div');heading.className='source-heading';const name=document.createElement('span');name.className='source-name';name.textContent=v.name||'无昵称';const badge=document.createElement('span');badge.className='status-badge status-'+v.status;badge.textContent=sourceLabels[v.status]||'未知状态';heading.append(name,badge);row.append(heading);text(row,'来源编号 '+v.id+' · 采集者 '+v.collector_id);text(row,sourceHints[v.status]||'请重新读取来源状态。');for(const [status,label,currentLabel] of [['trusted','设为可信','已设为可信'],['review','恢复待审核','当前待审核'],['blocked','封禁上传','已封禁上传']])button(row,status===v.status?currentLabel:label,async()=>{if(!confirm('将此来源改为 '+label+'？'))return;await call('/v3/admin/sources/'+v.id,'POST',{status});await refresh();},status===v.status);button(row,'批量处理此来源',async()=>{el('pending-source').value=v.id;sourceScope=null;updatePendingControls();await previewSource();el('pending-section').scrollIntoView({block:'start'});});button(row,'预览该来源的撤回',async()=>{el('rollback-source').value=v.id;rollbackAfter=0;await rollbackPreview();});el('sources').append(row);}if(!s.sources.length)text(el('sources'),'暂无来源。');renderPending(p.pending);controls();}
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
  if(['/','/health'].includes(url.pathname) && request.method==='GET'){
    const policy=await controls(env);
    return json({service:FORMAT,version:VERSION,ready:true,
    dataset_id:info.dataset_id,read_public:true,upload_scope:'library',preserves_original_collector:true,
    upload_confirmation:'returning_rows',
    trusted_pending_bulk_approval:true,
    scoped_pending_bulk_review:true,max_bulk_review_batch:MAX_BULK_REVIEW_BATCH,
    upload_preflight:true,max_upload_check_batch:MAX_UPLOAD_CHECK_BATCH,public_correction_state:true,
    upload_min_interval_days:policy.upload_min_interval_days,
    uploader_nickname:true,max_upload_batch:MAX_BATCH,upload_strategy:'set_based',quota_retry_after:true,
    upload_requires_contribution_code:false,new_source_requires_review:true,
    upload_enabled:Boolean(policy.upload_enabled),admin_configured:typeof env.ADMIN_SECRET==='string' && /^[A-Za-z0-9_-]{32,256}$/.test(env.ADMIN_SECRET)});
  }
  if(url.pathname==='/v3/changes' && request.method==='GET')return await changes(url,env,info);
  if(url.pathname==='/v3/upload-state' && request.method==='GET')return await uploadState(url,env);
  if(url.pathname==='/v3/upload-check' && request.method==='POST')return await uploadCheck(request,env,now);
  if(url.pathname==='/v3/register' && request.method==='POST')return await register(request,env,now);
  if(url.pathname==='/v3/contribute' && request.method==='POST')return await contribute(request,env,now);
  if(url.pathname.startsWith('/v3/admin/'))return await adminApi(request,url,env,now);
  if(url.pathname.startsWith('/v1/') || url.pathname.startsWith('/v2/'))throw new ApiError(426,'同步协议已升级，请安装 v1.1.19 或更新的客户端。');
  throw new ApiError(404,'接口不存在或方法不正确。');
}catch(e){return json({error:e instanceof ApiError?e.message:'同步暂时不可用、候选容量已满或上传状态变化，请稍后重试。'},e instanceof ApiError?e.status:503,e instanceof ApiError?e.headers:{});}},
async scheduled(controller,env){await cleanup(env,Math.floor(Date.now()/1000));}};

