"use strict";

// Shared presentation behavior. Collection, search and maintenance continue to
// use their existing forms, API calls and explicit confirmation steps.
(() => {
  const byId = id => document.getElementById(id);
  const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const scrollTo = node => node?.scrollIntoView({block: "start", behavior: reducedMotion() ? "auto" : "smooth"});
  const maintenanceLinks = [...document.querySelectorAll(".maintenance-tabs a")];
  const panels = [...document.querySelectorAll(".maintenance-section")];

  function maintenancePanel(target) {
    const node = typeof target === "string" ? byId(target.replace(/^#/, "")) : target;
    return node?.closest(".maintenance-section");
  }

  function selectTool(target, {updateURL = false, focus = false} = {}) {
    const panel = maintenancePanel(target);
    if (!panel) return false;
    for (const item of panels) item.hidden = item !== panel;
    for (const link of maintenanceLinks) {
      const active = link.hash === `#${panel.id}`;
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    }
    if (updateURL && location.hash !== `#${panel.id}`) {
      history.pushState(null, "", `${location.pathname}${location.search}#${panel.id}`);
    }
    if (focus) {
      panel.focus({preventScroll: true});
      const top = panel.getBoundingClientRect().top;
      if (top < 0 || top > innerHeight - 120) scrollTo(panel);
    }
    return true;
  }

  function reveal(target, options = {}) {
    const node = typeof target === "string" ? byId(target.replace(/^#/, "")) : target;
    if (!node) return;
    selectTool(node, options);
    for (let parent = node; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
    }
    if (options.scroll) scrollTo(node);
  }
  window.catalogWorkspace = {reveal};

  if (panels.length) {
    panels.forEach(panel => { panel.tabIndex = -1; });
    document.body.classList.add("workspace-ready");
    const initial = new URLSearchParams(location.search).get("upload-guide") === "1"
      ? "cloud-sync-section" : location.hash.slice(1);
    if (!selectTool(initial)) selectTool(panels[0]);
    // Keep fragment links (also in help text) and browser history working.
    document.addEventListener("click", event => {
      const link = event.target.closest('a[href^="#"]');
      if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      if (maintenancePanel(link.hash.slice(1))) {
        event.preventDefault();
        selectTool(link.hash.slice(1), {updateURL: true, focus: true});
      }
    });
    addEventListener("hashchange", () => selectTool(location.hash.slice(1)));
    addEventListener("popstate", () => {
      if (!selectTool(location.hash.slice(1))) selectTool(panels[0]);
    });
  }

  function revealFragment() {
    if (location.hash === "#collector-panel") reveal("collector-panel", {scroll: true});
  }
  addEventListener("hashchange", revealFragment);
  document.querySelectorAll('a[href="#collector-panel"], a[href="/#collector-panel"]').forEach(link => {
    if (!byId("collector-panel")) return;
    link.addEventListener("click", event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (location.hash !== "#collector-panel") history.pushState(null, "", `${location.pathname}${location.search}#collector-panel`);
      revealFragment();
      byId("collector-panel").querySelector("summary").focus({preventScroll: true});
    });
  });
  revealFragment();

  function updateFilterSummaries() {
    const searchSummary = byId("search-filter-summary");
    if (searchSummary) {
      const items = [];
      const excluded = byId("selected-exclude-tags").children.length;
      const blocked = byId("blacklist-tags").querySelectorAll("button").length;
      if (excluded) items.push(`排除 ${excluded} 个标签`);
      if (byId("exclude-tag-input").value.trim()) items.push("有待添加的排除标签");
      if (!byId("use-tag-blacklist").checked) items.push("黑名单未启用");
      else if (blocked) items.push(`黑名单 ${blocked} 项`);
      if (byId("include-inactive").checked) items.push("包含历史记录");
      searchSummary.textContent = items.join(" · ") || "排除标签、黑名单与历史记录";
      searchSummary.classList.toggle("has-filters", items.length > 0);
    }
    const recordSummary = byId("records-filter-summary");
    if (recordSummary) {
      const items = [];
      for (const [id, fallback] of [["record-state", "all"], ["record-opened", "all"], ["record-source", ""], ["record-age", "all"]]) {
        const input = byId(id);
        if (input.value !== fallback) items.push(input.selectedOptions[0]?.textContent || input.value);
      }
      recordSummary.textContent = items.join(" · ") || "阅读状态、来源与时间";
      recordSummary.classList.toggle("has-filters", items.length > 0);
    }
  }

  for (const id of ["selected-exclude-tags", "blacklist-tags", "record-chips"]) {
    const node = byId(id);
    if (node) new MutationObserver(updateFilterSummaries).observe(node, {childList: true, subtree: true});
  }
  document.addEventListener("input", updateFilterSummaries);
  document.addEventListener("change", updateFilterSummaries);
  document.addEventListener("catalog:filters-changed", updateFilterSummaries);
  document.addEventListener("reset", () => queueMicrotask(updateFilterSummaries));
  updateFilterSummaries();
})();
