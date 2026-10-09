"use strict";
(() => {
  const $=id=>document.getElementById(id);
  const number=value=>Number(value||0).toLocaleString("zh-CN");
  const views=["minimal","compact","extended","thumbnails"];
  let view="extended",legacyView=null,viewSaveQueue=Promise.resolve(),viewSaveRevision=0;
  try{
    const saved=localStorage.getItem("records-view");
    if(saved==="minimal-tags"){view="minimal";legacyView=view;localStorage.setItem("records-view",view);}
    else if(views.includes(saved)){view=saved;legacyView=view;}
  }catch(failure){ /* Storage can be disabled. */ }
  const labels=new Map();
  let tags=[],suggestions=[],suggestionIndex=-1,suggestionTimer,suggestionRequest;
  let request,current=null,applied=new URLSearchParams(),loading=false,actionToken="",actionBusy=false;
  let collection="all",jobFilter="";
  const selected=new Set();

  function node(tag,className,text){const result=document.createElement(tag);if(className)result.className=className;if(text!==undefined)result.textContent=text;return result;}
  const dateParts=window.galleryCards.dateParts;
  function remember(data={}){Object.entries(data).forEach(([key,value])=>labels.set(key,value));}
  function tagName(tag){const translated=labels.get(tag);return translated?.translated?`${translated.namespace_name}：${translated.name}`:tag;}
  function error(text=""){$("records-error").textContent=text;$("records-error").hidden=!text;}
  function busy(value){
    loading=value;$("records-list").setAttribute("aria-busy",String(value));
    $("filter-records").disabled=value;$("refresh-records").disabled=value;
    $("records-previous").disabled=value||!current||current.page<=1;
    $("records-next").disabled=value||!current||current.page>=current.pages;
    $("records-jump").disabled=value||!current;
    $("apply-bulk-state").disabled=value||actionBusy||!selected.size;
  }
  function hideSuggestions(){$("record-suggestions").hidden=true;$("record-tag").setAttribute("aria-expanded","false");$("record-tag").removeAttribute("aria-activedescendant");suggestionIndex=-1;}
  function renderTags(){
    $("record-chips").replaceChildren(...tags.map(tag=>{
      const chip=node("span","tag-chip");chip.append(node("span","",tagName(tag)));
      if(labels.get(tag)?.translated)chip.append(node("small","tag-original",tag));
      const remove=node("button","","×");remove.type="button";remove.setAttribute("aria-label",`移除标签 ${tagName(tag)} ${tag}`);
      remove.onclick=()=>{tags=tags.filter(value=>value!==tag);renderTags();};chip.append(remove);return chip;
    }));
  }
  function addTag(value){
    value=value.trim().toLowerCase().replace(/\$$/,"");if(!value)return;
    if(tags.length>=12&&!tags.includes(value)){error("最多同时筛选 12 个标签。");return;}
    if(!tags.includes(value))tags.push(value);
    $("record-tag").value="";clearTimeout(suggestionTimer);suggestionRequest?.abort();hideSuggestions();renderTags();
  }
  async function suggest(){
    suggestionRequest?.abort();const q=$("record-tag").value.trim();if(!q){hideSuggestions();return;}
    const active=new AbortController();suggestionRequest=active;
    try{
      const response=await fetch(`/api/tags?q=${encodeURIComponent(q)}`,{signal:active.signal});if(!response.ok)return;
      const data=await response.json();if(active.signal.aborted||q!==$("record-tag").value.trim())return;
      remember(data.labels);suggestions=data.items.filter(tag=>!tags.includes(tag));suggestionIndex=-1;
      $("record-suggestions").replaceChildren(...suggestions.map((tag,index)=>{
        const option=node("li");option.id=`record-option-${index}`;option.setAttribute("role","option");option.setAttribute("aria-selected","false");
        option.append(node("span","suggestion-name",tagName(tag)));if(labels.get(tag)?.translated)option.append(node("small","suggestion-original",tag));
        option.onpointerdown=event=>{event.preventDefault();addTag(tag);$("record-tag").focus();};return option;
      }));
      $("record-suggestions").hidden=!suggestions.length;$("record-tag").setAttribute("aria-expanded",String(Boolean(suggestions.length)));
    }catch(failure){if(failure.name!=="AbortError")hideSuggestions();}
  }
  $("record-tag").oninput=()=>{clearTimeout(suggestionTimer);suggestionTimer=setTimeout(suggest,180);};
  $("record-tag").onkeydown=event=>{
    const open=!$("record-suggestions").hidden&&suggestions.length;
    if(open&&(event.key==="ArrowDown"||event.key==="ArrowUp")){
      event.preventDefault();suggestionIndex=suggestionIndex<0?(event.key==="ArrowDown"?0:suggestions.length-1):(suggestionIndex+(event.key==="ArrowDown"?1:-1)+suggestions.length)%suggestions.length;
      [...$("record-suggestions").children].forEach((option,index)=>option.setAttribute("aria-selected",String(index===suggestionIndex)));
      $("record-tag").setAttribute("aria-activedescendant",`record-option-${suggestionIndex}`);$("record-suggestions").children[suggestionIndex].scrollIntoView({block:"nearest"});
    }else if(event.key==="Enter"&&$("record-tag").value.trim()){
      event.preventDefault();addTag(open&&suggestionIndex>=0?suggestions[suggestionIndex]:$("record-tag").value);
    }else if(event.key==="Escape")hideSuggestions();
  };
  document.addEventListener("pointerdown",event=>{if(!$("record-tag-box").contains(event.target))hideSuggestions();});

  function filters(){
    const params=new URLSearchParams();tags.forEach(tag=>params.append("tag",tag));
    const text=$("record-query").value.trim();if(text)params.set("q",text);
    params.set("sort",$("record-sort").value);params.set("age",$("record-age").value);
    params.set("state",$("record-state").value);params.set("opened",$("record-opened").value);
    params.set("collection",collection);if(jobFilter)params.set("job_id",jobFilter);
    if($("record-source").value)params.set("source",$("record-source").value);
    return params;
  }
  async function post(path,payload){
    const response=await fetch(`/api/records/${path}`,{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":actionToken},body:JSON.stringify(payload),keepalive:path==="view"});
    const data=await response.json();if(!response.ok)throw new Error(data.error||"操作失败");return data;
  }
  async function markOpened(item){
    try{
      await post("opened",{gid:item.gid});await load(current?.page||1,false,false);
    }catch(failure){error(`源站页面已经打开，但“已点开”记录保存失败：${failure.message}`);}
  }
  async function setState(gids,state){
    if(actionBusy)return;actionBusy=true;busy(loading);error();
    try{
      await post("state",{gids,state});selected.clear();await load(current?.page||1,false,false);
    }catch(failure){error(failure.message);}
    finally{actionBusy=false;busy(false);}
  }
  function recordCard(item){
    return window.galleryCards.render(item,{
      view,tags:current.tags,labels,collectorIdentity:current.collector_identity,selected,recordControls:true,
      tagHint:"在已记录作品中筛选此标签",
      onTag:tag=>{addTag(tag);load(1,true);$("record-filter-title").scrollIntoView({block:"start",behavior:"smooth"});},
      onSelect:(item,checked)=>{checked?selected.add(item.gid):selected.delete(item.gid);render();},
      onOpen:markOpened,onState:setState,
    });
  }
  function render(){
    const {summary}=current,latest=dateParts(summary.last_recorded_at||summary.last_checked_at);
    $("saved-total").textContent=number(summary.total);$("nav-count").textContent=number(summary.total);$("saved-recent").textContent=number(summary.recent_7_days);
    $("saved-latest").textContent=latest.date;$("saved-latest-time").textContent=latest.time;
    $("saved-failed").textContent=number(summary.failed);
    const collectionChoices=[{value:"all",label:"全部记录",count:summary.total},{value:"success",label:"采集成功",count:summary.successful??summary.total},{value:"failed",label:"抓取失败",count:summary.failed||0}];
    $("record-collection-tabs").replaceChildren(...collectionChoices.map(item=>{
      const button=node("button",`record-collection-button${item.value===collection?" active":""}${item.value==="failed"?" failed":""}`);button.type="button";button.dataset.collection=item.value;button.setAttribute("aria-pressed",String(item.value===collection));
      button.append(node("span","",item.label),node("strong","",number(item.count)));
      button.onclick=()=>{collection=item.value;selected.clear();if(collection!=="failed")jobFilter="";if(collection==="failed")$("record-sort").value="recorded_desc";load();};return button;
    }));
    $("record-job-filter").hidden=!jobFilter;
    $("record-job-label").textContent=jobFilter?`只查看采集任务 #${jobFilter} 的失败作品`:"";
    $("records-connection").replaceChildren(node("i"),document.createTextNode("本地记录已读取"));
    $("record-summary").textContent=`筛选出 ${number(current.total)} 条 · 全部已记录 ${number(summary.total)} 条 · ${(current.elapsed_ms/1000).toFixed(2)} 秒`;
    const overview=[{value:"all",label:"全部记录",count:summary.total},{value:"none",label:"未分类",count:summary.states.none},{value:"planned",label:"待看",count:summary.states.planned},{value:"reading",label:"在看",count:summary.states.reading},{value:"watched",label:"看过",count:summary.states.watched},{value:"ignored",label:"不看",count:summary.states.ignored},{opened:"yes",label:"已点开",count:summary.opened},{opened:"no",label:"从未点开",count:summary.total-summary.opened}];
    $("record-state-overview").replaceChildren(...overview.map(item=>{const active=item.opened?$("record-opened").value===item.opened:item.value==="all"?$("record-state").value==="all"&&$("record-opened").value==="all":$("record-state").value===item.value;const button=node("button",`state-summary-button${active?" active":""}`);button.type="button";button.append(node("span","",item.label),node("strong","",number(item.count)));button.onclick=()=>{if(item.opened)$("record-opened").value=item.opened;else if(item.value==="all"){$("record-state").value="all";$("record-opened").value="all";}else $("record-state").value=item.value;load();};return button;}));
    renderBulk();
    if(!current.total){
      const empty=node("div","empty-state");empty.append(node("div","empty-icon","✓"));
      if(!summary.total){
        empty.append(node("h3","","还没有已记录的作品"),node("p","","在标签搜索页开始采集后，成功和失败的作品都会保存在这里。"));
        const link=node("a","button primary records-empty-link","前往标签搜索");link.href="/";empty.append(link);
      }else{
        empty.append(node("h3","",collection==="failed"&&!summary.failed?"当前没有抓取失败的作品":"没有符合当前筛选的记录"),node("p","",collection==="failed"&&!summary.failed?"失败作品会在这里保留；重试成功后自动移出失败列表。":"可以调整采集状态、标签、关键词、来源或时间范围。"));
        const clear=node("button","button secondary records-empty-link","查看全部记录");clear.onclick=clearFilters;empty.append(clear);
      }
      $("records-list").replaceChildren(empty);$("records-pagination").hidden=true;
    }else{
      const list=node("div",`saved-list view-${view}`);list.append(...current.items.map(recordCard));$("records-list").replaceChildren(list);
      $("records-pagination").hidden=false;$("records-range").textContent=`显示 ${number((current.page-1)*current.limit+1)}–${number((current.page-1)*current.limit+current.items.length)} / ${number(current.total)} 条`;
      $("records-page").value=current.page;$("records-page").max=current.pages;$("records-pages").textContent=`/ ${number(current.pages)} 页`;
    }
  }
  function renderBulk(){
    $("record-bulk").hidden=!current?.total;
    $("selected-record-count").textContent=`已选 ${number(selected.size)} 条`;
    const pageIds=(current?.items||[]).map(item=>item.gid),selectedOnPage=pageIds.filter(gid=>selected.has(gid)).length;
    $("select-record-page").checked=Boolean(pageIds.length&&selectedOnPage===pageIds.length);
    $("select-record-page").indeterminate=selectedOnPage>0&&selectedOnPage<pageIds.length;
    $("apply-bulk-state").disabled=loading||actionBusy||!selected.size;
  }
  async function load(page=1,fresh=true,updateHistory=true){
    if(fresh){if($("record-tag").value.trim())addTag($("record-tag").value);applied=filters();}
    request?.abort();const active=new AbortController();request=active;
    const params=new URLSearchParams(applied);params.set("page",String(Math.max(1,Number(page)||1)));
    error();hideSuggestions();busy(true);$("records-pagination").hidden=true;
    $("records-list").replaceChildren(node("div","loading","正在读取已保存的作品…"));
    try{
      const response=await fetch(`/api/records?${params}`,{signal:active.signal});const data=await response.json();
      if(!response.ok)throw new Error(data.error||"记录读取失败，请重试。");if(active.signal.aborted)return;
      remember(data.tag_labels);
      const sent=applied.getAll("tag");if(tags.length===sent.length&&tags.every((tag,index)=>tag===sent[index])){tags=[...data.tags];renderTags();}
      applied.delete("tag");params.delete("tag");data.tags.forEach(tag=>{applied.append("tag",tag);params.append("tag",tag);});
      current=data;render();params.set("page",String(data.page));
      const url="/records?"+params;
      if(updateHistory&&location.pathname+location.search!==url)history.pushState(null,"",url);
    }catch(failure){
      if(failure.name==="AbortError")return;
      current=null;$("records-list").replaceChildren();$("record-summary").textContent="本次读取未完成";
      error(failure.message==="Failed to fetch"?"无法连接本地服务。请双击“启动搜索.cmd”，然后刷新页面。":failure.message);
    }finally{if(request===active)busy(false);}
  }
  function clearFilters(){tags=[];collection="all";jobFilter="";selected.clear();$("records-form").reset();renderTags();hideSuggestions();load(1,true);}
  function pageTo(value){load(value,false);$("records-heading").scrollIntoView({block:"start"});}
  function restore(){
    $("records-form").reset();clearTimeout(suggestionTimer);suggestionRequest?.abort();hideSuggestions();
    const params=new URLSearchParams(location.search);tags=params.getAll("tag").slice(0,12);renderTags();
    collection=["all","success","failed"].includes(params.get("collection"))?params.get("collection"):"all";jobFilter=params.get("job_id")||"";
    $("record-query").value=params.get("q")||"";
    selected.clear();
    for(const [id,key,fallback] of [["record-sort","sort",collection==="failed"?"recorded_desc":"favorites_desc"],["record-source","source",""],["record-age","age","all"],["record-state","state","all"],["record-opened","opened","all"]]){$(id).value=params.get(key)||fallback;if(!$(id).value&&fallback)$(id).value=fallback;}
    load(Number(params.get("page"))||1,true,false);
  }
  $("records-form").onsubmit=event=>{event.preventDefault();load();};
  $("clear-record-filters").onclick=clearFilters;
  $("clear-record-job").onclick=()=>{jobFilter="";load();};
  $("refresh-records").onclick=()=>load(current?.page||1,false,false);
  $("records-previous").onclick=()=>pageTo(current.page-1);$("records-next").onclick=()=>pageTo(current.page+1);
  $("records-jump").onclick=()=>pageTo($("records-page").value);
  $("records-page").onkeydown=event=>{if(event.key==="Enter"){event.preventDefault();pageTo(event.target.value);}};
  $("select-record-page").onchange=event=>{for(const item of current?.items||[]){event.target.checked?selected.add(item.gid):selected.delete(item.gid);}render();};
  $("clear-record-selection").onclick=()=>{selected.clear();render();};
  $("apply-bulk-state").onclick=()=>setState([...selected],$("bulk-record-state").value);
  $("record-view").value=view;
  $("record-view").disabled=true;
  $("record-view").onchange=()=>{
    const chosen=$("record-view").value,revision=++viewSaveRevision;view=chosen;
    try{localStorage.setItem("records-view",view);}catch(failure){}
    if(current)render();$("record-view").disabled=true;
    viewSaveQueue=viewSaveQueue.then(()=>post("view",{view:chosen})).then(()=>{
      error();
    }).catch(failure=>error(`显示方式已切换，但尚未保存到本地资料库：${failure.message}。请重新选择后再退出。`)).finally(()=>{if(revision===viewSaveRevision)$("record-view").disabled=false;});
  };
  window.addEventListener("popstate",restore);
  (async()=>{
    try{
      const [response,preferencesResponse]=await Promise.all([fetch("/api/status"),fetch("/api/preferences")]);
      const status=await response.json(),preferences=await preferencesResponse.json();
      if(!response.ok||!preferencesResponse.ok)throw new Error(status.error||preferences.error||"无法读取服务状态");
      actionToken=status.action_token||"";
      if(views.includes(preferences.records_view))view=preferences.records_view;
      else if(legacyView)await post("view",{view:legacyView});
      $("record-view").value=view;$("record-view").disabled=false;
      try{localStorage.setItem("records-view",view);}catch(failure){}
      restore();
    }
    catch(failure){$("records-connection").textContent="本地服务未连接";error("无法连接本地服务，请双击“启动搜索.cmd”后刷新页面。");busy(false);}
  })();
})();
