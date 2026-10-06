"use strict";
(() => {
  const $=id=>document.getElementById(id);
  const number=value=>Number(value||0).toLocaleString("zh-CN");
  const categories={Doujinshi:"同人志",Manga:"漫画","Artist CG":"画师 CG","Game CG":"游戏 CG","Image Set":"图片集","Non-H":"非成人内容",Western:"欧美作品",Cosplay:"角色扮演","Asian Porn":"亚洲写真",Misc:"其他",private:"私有记录"};
  const sources={"exhentai.org":"ExHentai","e-hentai.org":"E-Hentai"};
  const stateNames={none:"未分类",planned:"待看",reading:"在看",watched:"看过",ignored:"不看"};
  const labels=new Map();
  let tags=[],suggestions=[],suggestionIndex=-1,suggestionTimer,suggestionRequest;
  let request,current=null,applied=new URLSearchParams(),loading=false,actionToken="",actionBusy=false;
  let collection="all",jobFilter="";
  const selected=new Set();

  function node(tag,className,text){const result=document.createElement(tag);if(className)result.className=className;if(text!==undefined)result.textContent=text;return result;}
  function dateParts(timestamp){
    if(!timestamp)return {date:"—",time:"尚无采集记录"};
    const value=new Date(timestamp*1000);
    return {date:new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Hong_Kong",year:"numeric",month:"2-digit",day:"2-digit"}).format(value).replaceAll("/","-"),time:new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Hong_Kong",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(value)};
  }
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
  function sourceURL(value){
    if(!value)return null;
    try{const url=new URL(value);return url.protocol==="https:"&&sources[url.hostname]&&!url.username&&!url.password&&!url.port&&/^\/g\/[1-9]\d*\/[0-9a-fA-F]{10}\/$/.test(url.pathname)?url.href:null;}catch(failure){return null;}
  }
  async function post(path,payload){
    const response=await fetch(`/api/records/${path}`,{method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":actionToken},body:JSON.stringify(payload)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||"操作失败");return data;
  }
  async function markOpened(item){
    try{
      await post("opened",{gid:item.gid});await load(current?.page||1,false,false);
    }catch(failure){error(`源站页面已经打开，但“已点开”记录保存失败：${failure.message}`);}
  }
  function trackLink(link,item){link.onclick=()=>{markOpened(item);};}
  async function setState(gids,state){
    if(actionBusy)return;actionBusy=true;busy(loading);error();
    try{
      await post("state",{gids,state});selected.clear();await load(current?.page||1,false,false);
    }catch(failure){error(failure.message);}
    finally{actionBusy=false;busy(false);}
  }
  function stateBadge(item){return node("span",`reading-badge reading-${item.reading_state}`,stateNames[item.reading_state]||"未分类");}
  const namespaceOrder=["artist","group","parody","character","cosplayer","language","female","male","mixed","other","reclass","location","temp"];
  function tagParts(tag){
    const separator=tag.indexOf(":"),namespace=separator>=0?tag.slice(0,separator):"other",original=separator>=0?tag.slice(separator+1):tag,translated=labels.get(tag);
    return {tag,namespace,groupName:translated?.namespace_name||namespace,name:translated?.translated?translated.name:original,matched:current.tags.includes(tag)};
  }
  function tagButton(part){
    const button=node("button",part.matched?"matched":"",part.name);button.type="button";button.title=`${part.groupName}：${part.name}\n${part.tag}\n在已记录作品中筛选此标签`;
    button.onclick=()=>{addTag(part.tag);load(1,true);$("record-filter-title").scrollIntoView({block:"start",behavior:"smooth"});};return button;
  }
  function groupedTags(item){
    const groups=new Map();
    for(const tag of item.tags){const part=tagParts(tag);if(!groups.has(part.namespace))groups.set(part.namespace,{namespace:part.namespace,name:part.groupName,items:[]});groups.get(part.namespace).items.push(part);}
    const priority=namespace=>{const index=namespaceOrder.indexOf(namespace);return index<0?namespaceOrder.length:index;};
    const values=[...groups.values()];
    for(const group of values)group.items.sort((a,b)=>Number(b.matched)-Number(a.matched)||a.name.localeCompare(b.name,"zh-CN"));
    values.sort((a,b)=>Number(b.items.some(item=>item.matched))-Number(a.items.some(item=>item.matched))||priority(a.namespace)-priority(b.namespace)||a.name.localeCompare(b.name,"zh-CN"));
    return values;
  }
  function tagGroupRow(group,items,className=""){
    const row=node("div",`saved-tag-group${className?" "+className:""}`),heading=node("span","saved-tag-namespace",group.name),values=node("div","saved-tag-values");
    heading.title=group.namespace;values.append(...items.map(tagButton));row.append(heading,values);return row;
  }
  function renderTagSummary(item){
    const groups=groupedTags(item),total=item.tags.length,section=node("section","saved-tag-section");
    if(!total)return section;
    const all=node("div","saved-tag-preview saved-tag-all-visible");
    all.append(...groups.map(group=>tagGroupRow(group,group.items)));
    section.append(all);
    return section;
  }
  function recordCard(item){
    const failed=item.collection_status==="failed",hasCount=item.favorite_count!=null;
    const card=node("article",`saved-card${failed?" failed":""}${selected.has(item.gid)?" selected":""}`);const content=node("div","saved-content");
    const cover=node("div","saved-cover"),image=node("img");image.src=item.cover_path||"/cover-placeholder.svg";image.alt="";image.loading="lazy";image.decoding="async";image.width=176;image.height=235;image.onerror=()=>{if(!image.src.endsWith("/cover-placeholder.svg"))image.src="/cover-placeholder.svg";};cover.append(image);card.append(cover);
    const head=node("div","saved-card-head");
    const choose=node("input","record-select");choose.type="checkbox";choose.checked=selected.has(item.gid);choose.setAttribute("aria-label",`选择作品 ID ${item.gid}`);choose.onchange=()=>{choose.checked?selected.add(item.gid):selected.delete(item.gid);render();};head.append(choose);
    head.append(node("span","category-badge",item.metadata_available?(categories[item.category]||item.category||"未分类"):"目录信息缺失"));
    head.append(node("span",`collection-badge ${failed?"failed":"success"}`,failed?(hasCount?"刷新失败":"抓取失败"):"采集成功"));
    if(item.replaced)head.append(node("span","record-state","旧版本"));
    if(item.removed||item.expunged)head.append(node("span","record-state","已移除 / 隐藏"));
    head.append(stateBadge(item));
    if(item.opened_count)head.append(node("span","opened-badge",`已点开 ${number(item.opened_count)} 次`));
    else head.append(node("span","opened-badge","从未点开"));
    head.append(node("span","saved-id",`ID ${item.gid}`));content.append(head);
    const link=sourceURL(item.source_url),title=node(link?"a":"span","saved-title",item.title||item.title_jpn||`作品 ID ${item.gid}`);
    if(link){title.href=link;title.target="_blank";title.rel="noopener noreferrer";trackLink(title,item);}content.append(title);
    if(item.title_jpn&&item.title_jpn!==item.title)content.append(node("p","saved-subtitle",item.title_jpn));
    const details=node("div","saved-metadata");
    if(item.metadata_available){
      if(item.filecount!=null)details.append(node("span","",`${number(item.filecount)} 页`));
      const rating=Number(item.rating);if(item.rating!=null&&Number.isFinite(rating))details.append(node("span","",`☆ ${rating.toFixed(2)} 平均评分`));
      if(item.posted)details.append(node("span","",`发布于 ${dateParts(item.posted).date}`));
      content.append(details);
    }else content.append(node("p","saved-missing-note","当前作品目录中未找到对应标题和标签，已保存的采集结果仍保留。"));
    if(hasCount){
      const own=item.collector_id&&item.collector_id===current.collector_identity?.collector_id;
      const label=item.collector_id?`${own?"我 · ":""}${item.collector_name||"匿名采集者"} · ${item.collector_id.slice(0,8)}`:"未知（旧数据）";
      const author=node("p","saved-collector",`收藏数采集者：${label}`);author.title=item.collector_id?`采集者 ID：${item.collector_id}`:"旧记录未保存采集者身份";content.append(author);
    }
    if(failed){
      const failure=node("section","saved-failure-info");failure.setAttribute("aria-label","抓取失败详情");
      failure.append(node("strong","","失败原因"),node("p","saved-failure-reason",item.error||"源站读取失败"));
      failure.append(node("span","saved-failure-attempts",`已尝试 ${number(item.failure_attempts)} 次`));
      if(hasCount){const previous=dateParts(item.checked_at);failure.append(node("p","saved-previous-success",`保留上次成功采集的收藏数 · ${previous.date} ${previous.time} · ${sources[item.last_success_source]||"来源未知"}`));}
      if(link){const url=node("a","saved-failure-url",link);url.href=link;url.target="_blank";url.rel="noopener noreferrer";trackLink(url,item);failure.append(url);}
      content.append(failure);
    }
    content.append(renderTagSummary(item));card.append(content);
    const count=node("aside","saved-count");count.append(node("strong","",hasCount?number(item.favorite_count):"—"),node("span","",hasCount?(failed?"上次成功的收藏数":"已记录收藏数"):"尚未取得收藏数"));
    const stateControl=node("label","saved-state-control","阅读状态"),stateSelect=node("select");stateSelect.setAttribute("aria-label",`作品 ID ${item.gid} 的阅读状态`);
    for(const [value,label] of Object.entries(stateNames)){const option=node("option","",label);option.value=value;option.selected=value===item.reading_state;stateSelect.append(option);}
    stateSelect.onchange=()=>setState([item.gid],stateSelect.value);stateControl.append(stateSelect);count.append(stateControl);card.append(count);
    const footer=node("div","saved-card-footer"),saved=dateParts(item.recorded_at||item.checked_at),stamp=node("div","",`${failed?"失败记录于":"采集于"} ${saved.date} ${saved.time}`);
    stamp.append(node("span","saved-source",sources[item.source]||"来源未知"));footer.append(stamp);
    const actions=node("div","saved-footer-actions");
    if(link){const open=node("a","","查看源站 ↗");open.href=link;open.target="_blank";open.rel="noopener noreferrer";trackLink(open,item);actions.append(open);}footer.append(actions);
    card.append(footer);return card;
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
      const list=node("div","saved-list");list.append(...current.items.map(recordCard));$("records-list").replaceChildren(list);
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
  window.addEventListener("popstate",restore);
  (async()=>{
    try{const response=await fetch("/api/status");const status=await response.json();if(!response.ok)throw new Error(status.error||"无法读取服务状态");actionToken=status.action_token||"";restore();}
    catch(failure){$("records-connection").textContent="本地服务未连接";error("无法连接本地服务，请双击“启动搜索.cmd”后刷新页面。");busy(false);}
  })();
})();
