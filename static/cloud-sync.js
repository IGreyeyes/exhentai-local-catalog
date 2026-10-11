"use strict";
(() => {
  const ui=id=>document.getElementById(id);
  let state=null, token="", requestBusy=false, edited=false, loaded=false, polling=false, localError="";
  const fields=["cloud-sync-endpoint","cloud-sync-name","cloud-sync-enabled","cloud-sync-upload","cloud-sync-consent"];
  const display=text=>ui("cloud-sync-message").textContent=text;
  const guided=new URLSearchParams(location.search).get("upload-guide")==="1";
  let guideAcknowledged=false,guideAcknowledging=false,guidePositioned=false;
  async function acknowledgeGuide(){
    if(!guided||!loaded||guideAcknowledged||guideAcknowledging||!window.pywebview?.api?.acknowledge_cloud_upload_guide)return;
    if(document.querySelector('#client-update-dialog[open]'))return;
    guideAcknowledging=true;
    try{const value=await window.pywebview.api.acknowledge_cloud_upload_guide();if(!value.error)guideAcknowledged=true;}
    finally{guideAcknowledging=false;}
  }
  if(guided){ui("cloud-upload-guide").hidden=false;ui("cloud-sync-section").classList.add("cloud-guide-active");}
  ui("cloud-upload-guide-done").onclick=()=>{ui("cloud-upload-guide").hidden=true;history.replaceState(null,"",location.pathname+location.hash);window.dispatchEvent(new Event("catalog-upload-guide-done"));};
  addEventListener("pywebviewready",acknowledgeGuide);
  addEventListener("catalog-client-notes-read",acknowledgeGuide);
  function controls(){
    const blocked=!state||requestBusy||state.busy||!token;
    ui("cloud-sync-save").disabled=blocked;
    ui("cloud-sync-now").disabled=blocked||!state.enabled;
    fields.forEach(id=>ui(id).disabled=requestBusy||Boolean(state?.busy));
  }
  function render(){
    if(!loaded||!edited){
      ui("cloud-sync-endpoint").value=state.endpoint;
      ui("cloud-sync-name").value=state.uploader_name||"";
      ui("cloud-sync-enabled").checked=state.download_enabled;
      ui("cloud-sync-upload").checked=state.upload_enabled;
      ui("cloud-sync-consent").checked=state.upload_enabled;
      loaded=true;
    }
    const labels={review:"新来源：提交待审核",trusted:"可信来源：正常变化自动入库",blocked:"来源已封禁",unregistered:"首次上传时自动登记"};
    ui("cloud-sync-source-state").textContent=(labels[state.source_status]||"待登记")+(state.source_id?" · 来源编号："+state.source_id:"");
    display(localError||state.message);
    const progress=[];
    if(typeof state.upload_total==="number")progress.push(`资料库可提交 ${state.upload_total.toLocaleString("zh-CN")} 条`);
    if(typeof state.upload_confirmed==="number")progress.push(`服务器已确认 ${state.upload_confirmed.toLocaleString("zh-CN")} 条`);
    if(typeof state.session_upload_processed==="number")progress.push(`本次运行累计提交 ${state.session_upload_processed.toLocaleString("zh-CN")} 条快照`);
    if(typeof state.session_download_received==="number")progress.push(`本次运行累计接收 ${state.session_download_received.toLocaleString("zh-CN")} 条公共快照`);
    if(typeof state.upload_remaining==="number")progress.push(`待上传 ${state.upload_remaining.toLocaleString("zh-CN")} 条`);
    if(state.next_sync_at)progress.push(`${state.phase==="continuing"?"下一批":state.phase==="retrying"?"下次重试":"下次检查"}：${new Date(state.next_sync_at*1000).toLocaleTimeString("zh-CN",{timeZone:"Asia/Hong_Kong"})}（UTC+8）`);
    ui("cloud-sync-progress").textContent=progress.join(" · ");
    ui("cloud-sync-last").textContent=state.last_success?"最近完成同步："+new Date(state.last_success*1000).toLocaleString("zh-CN",{timeZone:"Asia/Hong_Kong"})+"（UTC+8） · 刷新搜索或已记录页面查看同步结果。":"尚未完成在线同步。";
    controls();
    if(guided){if(!guidePositioned){ui("cloud-sync-section").scrollIntoView({block:"start"});guidePositioned=true;}acknowledgeGuide().catch(()=>{});}
  }
  async function refresh(){
    if(polling||requestBusy)return;
    polling=true;
    try{
      const response=await fetch("/api/cloud-sync");
      const value=await response.json();if(!response.ok)throw Error(value.error||"无法读取同步状态。");
      state=value;render();
    }catch(error){display("在线同步未连接："+(error.message==="Failed to fetch"?"请确认本地服务正在运行。":error.message));}
    finally{polling=false;}
  }
  async function action(name,payload={}){
    if(requestBusy)return;
    requestBusy=true;localError="";controls();display("正在保存操作…");
    try{
      const response=await fetch("/api/cloud-sync/"+name,{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":token},body:JSON.stringify(payload)});
      const value=await response.json();if(!response.ok)throw Error(value.error||"操作未完成。");
      state=value;edited=false;
      render();
    }catch(error){localError=error.message==="Failed to fetch"?"无法连接本地服务，请刷新后重试。":error.message;display(localError);}
    finally{requestBusy=false;controls();}
  }
  fields.forEach(id=>ui(id).addEventListener("input",()=>{edited=true;localError="";}));
  ui("cloud-sync-save").onclick=()=>action("settings",{
    uploader_name:ui("cloud-sync-name").value.trim(),
    endpoint:ui("cloud-sync-endpoint").value.trim(),download_enabled:ui("cloud-sync-enabled").checked,
    upload_enabled:ui("cloud-sync-upload").checked,consent:ui("cloud-sync-consent").checked,
  });
  ui("cloud-sync-now").onclick=()=>action("now");
  (async()=>{try{const response=await fetch("/api/status");if(!response.ok)throw Error();token=(await response.json()).action_token||"";await refresh();}catch{display("同步设置未连接，请重启本地服务后刷新。");}})();
  setInterval(refresh,3000);
})();
