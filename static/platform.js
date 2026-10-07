"use strict";
(() => {
  const desktop = () => Boolean(window.pywebview?.api?.save_attachment);
  window.catalogSaveAttachment = async (endpoint, filename, token) => {
    if (desktop()) {
      const result = await window.pywebview.api.save_attachment(endpoint);
      if (result.error) throw new Error(result.error);
      return result;
    }
    const response = await fetch(endpoint, {method: "POST", headers: {"Content-Type": "application/json", "X-Catalog-Token": token}, body: "{}"});
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.error || "文件导出失败。");
    }
    const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement("a");
    link.href = url;
    link.download = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || filename;
    document.body.append(link);link.click();link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return {cancelled: false, record_count: response.headers.get("X-Favorite-Record-Count")};
  };
  window.catalogExportLocation = () => desktop() ? "文件已保存到你选择的位置。" : "请在浏览器下载列表查看。";
  window.addEventListener("pywebviewready", () => {
    window.catalogDesktop = true;
    const confirmation = document.querySelector("#maintenance-stop-confirm p");
    if (confirmation) confirmation.textContent = "当前采集会暂停，已抓取的数据会保留。再次使用时，重新打开应用。登录信息只存在内存中，下次可重新填写或导入加密登录文件。";
    for (const button of document.querySelectorAll("#maintenance-stop-request, #maintenance-stop")) {
      button.textContent = button.id === "maintenance-stop" ? "确认停止服务" : "停止服务";
    }
    initializeClientUpdates();
  });

  async function initializeClientUpdates() {
    const api=window.pywebview.api;
    if(!api.client_info||document.getElementById("client-update-dialog"))return;
    const make=(tag,className,text)=>{const element=document.createElement(tag);element.className=className||"";if(text!==undefined)element.textContent=text;return element;};
    function appendInline(parent,text){
      for(const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)){
        if(part.startsWith("**")&&part.endsWith("**"))parent.append(make("strong","",part.slice(2,-2)));
        else if(part.startsWith("`")&&part.endsWith("`"))parent.append(make("code","",part.slice(1,-1)));
        else parent.append(document.createTextNode(part));
      }
    }
    function renderNotes(text){
      notes.replaceChildren();let list=null;
      for(const line of String(text||"").replace(/\r\n?/g,"\n").split("\n")){
        const value=line.trim();if(!value){list=null;continue;}
        const item=value.match(/^(?:(\d+)[.)、]\s+|[-*+]\s+)(.*)$/);
        if(item){
          const tag=item[1]?"ol":"ul";
          if(!list||list.tagName.toLowerCase()!==tag){list=make(tag,"client-note-list");notes.append(list);}
          const entry=make("li");appendInline(entry,item[2]);list.append(entry);
        }else{
          list=null;const heading=value.match(/^#{1,6}\s+(.*)$/),entry=make(heading?"h3":"p");appendInline(entry,heading?heading[1]:value);notes.append(entry);
        }
      }
      notes.hidden=!notes.childNodes.length;
    }
    const controls=make("div","client-update-controls"),version=make("span","client-version"),check=make("button","button secondary","检查客户端更新"),update=make("button","button secondary","更新客户端");
    check.id="client-check-update";update.id="client-download-update";update.disabled=true;
    controls.append(version,check,update);(document.querySelector(".topbar-actions")||document.querySelector("main")).append(controls);
    const dialog=make("dialog","client-update-dialog");dialog.id="client-update-dialog";
    const header=make("div","client-update-header"),icon=make("span","client-update-icon","✓"),heading=make("div","client-update-heading"),eyebrow=make("p","client-update-eyebrow","客户端更新");
    const title=make("h2"),body=make("div","client-update-body"),notes=make("div","client-release-notes"),message=make("p","client-update-message"),progress=make("progress"),actions=make("div","client-update-actions"),close=make("button","button secondary","关闭"),install=make("button","button primary","下载并更新");
    title.id="client-update-title";message.id="client-update-message";dialog.setAttribute("aria-labelledby",title.id);dialog.setAttribute("aria-describedby",message.id);icon.setAttribute("aria-hidden","true");
    progress.max=100;progress.hidden=true;install.hidden=true;install.id="client-install-update";close.id="client-notes-read";
    heading.append(eyebrow,title);header.append(icon,heading);body.append(notes,message,progress);actions.append(close,install);dialog.append(header,body,actions);document.body.append(dialog);
    let info=null,release=null,currentNotes=false,busy=false,phase="idle",poll=null;
    const show=()=>{if(!dialog.open)dialog.showModal();};
    const result=async promise=>{const value=await promise;if(value.error)throw new Error(value.error);return value;};
    function buttons(){check.disabled=busy||phase==="ready";update.disabled=busy||!release?.available||!info?.can_install;install.disabled=busy;install.hidden=!release?.available||!info?.can_install||currentNotes;update.textContent=phase==="ready"?"安装客户端更新":"更新客户端";install.textContent=phase==="ready"?"安装并重新打开":"下载并更新";close.classList.toggle("primary",currentNotes);close.classList.toggle("secondary",!currentNotes);}
    function watchUpdate(){
      poll=setInterval(async()=>{
        try{
          const state=await result(api.client_update_status());phase=state.phase;message.textContent=state.message;progress.value=state.percent;
          if(["ready","error"].includes(phase)){clearInterval(poll);poll=null;busy=false;progress.hidden=true;buttons();if(phase==="error")message.classList.add("error");show();}
        }catch(error){clearInterval(poll);poll=null;busy=false;buttons();fail(error);}
      },750);
    }
    function showRelease(){currentNotes=false;icon.textContent=release.available?"↑":"✓";close.textContent="关闭";title.textContent=release.available?`发现新版 v${release.latest_version}`:`当前版本 v${info.version}`;renderNotes(release.notes);message.textContent=release.available?(info.can_install?"更新将保留本地资料；安装时安全退出，并自动重新打开客户端。":"当前正在运行源码，请用 GitHub Desktop 更新源码或从 Releases 下载完整客户端。"):"当前客户端已是最新发布版本，或比最新发布版本更新。";show();buttons();}
    function fail(error){message.textContent=error.message||String(error);message.classList.add("error");show();}
    close.onclick=async()=>{
      if(currentNotes){try{await result(api.acknowledge_client_notes());info.show_notes=false;}catch(error){fail(error);return;}}
      dialog.close();
    };
    check.onclick=async()=>{
      currentNotes=false;icon.textContent="↻";close.textContent="关闭";title.textContent="正在检查客户端更新";renderNotes("");message.classList.remove("error");message.textContent="正在连接本项目的 GitHub Releases…";busy=true;show();buttons();
      try{release=await result(api.check_client_update());showRelease();}catch(error){release=null;fail(error);}finally{busy=false;buttons();}
    };
    const performUpdate=async()=>{
      currentNotes=false;message.classList.remove("error");show();busy=true;buttons();
      try{
        if(phase==="ready"){
          await result(api.install_client_update());message.textContent="正在保存采集进度、退出并安装更新，请稍候…";watchUpdate();return;
        }
        await result(api.prepare_client_update());progress.hidden=false;
        watchUpdate();
      }catch(error){busy=false;buttons();fail(error);}
    };
    install.onclick=performUpdate;update.onclick=()=>{showRelease();if(phase==="ready")message.textContent="新版已校验通过，可以安装并重新打开。";};
    try{
      info=await result(api.client_info());version.textContent=`v${info.version}`;buttons();
      if(info.show_notes){currentNotes=true;title.textContent=`已更新到 v${info.version}`;renderNotes(info.notes);message.textContent="点击「已读」后，本版本的说明不再自动弹出。";close.textContent="已读";buttons();show();}
    }catch(error){title.textContent="客户端更新";fail(error);}
  }
})();
