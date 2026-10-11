"use strict";
(() => {
  const make=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  function inline(parent,text){
    const pattern=/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^\s)]+\))/g;
    for(const part of text.split(pattern)){
      if(part.startsWith('**')&&part.endsWith('**'))parent.append(make('strong',part.slice(2,-2)));
      else if(part.startsWith('`')&&part.endsWith('`'))parent.append(make('code',part.slice(1,-1)));
      else{
        const link=part.match(/^\[([^\]]+)\]\(([^\s)]+)\)$/);
        let url;try{if(link)url=new URL(link[2]);}catch{}
        if(url&&['https:','http:'].includes(url.protocol)&&!url.username&&!url.password){
          const anchor=make('a',link[1]);anchor.href=url.href;anchor.target='_blank';anchor.rel='noopener noreferrer';parent.append(anchor);
        }else parent.append(document.createTextNode(part));
      }
    }
  }
  function render(parent,text){
    parent.replaceChildren();let list=null,code=null;
    for(const line of String(text||'').replace(/\r\n?/g,'\n').split('\n')){
      if(line.trim().startsWith('```')){
        if(code){code=null;}else{list=null;const pre=make('pre');code=make('code');pre.append(code);parent.append(pre);}continue;
      }
      if(code){code.append(document.createTextNode(line+'\n'));continue;}
      const value=line.trim();if(!value){list=null;continue;}
      const item=value.match(/^(?:(\d+)[.)、]\s+|[-*+]\s+)(.*)$/);
      if(item){
        const tag=item[1]?'ol':'ul';
        if(!list||list.tagName.toLowerCase()!==tag){list=make(tag);list.className='client-note-list';parent.append(list);}
        const entry=make('li');inline(entry,item[2]);list.append(entry);continue;
      }
      list=null;const heading=value.match(/^#{1,6}\s+(.*)$/),quote=value.match(/^>\s?(.*)$/);
      const node=make(heading?'h3':quote?'blockquote':'p');inline(node,heading?heading[1]:quote?quote[1]:value);parent.append(node);
    }
    parent.hidden=!parent.childNodes.length;
  }
  window.catalogReleaseNotes={render};
})();
