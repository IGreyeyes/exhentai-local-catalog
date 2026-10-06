"use strict";
(() => {
  const ui=id=>document.getElementById(id);
  const number=value=>Number(value||0).toLocaleString("zh-CN");
  const date=value=>value?new Date(value*1000).toLocaleDateString("zh-CN",{timeZone:"Asia/Hong_Kong"}):"—";
  const size=bytes=>Number(bytes)>=1024**3?`${(bytes/1024**3).toFixed(2)} GiB`:`${(Number(bytes||0)/1024**2).toFixed(1)} MiB`;
  const element=(tag,className,text="")=>{const node=document.createElement(tag);node.className=className;node.textContent=text;return node;};
  let state=null,token="",busy=false,polling=false,preparedId="",restoreId="",backupsKey="",directoryLoaded=false;
  function message(text,error=false){ui("maintenance-status").textContent=text;ui("maintenance-status").classList.toggle("error",error);}
  function controls(){
    const working=Boolean(busy||state?.busy||window.catalogStopped||!token||!state);
    ["maintenance-backup","backup-full","maintenance-prepare","archive-sha256","maintenance-stop","maintenance-stop-request","backup-directory","backup-save-directory","backup-default-directory","backup-automatic","backup-browse","restore-browse","restore-directory","restore-full","maintenance-check-restore"].forEach(id=>ui(id).disabled=working);
    ui("maintenance-apply").disabled=working||!state?.prepared||(state.prepared.older&&!ui("allow-older-import").checked);
    const restore=state?.prepared_restore;
    const matches=restore&&ui("restore-directory").value.trim()===restore.source&&ui("restore-full").checked===restore.include_catalog;
    ui("restore-confirm").disabled=working||!matches;
    ui("maintenance-restore").disabled=working||!matches||!ui("restore-confirm").checked;
    document.querySelectorAll(".backup-row button").forEach(button=>button.disabled=working);
    const translationTask=state?.kind?.startsWith("translations-")&&state.busy;
    ui("translation-check").disabled=working;
    ui("translation-update").disabled=working;
    ui("translation-check").textContent=translationTask&&state.kind==="translations-check"?"正在检查…":"检查更新";
    ui("translation-update").textContent=translationTask&&state.kind==="translations-update"?"正在更新…":state?.translations?.local?.available?"更新到最新版":"安装中文词库";
  }
  function renderTranslations(){
    const translations=state.translations||{},local=translations.local||{},latest=translations.latest;
    const labels={unchecked:"尚未检查更新",not_installed:"尚未安装中文词库",up_to_date:"已是官方最新版本",update_available:"发现新版词库",unknown:"需要核对词库版本"};
    const task=state.kind?.startsWith("translations-")?state:[...(state.recent_tasks||[])].reverse().find(item=>item.kind?.startsWith("translations-"));
    const running=task?.busy,failed=task?.phase==="failed"||task?.phase==="interrupted";
    ui("translation-state").textContent=running?(task.kind==="translations-check"?"正在检查官方版本…":"正在更新中文词库…"):failed?(task.phase==="interrupted"?"上次词库操作被中断":task.kind==="translations-check"?"检查更新失败":"词库更新失败"):labels[translations.state]||labels.unchecked;
    ui("translation-state").className=`translation-badge ${failed?"failed":running?"working":translations.state||"unchecked"}`;
    ui("translation-local-version").textContent=local.available?(local.release_tag||local.revision?.slice(0,8)||"版本未记录"):"未安装";
    ui("translation-entry-count").textContent=local.available?number(local.entry_count):"—";
    const formatted=value=>{if(!value)return "—";const when=new Date(value);return Number.isNaN(when.getTime())?value:when.toLocaleString("zh-CN",{timeZone:"Asia/Hong_Kong"});};
    ui("translation-local-date").textContent=formatted(local.updated_at||local.downloaded_at);
    ui("translation-latest-version").textContent=latest?.tag||"尚未检查";
    ui("translation-checked-at").textContent=translations.checked_at?formatted(translations.checked_at):"尚未检查";
    ui("translation-feedback").textContent=task?.message||(local.error||"点击「检查更新」查询官方版本；检查本身不会安装词库。");
    ui("translation-feedback").classList.toggle("error",Boolean(failed||local.error));
  }
  function render(){
    if(!state)return;
    renderTranslations();
    ui("backup-path").textContent=state.backup_directory;
    if(!directoryLoaded){ui("backup-directory").value=state.backup_directory;directoryLoaded=true;}
    ui("archive-path").textContent=state.archive_path;
    ui("archive-state").textContent=state.archive_exists?"已发现本地压缩包，准备时会校验文件完整性。":"尚未发现 e-hentai.db.zstd，请先下载并放入项目文件夹。";
    ui("backup-catalog-size").textContent=`（约 ${size(state.catalog_size)}）`;
    ui("backup-automatic").checked=Boolean(state.settings.backup_on_completion);
    const covers=state.cover_cache||{};
    ui("cover-cache-count").textContent=number(covers.cached);ui("cover-cache-size").textContent=size(covers.size_bytes);ui("cover-cache-failed").textContent=number(covers.failed);ui("cover-cache-path").textContent=covers.directory||"—";
    const nextKey=JSON.stringify(state.backups);
    if(backupsKey!==nextKey){
      backupsKey=nextKey;
      const rows=(state.backups||[]).map(backup=>{
        const row=element("div","backup-row");
        const title=element("strong","",backup.include_catalog?"完整备份":"收藏资料与词库备份");
        const detail=element("span","",`${new Date(backup.created_at).toLocaleString("zh-CN",{timeZone:"Asia/Hong_Kong"})} · ${size(backup.size_bytes)} · ${backup.reason}`);
        const path=element("code","",backup.path);
        const button=element("button","button secondary","导入这份");button.type="button";
        button.onclick=()=>{ui("restore-directory").value=backup.path;ui("restore-full").checked=false;invalidateRestore();ui("restore-section").scrollIntoView({block:"start",behavior:"smooth"});ui("restore-directory").focus({preventScroll:true});};
        row.append(title,detail,path,button);return row;
      });
      ui("backup-list").replaceChildren(...(rows.length?rows:[element("p","backup-empty","这个位置还没有备份，点击「立即备份」保存第一份。") ]));
    }
    const candidate=state.prepared;
    ui("prepared-import").hidden=!candidate;
    if(candidate){
      if(preparedId!==candidate.id){preparedId=candidate.id;ui("allow-older-import").checked=false;}
      ui("import-old-count").textContent=number(candidate.old.gallery_count);ui("import-new-count").textContent=number(candidate.new.gallery_count);
      ui("import-old-tags").textContent=number(candidate.old.tag_count);ui("import-new-tags").textContent=number(candidate.new.tag_count);
      ui("import-old-date").textContent=date(candidate.old.latest_posted);ui("import-new-date").textContent=date(candidate.new.latest_posted);
      const warnings=[];if(candidate.older)warnings.push("候选目录的最新作品日期比当前目录旧，请核对下载文件。");if(candidate.fewer_records)warnings.push("候选目录的作品记录数较少，请确认这是你要使用的快照。");
      ui("import-warning").textContent=warnings.join(" ");ui("import-warning").hidden=!warnings.length;ui("allow-older-label").hidden=!candidate.older;
    }
    const restore=state.prepared_restore;
    if(restore&&restoreId!==restore.id){
      restoreId=restore.id;ui("restore-directory").value=restore.source;ui("restore-full").checked=restore.include_catalog;ui("restore-confirm").checked=false;
    }
    const matches=restore&&ui("restore-directory").value.trim()===restore.source&&ui("restore-full").checked===restore.include_catalog;
    ui("prepared-restore").hidden=!matches;
    if(restore){
      ui("restore-source").textContent=restore.source;
      ui("restore-favorites").textContent=number(restore.summary.favorites);ui("restore-reading").textContent=number(restore.summary.reading_states);ui("restore-jobs").textContent=number(restore.summary.jobs);
      ui("restore-scope").textContent=restore.include_catalog?"恢复范围：收藏资料、采集进度、备份内词库和作品目录。":"恢复范围：收藏资料、采集进度和备份内词库。继续使用当前作品目录。";
    }
    message(state.message,state.phase==="failed"||state.phase==="interrupted");
    controls();
  }
  async function refresh(){
    if(polling||busy||window.catalogStopped)return;
    polling=true;
    try{
      const response=await fetch("/api/maintenance");if(!response.ok)throw new Error("无法读取维护信息，请重启服务后刷新。");
      state=await response.json();if(!busy)render();
      ui("connection").replaceChildren(element("i",""),document.createTextNode(state.busy?" 维护任务进行中":" 本地服务已连接"));
    }catch(error){message(error.message==="Failed to fetch"?"无法连接本地服务，请先双击「启动搜索.cmd」再刷新。":error.message,true);ui("connection").textContent="本地服务未连接";}
    finally{polling=false;}
  }
  async function action(name,payload={}){
    if(busy)return false;
    busy=true;controls();message(name==="stop"?"正在暂停采集并等待当前请求结束，请稍候…":"正在提交维护操作…");
    try{
      const response=await fetch(`/api/maintenance/${name}`,{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":token},body:JSON.stringify(payload)});
      const result=await response.json();if(!response.ok)throw new Error(result.error||"操作失败");
      if(name==="stop"){
        window.catalogStopped=true;ui("connection").textContent="本地服务正在退出";
        message("服务已停止，采集进度已保存。再次使用时，请双击「启动搜索.cmd」，然后刷新本页。");
        document.querySelectorAll("button,input,select").forEach(control=>control.disabled=true);ui("maintenance-stop-confirm").hidden=true;
      }else{
        state={...result,cover_cache:result.cover_cache||state?.cover_cache};render();
        if(name==="settings"){
          ui("backup-directory").value=state.backup_directory;
          message("备份设置已保存。当前备份位置："+state.backup_directory);
        }
      }
      return true;
    }catch(error){message(error.message==="Failed to fetch"?"无法连接本地服务，请先双击启动搜索。":error.message,true);return false;}
    finally{busy=false;if(!window.catalogStopped)controls();}
  }
  function invalidateRestore(){ui("restore-confirm").checked=false;ui("prepared-restore").hidden=true;controls();}
  async function chooseDirectory(purpose){
    if(busy)return;
    const input=ui(purpose==="backup"?"backup-directory":"restore-directory");
    busy=true;controls();message("请在打开的文件夹选择窗口中完成选择…");
    try{
      const response=await fetch("/api/maintenance/pick-directory",{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":token},body:JSON.stringify({purpose,initial:input.value.trim()})});
      const result=await response.json();if(!response.ok)throw new Error(result.error||"无法选择文件夹");
      if(result.path){input.value=result.path.replaceAll("/","\\");if(purpose==="restore")invalidateRestore();message(purpose==="backup"?"已选择文件夹。点击「保存位置」或「立即备份」使用这个位置。":"已选择备份文件夹，点击「检查备份」继续。");}
      else message("已取消文件夹选择。");
    }catch(error){message(error.message,true);}
    finally{busy=false;controls();}
  }
  ui("backup-browse").onclick=()=>chooseDirectory("backup");
  ui("restore-browse").onclick=()=>chooseDirectory("restore");
  ui("backup-save-directory").onclick=()=>action("settings",{backup_directory:ui("backup-directory").value.trim()});
  ui("backup-default-directory").onclick=()=>action("settings",{backup_directory:""});
  ui("maintenance-backup").onclick=async()=>{
    if(ui("backup-directory").value.trim()!==state?.backup_directory&&!await action("settings",{backup_directory:ui("backup-directory").value.trim()}))return;
    action("backup",{include_catalog:ui("backup-full").checked});
  };
  ui("backup-automatic").onchange=()=>action("settings",{backup_on_completion:ui("backup-automatic").checked});
  ui("restore-directory").oninput=invalidateRestore;ui("restore-full").onchange=invalidateRestore;ui("restore-confirm").onchange=controls;
  ui("maintenance-check-restore").onclick=()=>{ui("restore-confirm").checked=false;action("prepare-restore",{path:ui("restore-directory").value.trim(),include_catalog:ui("restore-full").checked});};
  ui("maintenance-restore").onclick=()=>{if(state?.prepared_restore&&ui("restore-confirm").checked)action("restore",{prepared_id:state.prepared_restore.id});};
  ui("maintenance-prepare").onclick=()=>action("prepare",{sha256:ui("archive-sha256").value.trim()});
  ui("translation-check").onclick=()=>action("translations-check");
  ui("translation-update").onclick=()=>action("translations-update");
  ui("allow-older-import").onchange=controls;
  ui("maintenance-apply").onclick=()=>{if(state?.prepared)action("apply",{prepared_id:state.prepared.id,allow_older:ui("allow-older-import").checked});};
  function showStop(){ui("maintenance-stop-confirm").hidden=false;ui("maintenance-stop-confirm").scrollIntoView({block:"nearest",behavior:"smooth"});}
  ui("maintenance-stop-request").onclick=showStop;
  ui("maintenance-stop-cancel").onclick=()=>ui("maintenance-stop-confirm").hidden=true;
  ui("maintenance-stop").onclick=()=>action("stop");
  if(location.hash==="#stop")showStop();
  controls();
  (async()=>{try{const response=await fetch("/api/status");if(!response.ok)throw new Error();const data=await response.json();token=data.action_token||"";await refresh();}catch(error){message("维护工具未连接，请重启服务后刷新。",true);}})();
  setInterval(refresh,3000);
})();
