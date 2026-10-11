"use strict";
(async()=>{
  const el=id=>document.getElementById(id),make=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  let data=null,selected=new URLSearchParams(location.search).get('version')||'',token='';
  function date(item){if(!item.date)return item.combined_in?'合并于 v'+item.combined_in:'随客户端提供';const value=new Date(item.date);return Number.isNaN(value.getTime())?'日期未提供':new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(value);}
  function detail(item){
    selected=item.version;el('history-title').textContent='v'+item.version+' · '+item.title;
    el('history-meta').textContent=date(item)+(item.version===data.current_version?' · 当前版本':'')+(item.prerelease?' · 预发布':'')+(item.combined_in?' · 收录于合并公告':'');
    el('history-original').href=item.release_url;el('history-original').textContent=item.source==='bundled'?'在 GitHub 查看发布 ↗':'在 GitHub 查看原文 ↗';
    const lines=item.notes.split('\n'),heading=lines[0]?.match(/^#\s+v?(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)\b/);if(heading?.[1]===item.version)lines.shift();
    window.catalogReleaseNotes.render(el('history-notes'),lines.join('\n'));
    for(const button of el('history-list').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.version===selected));
    history.replaceState(null,'','/changelog?version='+encodeURIComponent(selected));
  }
  function render(){
    const entries=data.entries.filter(item=>el('history-prerelease').checked||!item.prerelease);
    el('history-list').replaceChildren();el('history-releases').href=data.releases_url;
    for(const item of entries){
      const button=make('button');button.className='history-version';button.dataset.version=item.version;button.setAttribute('aria-pressed','false');button.append(make('strong','v'+item.version));
      for(const [label,type] of [[item.version===data.current_version?'当前版本':'',''],[item.prerelease?'预发布':'',' pre']]){if(label){const badge=make('span',label);badge.className='history-badge'+type;button.append(badge);}}
      const title=make('span',item.title);title.className='history-version-title';const stamp=make('span',date(item));stamp.className='history-date';button.append(title,stamp);
      button.onclick=()=>detail(item);el('history-list').append(button);
    }
    const active=entries.find(item=>item.version===selected)||entries.find(item=>item.version===data.current_version)||entries[0];if(active)detail(active);
  }
  el('history-prerelease').onchange=()=>render();
  el('history-refresh').onclick=async()=>{
    el('history-refresh').disabled=true;el('history-status').classList.remove('error');el('history-status').textContent='正在读取 GitHub 发布记录…';
    try{const response=await fetch('/api/release-history/refresh',{method:'POST',headers:{'Content-Type':'application/json','X-Catalog-Token':token},body:'{}'});const value=await response.json();if(!response.ok)throw Error(value.error||'发布记录读取失败。');data=value;render();el('history-status').textContent='已刷新发布记录，共 '+data.entries.length+' 个版本。内置历史日志仍可离线查看。';}
    catch(error){el('history-status').classList.add('error');el('history-status').textContent=(error.message==='Failed to fetch'?'无法连接本地服务。':error.message)+' 当前更新日志已保留。';}
    finally{el('history-refresh').disabled=false;}
  };
  try{const response=await fetch('/api/release-history');if(!response.ok)throw Error('无法读取更新日志。');data=await response.json();if(!selected)selected=data.current_version;render();el('history-status').textContent='已载入 '+data.entries.length+' 个版本的更新日志；网络可用时可刷新 GitHub 发布记录。';const status=await (await fetch('/api/status')).json();token=status.action_token;el('history-refresh').disabled=!token;}
  catch(error){el('history-status').textContent=error.message;el('history-status').classList.add('error');}
})();
