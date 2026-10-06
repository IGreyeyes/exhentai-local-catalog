"use strict";
(() => {
  let state=null,token="",actionBusy=false,pollBusy=false,settingsDirty=false;
  let preview=null,previewRevision=0,initialStatus=true,errorRowsKey="";
  const ui=id=>document.getElementById(id);
  const labels={running:"采集中",paused:"已暂停",awaiting_next:"本轮已结束 · 等待继续",completed:"采集完成",completed_with_errors:"已处理完 · 有失败项",cancelled:"任务已结束",preparing:"正在准备"};
  function feedback(text,error=false){ui("collector-feedback").textContent=text;ui("collector-feedback").classList.toggle("error",error);}
  function refreshDays(){return Number(ui("collector-refresh").value==="custom"?ui("collector-custom-days").value:ui("collector-refresh").value);}
  function batchSize(){return Number(ui("collector-batch-size").value==="custom"?ui("collector-custom-batch-size").value:ui("collector-batch-size").value);}
  function savedTask(){return Boolean(state?.job&&["paused","awaiting_next","completed_with_errors"].includes(state.job.state));}
  function failureURL(value){
    try{const url=new URL(value);return url.protocol==="https:"&&["exhentai.org","e-hentai.org"].includes(url.hostname)&&!url.username&&!url.password&&!url.port&&/^\/g\/[1-9]\d*\/[0-9a-fA-F]{10}\/$/.test(url.pathname)?url.href:null;}catch(error){return null;}
  }
  function renderErrors(job){
    const entries=job.recent_errors||[];
    ui("collector-job-errors").hidden=!job.failed;
    ui("collector-errors-summary").textContent=`失败作品 ${number(job.failed)} 条 · 展开查看最近 ${number(entries.length)} 条及访问链接`;
    ui("collector-errors-more").href="/records?"+new URLSearchParams({collection:"failed",job_id:String(job.id),sort:"recorded_desc"});
    const key=JSON.stringify([job.id,entries]);
    if(key===errorRowsKey)return;
    errorRowsKey=key;
    ui("collector-error-list").replaceChildren(...entries.map(item=>{
      const row=document.createElement("li"),heading=document.createElement("strong"),reason=document.createElement("p");
      heading.textContent=`作品 ID ${item.gid} · 第 ${item.round_no||1} 轮 · 尝试 ${item.attempts||1} 次`;
      reason.textContent=item.error;
      row.append(heading,reason);
      const url=failureURL(item.source_url);
      if(url){const link=document.createElement("a");link.href=url;link.target="_blank";link.rel="noopener noreferrer";link.textContent=url;row.append(link);}
      return row;
    }));
  }
  function showJob(){
    if(!state?.job)return;
    ui("collector-panel").open=true;
    ui("collector-job").scrollIntoView({block:"start",behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth"});
  }
  function scopeKey(){
    if(!submittedFilters)return "";
    return JSON.stringify({tags:submittedFilters.getAll("tag").sort(),excluded:submittedFilters.getAll("exclude_tag").sort(),blacklist:submittedFilters.get("use_blacklist")||"1",title:submittedFilters.get("title")||"",category:submittedFilters.get("category")||"",inactive:submittedFilters.get("include_inactive")||"0"});
  }
  function invalidatePreview(){preview=null;previewRevision++;ui("collector-estimate").hidden=true;}
  function duration(seconds){
    if(seconds<=0)return "无需抓取";
    if(seconds<60)return `约 ${Math.ceil(seconds)} 秒`;
    const minutes=Math.ceil(seconds/60),days=Math.floor(minutes/1440),hours=Math.floor(minutes%1440/60),rest=minutes%60;
    return "约 "+(days?`${days} 天 ${hours} 小时`:hours?`${hours} 小时 ${rest} 分钟`:`${minutes} 分钟`);
  }
  function renderPreview(data,key,queryText){
    preview={...data,scopeKey:key,queryText};
    ui("collector-estimate").hidden=false;
    const operation={start:"新建采集",next_batch:"追加下一批",next_round:"继续下一轮",resume:"继续任务",retry:"重试失败项"}[data.operation];
    const terms=(data.query.tag||[]).map(tagName);
    (data.query.exclude_tag||[]).forEach(tag=>terms.push("排除："+tagName(tag)));
    if(data.query.title?.[0])terms.push("标题："+data.query.title[0]);
    ui("estimate-scope").textContent=`${operation} · ${terms.join(" + ")} · ${data.host}`;
    ui("estimate-total").textContent=number(data.total)+" 条";
    const newTask=["start","next_batch"].includes(data.operation),batched=data.collection_mode==="batch",rounds=data.collection_mode==="rounds";
    ui("estimate-total-label").textContent=newTask?"全范围匹配":"当前队列范围";
    ui("estimate-pending-label").textContent=rounds?"本轮需要抓取":batched?"本批需要抓取":"需要抓取";
    ui("estimate-pending").textContent=number(data.pending)+" 条";
    ui("estimate-skipped").textContent=number(data.skipped)+" 条";
    ui("estimate-duration").textContent=duration(data.estimated_seconds);
    ui("estimate-detail").textContent=`其中首次采集 ${number(data.never_seen)} 条、过期刷新 ${number(data.refresh_count)} 条。`+(data.skip_existing?"本次跳过所有已成功采集的作品，缓存天数不生效。":`本次复用 ${data.refresh_days} 天内的缓存。`)+(batched&&newTask?`另有 ${number(data.deferred)} 条需要处理，但未纳入本批，留待后续。`:"");
    ui("estimate-detail").textContent+=data.rating_priority?"队列按平均评分从高到低排列；持续失败项延后。":data.collection_mode==="full"?"队列使用作品 ID 顺序。":"队列使用年份均衡顺序。";
    if(rounds)ui("estimate-detail").textContent+=newTask?`将保存 ${number(data.queue_total)} 条待抓取作品，分为 ${number(data.round_count)} 轮，每轮最多 ${number(data.batch_size)} 条。本次只执行第 1 轮，后续由你手动继续。`:`本次处理第 ${number(data.round_number)} / ${number(data.round_count)} 轮；另有 ${number(data.deferred)} 条留在后续轮次。`;
    ui("estimate-coverage").textContent=(data.scope_known!=null?`全范围 ${number(data.scope_total)} 条，预估时已有收藏数 ${number(data.scope_known)} 条。`:`这是原任务的剩余队列；原搜索范围共 ${number(data.scope_total)} 条。`)+(batched?"覆盖完成前，分批结果不能代表完整收藏榜。":"最新覆盖情况可在刷新搜索结果后查看。");
    ui("estimate-years").hidden=!data.year_breakdown?.length;
    ui("estimate-year-list").replaceChildren(...(data.year_breakdown||[]).map(item=>{const label=document.createElement("span");label.textContent=`${item.year||"年份未知"} · ${number(item.count)} 条`;return label;}));
    const basis=data.estimate_basis==="measured"?`网页读取平均约 ${data.response_seconds.toFixed(2)} 秒，来自最近 ${data.response_samples} 条成功请求。`:"尚无网页耗时测量，暂按每条读取 2 秒估算；验证成功后可重新预估。";
    ui("estimate-basis").textContent=data.pending?`请求间隔 ${data.interval} 秒，间隔耗时约 ${duration(data.interval_seconds).replace(/^约 /,"")}。${basis}估计未包含重试、限流和暂停，实际可能更久。`:"全部符合跳过条件，确认后不会向源站发起抓取请求。";
    ui("collector-confirm").textContent=data.pending===0?"确认跳过，无需抓取":data.operation==="next_round"?`确认抓取第 ${data.round_number} 轮`:data.operation==="next_batch"?"确认采集下一批":data.operation==="start"?(rounds?"保存计划并开始第 1 轮":batched?"确认采集本批":"确认开始全量采集"):data.operation==="resume"?"确认继续采集":"确认重试";
    ui("collector-estimate").scrollIntoView({block:"nearest",behavior:"smooth"});
  }
  function updateControls(){
    const job=state?.job;
    const searching=ui("results").getAttribute("aria-busy")==="true";
    const hasSearch=Boolean(responseData?.total&&submittedFilters&&!searching);
    const busy=actionBusy||Boolean(state?.busy)||Boolean(window.catalogMaintenanceActive)||Boolean(window.catalogStopped);
    const cooling=Boolean(state?.cooldown_until>Date.now()/1000);
    for(const control of ui("collector-settings").elements)control.disabled=busy;
    ui("credential-import").disabled=busy;
    ui("credential-export").disabled=busy||settingsDirty||!state?.has_credentials;
    const skip=ui("collector-skip-existing").checked,custom=ui("collector-refresh").value==="custom";
    ui("collector-refresh").disabled=busy||skip;
    ui("custom-refresh").hidden=!custom;
    ui("collector-custom-days").disabled=busy||skip||!custom;
    ui("collector-custom-days").required=custom&&!skip;
    const mode=ui("collector-mode").value,batchMode=mode!=="full",roundMode=mode==="rounds",customBatch=ui("collector-batch-size").value==="custom";
    const ratingPriority=ui("collector-rating-priority").checked;
    ui("collector-batch-size").disabled=busy||!batchMode;
    ui("custom-batch-control").hidden=!batchMode||!customBatch;
    ui("collector-custom-batch-size").disabled=busy||!batchMode||!customBatch;
    ui("collector-custom-batch-size").required=batchMode&&customBatch;
    ui("collector-round-hint").hidden=!roundMode;
    ui("collector-rating-hint").textContent=ratingPriority?"开启后，待抓取候选按数据库平均评分降序；评分相同时作品 ID 较新的优先。持续失败项仍会延后。":"关闭时，分批模式按年份均衡挑选，并在各年份内优先高评分作品；全量模式沿用作品 ID 顺序。";
    ui("collector-batch-hint").textContent=roundMode?"确认时保存全部待抓取作品及其顺序，按设定数量分轮。每轮结束自动停止；修改此处的数量仅影响新计划，已保存计划继续使用原来的每轮数量。":batchMode?(ratingPriority?"从全历史匹配记录中直接选择评分最高的本批候选，不再平均分配年份。分批结果仍不代表完整收藏榜。":"从全历史匹配记录中按发布年份轮流挑选，年份内评分高的优先；先补未记录、未尝试的作品。分批排名不代表完整收藏榜。"):(ratingPriority?"全量采集所有待处理作品，并按平均评分从高到低排列队列。":"全量采集所有符合缓存规则的待处理作品；数量较大时可能需要较长时间。");
    ui("collector-policy-hint").textContent=skip?"只补缺失的收藏数；已成功采集的作品即使过期也跳过，缓存天数此时不生效。失败或未知项仍可抓取。":"超过缓存天数后，再次创建任务时才会刷新；不会自动重新抓取。";
    ui("collector-verify").disabled=busy||cooling||settingsDirty||!state?.configured||(!hasSearch&&!savedTask());
    ui("collector-verify").textContent=savedTask()?"验证原任务的源站访问":"验证并读取一条";
    ui("collector-start").disabled=busy||settingsDirty||!state?.configured||!hasSearch||["paused","running","preparing","awaiting_next"].includes(job?.state);
    ui("collector-pause").disabled=actionBusy||job?.state!=="running";
    ui("collector-resume").disabled=busy||settingsDirty||!state?.configured||job?.state!=="paused";
    ui("collector-retry").disabled=busy||settingsDirty||!state?.configured||!job?.failed||!["paused","awaiting_next","completed_with_errors"].includes(job?.state);
    ui("collector-next-round").hidden=job?.collection_mode!=="rounds"||job?.state!=="awaiting_next";
    ui("collector-next-round").disabled=busy||settingsDirty||!state?.configured;
    if(job?.round_progress)ui("collector-next-round").textContent=`继续第 ${number(job.round_progress.current+1)} 轮 · ${number(Math.min(job.round_progress.remaining,job.selection.batch_size))} 条`;
    ui("collector-pause").hidden=job?.state!=="running";
    ui("collector-resume").hidden=job?.state!=="paused";
    ui("collector-retry").hidden=!job?.failed;
    ui("collector-next-batch").hidden=job?.collection_mode!=="batch";
    ui("collector-next-batch").disabled=busy||settingsDirty||!state?.configured||state?.collection_mode!=="batch"||!["completed","completed_with_errors","cancelled"].includes(job?.state);
    ui("collector-cancel").disabled=actionBusy||!["running","paused","awaiting_next"].includes(job?.state);
    ui("collector-refresh-results").disabled=actionBusy||!hasSearch;
    if(preview&&(settingsDirty||preview.expires_at<=Date.now()/1000||(preview.operation==="start"&&preview.scopeKey!==scopeKey())))invalidatePreview();
    const needsVerification=Boolean(preview?.pending&&!state?.verified);
    ui("collector-confirm").disabled=busy||!preview||settingsDirty||needsVerification||Boolean(preview?.pending&&cooling);
    ui("estimate-verification").hidden=!needsVerification&&!cooling;
    ui("estimate-verification").textContent=needsVerification?`请先点击“${ui("collector-verify").textContent}”，验证成功后重新预估，再确认采集。`:cooling?"源站冷却时间尚未结束，可先查看预估。":"";
    if(hasSearch){
      const names=(responseData.tags||[]).map(tagName).join(" + ");
      const title=submittedFilters.get("title");
      ui("collection-scope").textContent=`新计划候选范围：最近一次搜索的全部 ${number(responseData.total)} 条匹配记录${names?" · "+names:""}${title?" · 标题："+title:""}。`+(batchMode?`${roundMode?"每轮":"每批"}最多抓取 ${number(batchSize())} 条。`:"全量模式处理所有待采集作品。")+"修改筛选条件后，请先重新搜索。";
    }else ui("collection-scope").textContent=savedTask()?"已找到保存的采集任务。应用并验证源站设置后，可直接继续，无需重新搜索；继续时沿用原队列和轮次。":"先完成一次有匹配结果的搜索，再建立采集计划。分轮抓取会保存完整队列，每轮结束自动停止。";
  }
  function render(){
    if(!state)return;
    const job=state.job;
    const jobLabel=job?.collection_mode==="batch"&&job.state==="completed"?"本批完成":job?.collection_mode==="batch"&&job.state==="completed_with_errors"?"本批完成 · 有失败项":job?(labels[job.state]||job.state):"";
    ui("collector-config-state").textContent=settingsDirty?"设置已修改，请先应用":state.verified?`已验证 · ${state.host}`:state.configured?"设置已应用，可先预估耗时":"尚未配置";
    ui("collector-summary").textContent=job?jobLabel:"按轮逐步补充收藏数";
    ui("sidebar-collector").textContent=job?jobLabel:"采集工具已就绪";
    ui("collector-job").hidden=!job;
    ui("collector-view-progress").hidden=!job;
    if(initialStatus){
      if(savedTask()||["running","preparing"].includes(job?.state))ui("collector-panel").open=true;
      if(job?.state==="running")showJob();
      initialStatus=false;
    }
    if(job){
      ui("collector-job-state").textContent=jobLabel;
      const terms=job.query?.tag?.map(tagName)||[];
      (job.query?.exclude_tag||[]).forEach(tag=>terms.push("排除："+tagName(tag)));
      if(job.query?.title?.[0]) terms.push("标题："+job.query.title[0]);
      if(job.query?.category?.[0]) terms.push("分类："+job.query.category[0]);
      ui("collector-job-scope").textContent=`原任务范围：${terms.join(" + ")||"全部作品"} · ${job.host}`;
      const selection=job.selection||{},batched=job.collection_mode==="batch",rounds=job.collection_mode==="rounds",plan=job.round_progress;
      const ordering=selection.rating_priority?"评分从高到低":"年份均衡";
      ui("collector-job-selection").textContent=batched?`分批采集 · ${ordering} · 本批 ${number(job.total)} 条 · 全范围 ${number(selection.scope_total)} 条 · 建立本批时已有收藏数 ${number(selection.scope_known)} 条，另有 ${number(selection.deferred)} 条待后续。`:`全量采集 · ${selection.rating_priority?"评分从高到低":"作品 ID 顺序"} · 进度包含已复用的缓存。`;
      ui("collector-round-plan").hidden=!rounds;
      ui("collector-active-heading").hidden=!rounds;
      ui("collector-save-note").hidden=!rounds;
      ui("collector-save-note").textContent=!state.configured?"已恢复上次进度。先在下方应用源站设置或导入登录文件，再验证原任务的源站访问，即可继续；无需重新搜索。":!state.verified?"请在下方验证原任务的源站访问，验证成功后预估并继续。原队列、轮次和剩余数量均已保存。":"关闭服务后仍保留队列、轮次和剩余数量。重新打开后，应用并验证源站访问即可接着抓取。";
      const active=rounds?plan.active:job,done=job.cached+job.succeeded+job.failed;
      ui("collector-progress").max=Math.max(1,active.total);
      ui("collector-progress").value=active.total?active.cached+active.succeeded+active.failed:(job.state==="completed"?1:0);
      ui("collector-job-counts").textContent=`${rounds?"本轮":batched?"本批":"共"} ${number(active.total)} 条 · 复用缓存 ${number(active.cached)} · 成功 ${number(active.succeeded)} · 失败 ${number(active.failed)} · 待处理 ${number(active.pending)}`;
      if(rounds){
        ui("collector-job-state").textContent=job.state==="awaiting_next"?`第 ${plan.current} 轮已结束 · 等待继续`:jobLabel;
        ui("collector-job-selection").textContent=`分轮抓取 · ${ordering} · 每轮 ${number(selection.batch_size)} 条 · 搜索匹配 ${number(selection.scope_total)} 条，建立计划时跳过 ${number(selection.scope_skipped)} 条缓存。累计成功 ${number(job.succeeded)} 条、复用缓存 ${number(job.cached)} 条、失败 ${number(job.failed)} 条。`;
        ui("round-plan-total").textContent=number(job.total);
        ui("round-plan-done").textContent=number(done);
        ui("round-plan-remaining").textContent=number(job.pending);
        ui("round-plan-rounds").textContent=`${number(plan.completed)} / ${number(plan.total)}`;
        ui("collector-total-progress").max=Math.max(1,job.total);
        ui("collector-total-progress").value=job.total?done:1;
        ui("collector-active-title").textContent=plan.total?`第 ${number(plan.current)} / ${number(plan.total)} 轮`:"无需抓取";
        ui("collector-active-percent").textContent=active.total?`${Math.round((active.cached+active.succeeded+active.failed)/active.total*100)}% 已处理`:"";
        const shown=[...new Set([1,plan.current-1,plan.current,plan.current+1,plan.current+2,plan.total])].filter(value=>value>0&&value<=plan.total).sort((a,b)=>a-b),nodes=[];
        shown.forEach((value,index)=>{
          if(index&&value-shown[index-1]>1){const gap=document.createElement("span");gap.className="round-gap";gap.textContent="…";nodes.push(gap);}
          const item=document.createElement("span"),handled=value<plan.current||(value===plan.current&&active.pending===0);
          item.className="round-milestone"+(handled?" handled":value===plan.current?" current":"");
          item.textContent=`${handled?"✓ ":""}第 ${number(value)} 轮`;
          item.title=value===plan.current?(active.failed?"本轮已处理，有失败项可重试":labels[job.state]):handled?"已处理；失败项可单独重试":"等待手动继续";
          if(value===plan.current)item.setAttribute("aria-current","step");
          nodes.push(item);
        });
        ui("round-milestones").replaceChildren(...nodes);
      }
      const cooling=state.cooldown_until>Date.now()/1000?` 冷却剩余约 ${Math.ceil((state.cooldown_until-Date.now()/1000)/60)} 分钟。`:"";
      ui("collector-job-message").textContent=job.message+cooling;
      renderErrors(job);
    }
    updateControls();
  }
  async function refresh(){
    if(pollBusy||actionBusy||window.catalogStopped||window.catalogMaintenanceActive)return;
    pollBusy=true;
    try{const response=await fetch("/api/collector");if(response.ok){const data=await response.json();if(!actionBusy){state=data;render();}}}
    catch(error){ /* The next local status poll can recover. */ }
    finally{pollBusy=false;}
  }
  async function action(path,payload={}){
    if(actionBusy)return;
    let focusProgress=false;
    invalidatePreview();
    const revision=previewRevision,key=scopeKey();
    actionBusy=true;updateControls();feedback(path==="preview"?"正在统计待抓取数量和预计耗时，不访问源站…":path==="verify"?"正在读取一条需要抓取的作品，请稍候…":"正在处理…");
    try{
      const response=await fetch(`/api/collector/${path}`,{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":token},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)throw new Error(data.error||"操作失败");
      if(path==="preview"){
        if(revision===previewRevision&&key===scopeKey()){renderPreview(data,key,payload.query||"");feedback("预估已生成，确认后才会进行批量抓取。");}
        else feedback("搜索或设置已变化，请重新预估。",true);
      }else if(path==="verify")feedback(data.no_request?data.message:`${data.unavailable?.length?`已跳过 ${number(data.unavailable.length)} 条不可访问作品，失败记录已保留。`:""}验证成功：ID ${data.gid} 的收藏数为 ${number(data.favorite_count)}。现在请预估耗时，再确认采集。`);
      else feedback({settings:"设置已应用。可先预估耗时，再确认采集。",start:data.job?.state==="completed"?data.job.message:"计划已保存，本轮已开始采集，可随时暂停。",next_round:"下一轮已开始。本轮结束后会再次自动停止。",next_batch:data.job?.state==="completed"?data.job.message:"下一批已开始采集，沿用上一批的搜索条件。",pause:"已请求暂停，正在发出的请求会等待结束。",resume:"已按预估继续采集。",retry:"已按预估重试失败项。",cancel:"任务已结束，已采集的收藏数保留。"}[path]||"操作完成。");
      if(path==="settings") {settingsDirty=false;["cookie-member","cookie-hash","cookie-igneous"].forEach(id=>ui(id).value="");}
      if(path!=="preview"&&path!=="verify"){
        state=data;render();
        focusProgress=["start","next_round","next_batch","resume","retry"].includes(path);
      }
      if(path==="verify"&&submittedFilters) await search(responseData?.page||1,false,false);
    }catch(error){feedback(error.message==="Failed to fetch"?"无法连接本地服务，请重新启动。":error.message,true);}
    finally{actionBusy=false;await refresh();updateControls();if(focusProgress)showJob();}
  }
  function settingsPayload(includeCredentials=false){
    const values={interval:Number(ui("collector-interval").value),refresh_days:refreshDays()||state?.refresh_days||7,skip_existing:ui("collector-skip-existing").checked,collection_mode:ui("collector-mode").value,batch_size:batchSize(),rating_priority:ui("collector-rating-priority").checked};
    if(includeCredentials)Object.assign(values,{host:ui("collector-host").value,proxy:ui("collector-proxy").value,ipb_member_id:ui("cookie-member").value,ipb_pass_hash:ui("cookie-hash").value,igneous:ui("cookie-igneous").value});
    return values;
  }
  async function exportCredentials(){
    if(actionBusy)return;
    actionBusy=true;updateControls();feedback("正在生成仅限当前 Windows 用户使用的加密登录文件…");
    try{
      const response=await fetch("/api/credentials/export",{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":token},body:"{}"});
      if(!response.ok){const data=await response.json();throw new Error(data.error||"登录文件导出失败。");}
      const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement("a");
      link.href=url;link.download="exhentai-login.ehcred";document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      feedback("加密登录文件已导出。它只能由这台电脑上的当前 Windows 用户解密。");
    }catch(error){feedback(error.message==="Failed to fetch"?"无法连接本地服务，登录文件没有导出。":error.message,true);}
    finally{actionBusy=false;updateControls();}
  }
  async function importCredentials(file){
    if(actionBusy||!file)return;
    if(file.size<1||file.size>20000){feedback("请选择有效的 .ehcred 登录文件。",true);return;}
    actionBusy=true;updateControls();feedback("正在解密并载入登录信息…");
    try{
      const bytes=new Uint8Array(await file.arrayBuffer());
      let binary="";for(let offset=0;offset<bytes.length;offset+=8192)binary+=String.fromCharCode(...bytes.subarray(offset,offset+8192));
      const response=await fetch("/api/credentials/import",{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":token},body:JSON.stringify({file:btoa(binary),settings:settingsPayload(false)})});
      const data=await response.json();if(!response.ok)throw new Error(data.error||"登录文件导入失败。");
      state=data;settingsDirty=false;ui("collector-host").value=data.host;ui("collector-proxy").value=data.proxy;
      ["cookie-member","cookie-hash","cookie-igneous"].forEach(id=>ui(id).value="");
      invalidatePreview();render();feedback("加密登录信息已载入内存。请先验证并读取一条，再开始采集。");
    }catch(error){feedback(error.message==="Failed to fetch"?"无法连接本地服务，登录文件没有导入。":error.message,true);}
    finally{ui("credential-file").value="";actionBusy=false;await refresh();updateControls();}
  }
  ui("collector-settings").onsubmit=event=>{
    event.preventDefault();
    action("settings",settingsPayload(true));
  };
  ui("credential-export").onclick=exportCredentials;
  ui("credential-import").onclick=()=>ui("credential-file").click();
  ui("credential-file").onchange=event=>importCredentials(event.target.files?.[0]);
  ui("collector-verify").onclick=()=>action("verify",savedTask()?{use_job:true}:{query:submittedFilters.toString()});
  ui("collector-start").onclick=()=>action("preview",{query:submittedFilters.toString(),operation:"start"});
  ["pause","cancel"].forEach(name=>ui(`collector-${name}`).onclick=()=>action(name));
  ["resume","retry"].forEach(name=>ui(`collector-${name}`).onclick=()=>action("preview",{operation:name}));
  ui("collector-next-batch").onclick=()=>action("preview",{operation:"next_batch"});
  ui("collector-next-round").onclick=()=>action("preview",{operation:"next_round"});
  ui("collector-view-progress").onclick=showJob;
  ui("collector-confirm").onclick=()=>{if(preview){const selected=preview;action(selected.operation,{query:selected.queryText,preview_id:selected.preview_id});}};
  ui("collector-dismiss").onclick=()=>{invalidatePreview();feedback("已取消本次预估，未开始抓取。");updateControls();};
  ui("collector-settings").addEventListener("input",()=>{settingsDirty=true;invalidatePreview();render();});
  ui("search-form").addEventListener("input",()=>{invalidatePreview();updateControls();});
  ui("collector-refresh-results").onclick=()=>search(responseData?.page||1,false,false);
  window.collectorUI={updateControls,refresh,invalidatePreview};
  if(location.hash==="#collector-panel")ui("collector-panel").open=true;
  (async()=>{
    try{
      const response=await fetch("/api/status");
      const data=await response.json();token=data.action_token||"";
      await refresh();
      if(state&&!settingsDirty){ui("collector-host").value=state.configured?state.host:(state.job?.host||state.host);ui("collector-proxy").value=state.proxy;ui("collector-interval").value=state.interval;ui("collector-refresh").value=[1,7,30].includes(state.refresh_days)?String(state.refresh_days):"custom";ui("collector-custom-days").value=state.refresh_days;ui("collector-skip-existing").checked=Boolean(state.skip_existing);ui("collector-mode").value=state.collection_mode||"rounds";ui("collector-batch-size").value=[100,300,1000,3000,5000].includes(state.batch_size)?String(state.batch_size):"custom";ui("collector-custom-batch-size").value=state.batch_size||1000;ui("collector-rating-priority").checked=Boolean(state.rating_priority);updateControls();}
    }catch(error){feedback("采集工具尚未连接，请重新启动服务后刷新页面。",true);}
  })();
  setInterval(()=>{if(!document.hidden)refresh();},3000);
})();
