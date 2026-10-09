"use strict";
(() => {
  const number=value=>Number(value||0).toLocaleString("zh-CN");
  const categories={Doujinshi:"同人志",Manga:"漫画","Artist CG":"画师 CG","Game CG":"游戏 CG","Image Set":"图片集","Non-H":"非成人内容",Western:"欧美作品",Cosplay:"角色扮演","Asian Porn":"亚洲写真",Misc:"其他",private:"私有记录"};
  const categoryStyles={Doujinshi:"doujinshi",Manga:"manga","Artist CG":"artist-cg","Game CG":"game-cg","Image Set":"image-set","Non-H":"non-h",Western:"western",Cosplay:"cosplay","Asian Porn":"asian-porn",Misc:"misc"};
  const sources={"exhentai.org":"ExHentai","e-hentai.org":"E-Hentai"};
  const stateNames={none:"未分类",planned:"待看",reading:"在看",watched:"看过",ignored:"不看"};
  function node(tag,className,text){const result=document.createElement(tag);if(className)result.className=className;if(text!==undefined)result.textContent=text;return result;}
  function dateParts(timestamp){
    if(!timestamp)return {date:"—",time:"尚无采集记录"};
    const value=new Date(timestamp*1000);
    return {date:new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Hong_Kong",year:"numeric",month:"2-digit",day:"2-digit"}).format(value).replaceAll("/","-"),time:new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Hong_Kong",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(value)};
  }
  function sourceURL(value){
    if(!value)return null;
    try{const url=new URL(value);return url.protocol==="https:"&&sources[url.hostname]&&!url.username&&!url.password&&!url.port&&/^\/g\/[1-9]\d*\/[0-9a-fA-F]{10}\/$/.test(url.pathname)?url.href:null;}catch(failure){return null;}
  }
  function renderRatingGuide(rules){
    const content=document.querySelector("#rating-system-guide .rating-guide-content");
    if(!content||!rules)return;
    const minimum=rules.minimum_ratings;
    const intro=node("p","",`收藏/评分 = 已保存收藏数 ÷ 评分人数。至少 ${minimum} 人评分才参与评级；比值向下保留一位小数，没有 5 分上限。`);
    const table=node("table"),caption=node("caption","","收藏/评分评级标准"),head=node("thead"),heading=node("tr"),body=node("tbody");
    heading.append(node("th","","收藏/评分比值"),node("th","","评级"));head.append(heading);
    rules.levels.forEach((level,index)=>{
      const row=node("tr"),upper=rules.levels[index-1]?.threshold;
      row.append(node("td","",upper===undefined?`≥ ${level.threshold}`:`${level.threshold} ≤ 比值 < ${upper}`),node("td","",level.label));body.append(row);
    });
    table.append(caption,head,body);
    const example=node("p","","例如：210 次收藏 ÷ 77 人评分 = 2.727…，显示为「2.7（一般）」。");
    const sorting=node("p","",`选择「收藏/评分 · 从高到低」后，先按全部匹配作品的未截断比值排序，再分页；比值相同时按作品 ID 从大到小排列。少于 ${minimum} 人评分、未评分或数据未知的作品排在最后。零收藏且评分人数达标时，显示「0.0（冷门）」。`);
    const data=node("p","",`所有排序和显示方式均展示此指标。样本不足会显示已评分人数 / ${minimum}；缺少收藏数或评分人数显示「未知」。评分人数随收藏数采集一并保存，旧记录需刷新采集补齐，切换排序不会自动采集。收藏数分享文件不包含评分人数，导入后使用本地已有数据计算。`);
    const caveat=node("p","","它反映收藏与评分的相对关系，作为选作品的辅助参考。数值来自本地保存的快照，分子和分母可能来自不同时间；较少的评分样本也可能抬高比值，不能直接代表客观质量或实时排名。");
    const reference=node("a","","查看原脚本的评分系统说明 ↗");reference.href=rules.reference_url;reference.target="_blank";reference.rel="noopener noreferrer";
    content.replaceChildren(intro,table,example,sorting,data,caveat,reference);
  }
  function render(item,options){
    const view=options.view,labels=options.labels;
    function trackLink(link,item){if(options.onOpen)link.onclick=()=>options.onOpen(item);}
    function stateBadge(item){return node("span",`reading-badge reading-${item.reading_state}`,stateNames[item.reading_state]||"未分类");}
    const namespaceOrder=["artist","group","parody","character","cosplayer","language","female","male","mixed","other","reclass","location","temp"];
    function tagParts(tag){
      const separator=tag.indexOf(":"),namespace=separator>=0?tag.slice(0,separator):"other",original=separator>=0?tag.slice(separator+1):tag,translated=labels.get(tag);
      return {tag,namespace,groupName:translated?.namespace_name||namespace,name:translated?.translated?translated.name:original,matched:options.tags.includes(tag)};
    }
    function tagButton(part){
      const button=node("button",part.matched?"matched":"",part.name);button.type="button";button.title=`${part.groupName}：${part.name}\n${part.tag}\n${options.tagHint}`;
      button.onclick=()=>options.onTag(part.tag);return button;
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
      if(!total||["minimal","thumbnails"].includes(view))return section;
      if(view==="compact"){
        section.classList.add("saved-flat-tags");section.append(...groups.flatMap(group=>group.items).map(tagButton));return section;
      }
      const all=node("div","saved-tag-preview saved-tag-all-visible");
      all.append(...groups.map(group=>tagGroupRow(group,group.items)));
      section.append(all);
      return section;
    }
    function recordCard(item){
      const failed=item.collection_status==="failed",hasCount=item.favorite_count!=null;
      const card=node("article",`saved-card${failed?" failed":""}${options.selected?.has(item.gid)?" selected":""}`);const content=node("div","saved-content");
      if(["extended","thumbnails"].includes(view)){
        const cover=node("div","saved-cover"),image=node("img");image.src=item.cover_path||"/cover-placeholder.svg";image.alt="";image.loading="lazy";image.decoding="async";image.width=176;image.height=235;image.onerror=()=>{if(!image.src.endsWith("/cover-placeholder.svg"))image.src="/cover-placeholder.svg";};cover.append(image);card.append(cover);
      }
      const head=node("div","saved-card-head");
      if(options.onSelect){
        const choose=node("input","record-select");choose.type="checkbox";choose.checked=Boolean(options.selected?.has(item.gid));choose.setAttribute("aria-label",`选择作品 ID ${item.gid}`);choose.onchange=()=>options.onSelect(item,choose.checked);head.append(choose);
      }
      head.append(node("span",`category-badge gallery-category category-${categoryStyles[item.category]||"misc"}`,item.metadata_available?(categories[item.category]||item.category||"未分类"):"目录信息缺失"));
      head.append(node("span",`collection-badge ${failed?"failed":hasCount?"success":"unknown"}`,failed?(hasCount?"刷新失败":"抓取失败"):(hasCount?"采集成功":"尚未采集")));
      if(item.replaced)head.append(node("span","record-state","旧版本"));
      if(item.removed||item.expunged)head.append(node("span","record-state","已移除 / 隐藏"));
      if(options.recordControls)head.append(stateBadge(item));
      if(options.recordControls&&item.opened_count)head.append(node("span","opened-badge",`已点开 ${number(item.opened_count)} 次`));
      else if(options.recordControls)head.append(node("span","opened-badge","从未点开"));
      head.append(node("span","saved-id",`ID ${item.gid}`));content.append(head);
      const link=sourceURL(item.source_url),title=node(link?"a":"span","saved-title",item.title||item.title_jpn||`作品 ID ${item.gid}`);
      const captured=dateParts(item.recorded_at||item.checked_at);title.title=`${title.textContent}\nID ${item.gid} · ${sources[item.source]||"来源未知"}\n记录于 ${captured.date} ${captured.time}`;
      if(link){title.href=link;title.target="_blank";title.rel="noopener noreferrer";trackLink(title,item);}content.append(title);
      if(item.title_jpn&&item.title_jpn!==item.title)content.append(node("p","saved-subtitle",item.title_jpn));
      const details=node("div","saved-metadata");
      if(item.metadata_available){
        if(item.filecount!=null)details.append(node("span","",`${number(item.filecount)} 页`));
        const rating=Number(item.rating);if(item.rating!=null&&Number.isFinite(rating))details.append(node("span","",`☆ ${rating.toFixed(2)} 平均评分`));
        if(item.rating_count!=null)details.append(node("span","",`评分人数 ${number(item.rating_count)}`));
        if(item.posted)details.append(node("span","",`发布于 ${dateParts(item.posted).date}`));
        content.append(details);
      }else content.append(node("p","saved-missing-note","当前作品目录中未找到对应标题和标签，已保存的采集结果仍保留。"));
      if(hasCount){
        const own=item.collector_id&&item.collector_id===options.collectorIdentity?.collector_id;
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
      const count=node("aside","saved-count");count.append(node("strong","",hasCount?number(item.favorite_count):"—"),node("span","",hasCount?(failed?"上次成功的收藏数":"已记录收藏数"):"收藏数未知"));
      count.title=hasCount?`已记录收藏数：${number(item.favorite_count)}`:"收藏数未知";
      const ratingInfo=item.favorite_rating;
      const ratingLabel=ratingInfo?.state==="rated"?`${Number(ratingInfo.value).toLocaleString("zh-CN",{minimumFractionDigits:1,maximumFractionDigits:1})}（${ratingInfo.label}）`:ratingInfo?.label||"未知";
      const value=node("span",`saved-ratio rating-${ratingInfo?.state||"unknown"}`,`收藏/评分 ${ratingLabel}`);
      value.title=ratingInfo?.state==="rated"?`收藏/评分：${number(item.favorite_count)} ÷ ${number(item.rating_count)} = ${item.favorite_rating_ratio}\n显示向下保留一位小数；排序使用未截断比值。`:(hasCount?(item.rating_count==null?"评分人数缺失，需刷新采集后补齐":item.rating_count===0?"评分人数为 0，暂不评级":`目前 ${number(item.rating_count)} 人评分，未达到评级所需人数`):"收藏数未知，需先采集");
      if(item.rating_count!=null)value.title+=`\n评分人数：${number(item.rating_count)}\n采集于 ${dateParts(item.rating_checked_at).date} ${dateParts(item.rating_checked_at).time}`;
      count.append(value);
      if(ratingInfo?.state==="insufficient")count.append(node("span","saved-rating-sample",`评分人数 ${number(item.rating_count)} / 20`));
      if(options.recordControls){
        const stateControl=node("label","saved-state-control","阅读状态"),stateSelect=node("select");stateSelect.setAttribute("aria-label",`作品 ID ${item.gid} 的阅读状态`);
        for(const [value,label] of Object.entries(stateNames)){const option=node("option","",label);option.value=value;option.selected=value===item.reading_state;stateSelect.append(option);}
        stateSelect.onchange=()=>options.onState([item.gid],stateSelect.value);stateControl.append(stateSelect);count.append(stateControl);
      }
      card.append(count);
      const footer=node("div","saved-card-footer"),saved=dateParts(item.recorded_at||item.checked_at),stamp=node("div","",item.recorded_at||item.checked_at?`${failed?"失败记录于":"采集于"} ${saved.date} ${saved.time}`:"尚无收藏数采集记录");
      if(item.source)stamp.append(node("span","saved-source",sources[item.source]||"来源未知"));footer.append(stamp);
      const actions=node("div","saved-footer-actions");
      if(link){const open=node("a","","查看源站 ↗");open.href=link;open.target="_blank";open.rel="noopener noreferrer";trackLink(open,item);actions.append(open);}footer.append(actions);
      card.append(footer);return card;
    }
    return recordCard(item);
  }
  window.galleryCards={render,dateParts,renderRatingGuide};
})();
