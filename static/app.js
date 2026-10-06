"use strict";
const $ = id => document.getElementById(id);
const number = value => Number(value).toLocaleString("zh-CN");
const categories = {Doujinshi:"同人志",Manga:"漫画","Artist CG":"画师 CG","Game CG":"游戏 CG","Image Set":"图片集","Non-H":"非成人内容",Western:"欧美作品",Cosplay:"角色扮演","Asian Porn":"亚洲写真",Misc:"其他",private:"私有记录"};
let tags = [], excludeTags = [], blacklistTags = [], suggestions = [], activeSuggestion = -1, suggestionTimer, suggestionRequest, suggestionKind = "include";
let searchRequest, responseData, submittedFilters;
let catalogActionToken = "", blacklistBusy = false;
const initialResult = $("results").cloneNode(true);
const tagLabels = new Map();

function rememberLabels(labels = {}) {
  Object.entries(labels).forEach(([key, value]) => tagLabels.set(key, value));
}
function tagName(tag) {
  const label = tagLabels.get(tag);
  return label?.translated ? `${label.namespace_name}：${label.name}` : tag;
}
async function loadLabels(values) {
  const needed = [...new Set(values)].filter(tag => !tagLabels.has(tag)).slice(0,24);
  if (!needed.length) return;
  const query = new URLSearchParams();
  needed.forEach(tag => query.append("tag", tag));
  try {
    const response = await fetch(`/api/tag-labels?${query}`);
    if (!response.ok) return;
    rememberLabels((await response.json()).labels);
    renderTags();
  } catch (error) { /* English tags remain usable without translations. */ }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function date(timestamp) {
  if (!timestamp) return "日期未知";
  return new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Hong_Kong",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(timestamp * 1000)).replaceAll("/", "-");
}
function message(text = "") {
  $("message").textContent = text;
  $("message").hidden = !text;
}
function tagUI(kind) {
  return kind === "exclude"
    ? {input:"exclude-tag-input", list:"exclude-suggestions", field:"exclude-tag-field", values:excludeTags, opposite:tags}
    : {input:"tag-input", list:"suggestions", field:"tag-field", values:tags, opposite:excludeTags};
}
function hideSuggestions(kind) {
  for(const value of kind ? [kind] : ["include","exclude"]){
    const config = tagUI(value);
    $(config.list).hidden = true;
    $(config.input).setAttribute("aria-expanded", "false");
    $(config.input).removeAttribute("aria-activedescendant");
  }
  activeSuggestion = -1;
}
function chips(values, removeTag, blacklist = false) {
  return values.map(tag => {
    const chip = element("span", `tag-chip${blacklist ? " blacklist-chip" : ""}`);
    chip.append(element("span", "", tagName(tag)));
    if (tagLabels.get(tag)?.translated) chip.append(element("small", "tag-original", tag));
    const remove = element("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除标签 ${tagName(tag)} ${tag}`);
    remove.onclick = () => removeTag(tag);
    chip.append(remove);
    return chip;
  });
}
function renderTags() {
  $("selected-tags").replaceChildren(...chips(tags, tag => {
    tags = tags.filter(value => value !== tag); renderTags(); window.collectorUI?.invalidatePreview();
  }));
  $("selected-exclude-tags").replaceChildren(...chips(excludeTags, tag => {
    excludeTags = excludeTags.filter(value => value !== tag); renderTags(); window.collectorUI?.invalidatePreview();
  }));
  $("blacklist-tags").replaceChildren(...(blacklistTags.length
    ? chips(blacklistTags, tag => updateBlacklist(blacklistTags.filter(value => value !== tag)), true)
    : [element("span", "blacklist-empty", "黑名单为空")]));
  $("save-excluded-blacklist").disabled = blacklistBusy || !excludeTags.length || !catalogActionToken;
}
function addTag(value, kind = "include") {
  const config = tagUI(kind);
  value = value.trim().toLowerCase().replace(/\$$/, "");
  if (!value) return;
  if (config.opposite.includes(value)) { message(`同一个标签不能同时包含和排除：${tagName(value)}。`); return; }
  if (config.values.length >= 12 && !config.values.includes(value)) { message(`最多同时${kind === "exclude" ? "排除" : "包含"} 12 个标签。`); return; }
  if (!config.values.includes(value)) config.values.push(value);
  window.collectorUI?.invalidatePreview();
  $(config.input).value = "";
  clearTimeout(suggestionTimer);
  suggestionRequest?.abort();
  hideSuggestions(kind);
  renderTags();
  loadLabels([value]);
  $(config.input).focus();
}
async function suggest(kind) {
  const config = tagUI(kind);
  if(suggestionKind!==kind)hideSuggestions(suggestionKind);
  suggestionKind = kind;
  suggestionRequest?.abort();
  const query = $(config.input).value.trim();
  if (!query) { hideSuggestions(kind); return; }
  const request = new AbortController();
  suggestionRequest = request;
  try {
    const response = await fetch(`/api/tags?q=${encodeURIComponent(query)}`, {signal:request.signal});
    if (!response.ok) return;
    const data = await response.json();
    if (request.signal.aborted || query !== $(config.input).value.trim()) return;
    rememberLabels(data.labels);
    renderTags();
    suggestions = data.items.filter(tag => !config.values.includes(tag));
    activeSuggestion = -1;
    $(config.list).replaceChildren(...suggestions.map((tag, index) => {
      const item = element("li");
      item.append(element("span", "suggestion-name", tagName(tag)));
      if (tagLabels.get(tag)?.translated) item.append(element("small", "suggestion-original", tag));
      item.id = `${kind}-suggestion-${index}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", "false");
      item.onpointerdown = event => { event.preventDefault(); addTag(tag, kind); };
      return item;
    }));
    $(config.list).hidden = !suggestions.length;
    $(config.input).setAttribute("aria-expanded", String(Boolean(suggestions.length)));
  } catch (error) { if (error.name !== "AbortError") hideSuggestions(kind); }
}
function configureTagInput(kind) {
  const config = tagUI(kind);
  $(config.input).oninput = () => { clearTimeout(suggestionTimer); suggestionTimer = setTimeout(() => suggest(kind), 180); };
  $(config.input).onkeydown = event => {
    const open = suggestionKind === kind && !$(config.list).hidden && suggestions.length;
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && open) {
      event.preventDefault();
      activeSuggestion = (activeSuggestion + (event.key === "ArrowDown" ? 1 : -1) + suggestions.length) % suggestions.length;
      [...$(config.list).children].forEach((item, index) => item.setAttribute("aria-selected", String(index === activeSuggestion)));
      $(config.input).setAttribute("aria-activedescendant", `${kind}-suggestion-${activeSuggestion}`);
      $(config.list).children[activeSuggestion].scrollIntoView({block:"nearest"});
    } else if (event.key === "Enter" && $(config.input).value.trim()) {
      event.preventDefault();
      addTag(open && activeSuggestion >= 0 ? suggestions[activeSuggestion] : $(config.input).value, kind);
    } else if (event.key === "Escape") hideSuggestions(kind);
  };
}
configureTagInput("include");configureTagInput("exclude");
document.addEventListener("pointerdown", event => {
  if (!$("tag-field").contains(event.target) && !$("exclude-tag-field").contains(event.target)) hideSuggestions();
});
document.querySelectorAll("[data-example]").forEach(button => button.onclick = () => addTag(button.dataset.example));

async function updateBlacklist(values, clearTemporary = false) {
  if (blacklistBusy || !catalogActionToken) return;
  blacklistBusy = true;renderTags();
  $("blacklist-status").classList.remove("error");
  $("blacklist-status").textContent = "正在保存黑名单…";
  try {
    const response = await fetch("/api/preferences", {method:"POST",headers:{"Content-Type":"application/json","X-Catalog-Token":catalogActionToken},body:JSON.stringify({tag_blacklist:values})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "黑名单保存失败。");
    rememberLabels(data.tag_labels);blacklistTags=[...data.tag_blacklist];
    if(clearTemporary)excludeTags=[];
    $("blacklist-status").textContent = `已保存 ${number(blacklistTags.length)} 个黑名单标签，今后的搜索会默认排除。`;
    renderTags();window.collectorUI?.invalidatePreview();
    if(submittedFilters)await search(1,true);
  } catch(error) {
    $("blacklist-status").textContent=error.message==="Failed to fetch"?"无法连接本地服务，黑名单没有保存。":error.message;
    $("blacklist-status").classList.add("error");
  } finally {blacklistBusy=false;renderTags();}
}
$("save-excluded-blacklist").onclick=()=>updateBlacklist([...new Set([...blacklistTags,...excludeTags])],true);
$("use-tag-blacklist").onchange=()=>{window.collectorUI?.invalidatePreview();};

function filters() {
  const params = new URLSearchParams();
  tags.forEach(tag => params.append("tag", tag));
  excludeTags.forEach(tag => params.append("exclude_tag", tag));
  if(!$("use-tag-blacklist").checked)params.set("use_blacklist","0");
  const title = $("title-input").value.trim();
  if (title) params.set("title", title);
  params.set("sort", $("sort").value);
  if ($("category").value) params.set("category", $("category").value);
  if ($("include-inactive").checked) params.set("include_inactive", "1");
  return params;
}
function setBusy(busy) {
  $("search-button").disabled = busy||Boolean(window.catalogMaintenanceActive)||Boolean(window.catalogStopped);
  $("results").setAttribute("aria-busy", String(busy));
  $("previous").disabled = busy || !responseData || responseData.page <= 1;
  $("next").disabled = busy || !responseData || responseData.page >= responseData.pages;
  $("jump-page").disabled = busy;
  window.collectorUI?.updateControls();
}
async function search(page = 1, fresh = true, updateHistory = true) {
  if(window.catalogStopped){message("服务已停止，请双击启动搜索后刷新页面。");return;}
  if(window.catalogMaintenanceActive){message("正在切换作品目录，请维护完成后再搜索。");return;}
  if (fresh) {
    if ($("tag-input").value.trim()) addTag($("tag-input").value);
    if ($("exclude-tag-input").value.trim()) addTag($("exclude-tag-input").value,"exclude");
    if (!tags.length && !$("title-input").value.trim()) { message("请添加至少一个标签，或输入标题关键词。"); $("tag-input").focus(); return; }
    submittedFilters = filters();
  }
  if (!submittedFilters) return;
  searchRequest?.abort();
  const request = new AbortController();
  searchRequest = request;
  const params = new URLSearchParams(submittedFilters);
  params.set("page", String(page));
  message(); hideSuggestions(); setBusy(true);
  $("pagination").hidden = true;
  $("results-summary").textContent = "正在检索全部匹配记录…";
  $("results").replaceChildren(element("div", "loading", "正在查询本地资料库，请稍候…"));
  try {
    const response = await fetch(`/api/search?${params}`, {signal:request.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "搜索失败，请重试。");
    if (request.signal.aborted) return;
    rememberLabels(data.tag_labels);
    const searchedTags = submittedFilters.getAll("tag");
    if (tags.length === searchedTags.length && tags.every((tag,index) => tag === searchedTags[index])) {
      tags = [...data.tags];
    }
    const searchedExcluded = submittedFilters.getAll("exclude_tag");
    if (excludeTags.length === searchedExcluded.length && excludeTags.every((tag,index) => tag === searchedExcluded[index])) excludeTags = [...data.exclude_tags];
    renderTags();
    submittedFilters.delete("tag");
    submittedFilters.delete("exclude_tag");
    params.delete("tag");
    params.delete("exclude_tag");
    data.tags.forEach(tag => { submittedFilters.append("tag", tag); params.append("tag", tag); });
    data.exclude_tags.forEach(tag => { submittedFilters.append("exclude_tag", tag); params.append("exclude_tag", tag); });
    responseData = data;
    renderResults();
    params.set("page", String(data.page));
    if (updateHistory) history.pushState(null, "", `/?${params}`);
  } catch (error) {
    if (error.name === "AbortError") return;
    responseData = null;
    $("results").replaceChildren();
    $("results-summary").textContent = "本次查询未完成";
    message(error.message === "Failed to fetch" ? "本地服务连接已中断，请重新双击启动搜索。" : error.message);
  } finally { if (searchRequest === request) setBusy(false); }
}
function renderResults() {
  const data = responseData;
  if (!data) return;
  const coverage=data.favorite_coverage;
  if(coverage) {
    $("favorite-coverage").hidden=false;
    $("favorite-coverage").textContent=`收藏数已获取 ${number(coverage.known)} / ${number(coverage.total)} 条 · 最近 ${coverage.fresh_days} 天采集 ${number(coverage.fresh)} 条。`+(coverage.complete?"已覆盖本次搜索的全部匹配记录。":"收藏排名仅覆盖已采集部分，未知值排在最后。");
  }
  window.collectorUI?.updateControls();
  const excludedCount=new Set([...(data.exclude_tags||[]),...(data.blacklist_tags||[])]).size;
  $("results-summary").textContent = `共 ${number(data.total)} 条匹配记录 · ${(data.elapsed_ms/1000).toFixed(2)} 秒 · ${data.include_inactive ? "包含历史及不可见记录" : "已排除移除、隐藏及旧版本"}${excludedCount?` · 排除 ${number(excludedCount)} 个标签`:""}`;
  if (!data.total) {
    const empty = element("div", "empty-state");
    empty.append(element("h3", "", "没有找到同时满足条件的作品"), element("p", "", "试着减少标签、移除标题关键词，或包含历史记录后重新搜索。"));
    $("results").replaceChildren(empty);
    $("pagination").hidden = true;
    return;
  }
  const list = element("div", "result-list");
  data.items.forEach((item, index) => {
    const card = element("article", "result-card");
    card.append(element("span", "result-number", number((data.page-1)*data.limit+index+1)));
    const cover=element("div","result-cover"),image=element("img");
    image.src=item.cover_path||"/cover-placeholder.svg";image.alt="";image.loading="lazy";image.decoding="async";image.width=90;image.height=120;
    image.onerror=()=>{if(!image.src.endsWith("/cover-placeholder.svg"))image.src="/cover-placeholder.svg";};cover.append(image);card.append(cover);
    const main = element("div", "result-main");
    const top = element("div", "result-top");
    const badges = element("div");
    badges.append(element("span", "category-badge", categories[item.category] || item.category));
    if (item.replaced) badges.append(element("span", "record-state", "旧版本"));
    if (item.removed || item.expunged) badges.append(element("span", "record-state", "已移除 / 隐藏"));
    top.append(badges, element("span", "result-date", date(item.posted)));
    main.append(top);
    const title = element(item.gallery_path ? "a" : "span", "result-title", item.title || item.title_jpn || "无标题");
    if (item.gallery_path) {
      title.href = `https://${$("link-target").value}${item.gallery_path}`;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    }
    main.append(title);
    if (item.title_jpn && item.title_jpn !== item.title) main.append(element("p", "result-subtitle", item.title_jpn));
    const meta = element("div", "result-meta");
    const rating = Number(item.rating);
    const favorites=element("span",item.favorite_count==null?"":"favorite-value",item.favorite_count==null?"收藏数未知":`${number(item.favorite_count)} 收藏`);
    if(item.favorite_checked_at) favorites.title=`采集日期：${date(item.favorite_checked_at)}`;
    meta.append(element("span", "rating", `☆ ${Number.isFinite(rating) ? rating.toFixed(2) : "—"} 平均评分`), element("span", "", `${number(item.filecount)} 页`), favorites, element("span", "", `ID ${item.gid}`));
    main.append(meta);
    const tagList = element("div", "result-tags");
    const ordered = [...item.tags].sort((a,b) => Number(data.tags.includes(b))-Number(data.tags.includes(a)));
    ordered.slice(0,10).forEach(tag => {
      const button = element("button", data.tags.includes(tag) ? "matched" : "", tagName(tag));
      button.type = "button";
      button.title = `${tagName(tag)}\n${tag}\n点击添加筛选`;
      button.onclick = () => { addTag(tag); $("search-title").scrollIntoView({block:"start",behavior:"smooth"}); };
      tagList.append(button);
    });
    if (ordered.length>10) { const more=element("span","more-tags",`+${ordered.length-10}`); more.title=ordered.slice(10).map(tag => `${tagName(tag)} (${tag})`).join("\n");tagList.append(more); }
    main.append(tagList);card.append(main);list.append(card);
  });
  $("results").replaceChildren(list);
  $("pagination").hidden = false;
  $("page-range").textContent = `显示 ${number((data.page-1)*data.limit+1)}–${number((data.page-1)*data.limit+data.items.length)} / ${number(data.total)} 条`;
  $("page-input").value = data.page;
  $("page-input").max = data.pages;
  $("page-total").textContent = `/ ${number(data.pages)} 页`;
  setBusy(false);
}
function goPage(page) { search(Math.max(1,Number(page)||1),false); $("results-heading").scrollIntoView({block:"start"}); }
$("search-form").onsubmit = event => { event.preventDefault(); search(); };
$("previous").onclick = () => goPage(responseData.page-1);
$("next").onclick = () => goPage(responseData.page+1);
$("jump-page").onclick = () => goPage($("page-input").value);
$("page-input").onkeydown = event => { if(event.key === "Enter") { event.preventDefault(); goPage(event.target.value); } };
$("link-target").onchange = () => renderResults();
function resetForm() {
  searchRequest?.abort(); suggestionRequest?.abort(); clearTimeout(suggestionTimer);
  searchRequest = null; tags=[];excludeTags=[]; responseData=null; submittedFilters=null;
  $("search-form").reset();renderTags();hideSuggestions();message();
  $("results").replaceChildren(...[...initialResult.cloneNode(true).children]);
  $("results-summary").textContent="准备好后，开始你的第一次搜索。";
  $("pagination").hidden=true;setBusy(false);
  $("favorite-coverage").hidden=true;
}
$("reset-filters").onclick = () => {resetForm();history.pushState(null,"","/");};
function restoreURL() {
  resetForm();
  const params=new URLSearchParams(location.search);
  tags=params.getAll("tag").slice(0,12);excludeTags=params.getAll("exclude_tag").slice(0,12);renderTags();
  loadLabels([...tags,...excludeTags]);
  $("title-input").value=params.get("title")||"";
  $("sort").value=params.get("sort")||"newest";
  if(!$("sort").value) $("sort").value="newest";
  $("category").value=params.get("category")||"";
  $("include-inactive").checked=params.get("include_inactive")==="1";
  $("use-tag-blacklist").checked=params.get("use_blacklist")!=="0";
  if(tags.length||$("title-input").value) search(Number(params.get("page"))||1,true,false);
}
window.addEventListener("popstate",restoreURL);
async function start() {
  try {
    const [response,preferencesResponse]=await Promise.all([fetch("/api/status"),fetch("/api/preferences")]);
    if(!response.ok||!preferencesResponse.ok) throw new Error("连接失败");
    const data=await response.json(),preferences=await preferencesResponse.json();
    catalogActionToken=data.action_token||"";
    rememberLabels(preferences.tag_labels);blacklistTags=[...preferences.tag_blacklist];renderTags();
    $("gallery-count").textContent=number(data.gallery_count);
    $("tag-count").textContent=number(data.tag_count);
    $("latest-date").textContent=date(data.latest_posted);
    const translation=data.translations;
    $("translation-status").textContent=translation?.available
      ? `中文词库已加载 · 本地 ${number(translation.matched_tag_count)} 个标签有译文；未翻译项显示原文。`
      : "中文词库未加载，当前使用英文标签。";
    $("connection").replaceChildren(element("i"),document.createTextNode("本地资料库已就绪"));
    restoreURL();
  } catch(error) {$("connection").textContent="本地服务未连接";message("无法读取数据库信息，请重新启动搜索服务。");}
}
start();
