"use strict";
(() => {
  const button = document.getElementById("cover-retry");
  const feedback = document.getElementById("cover-retry-feedback");
  if (!button || !feedback) return;
  async function request(path, options = {}) {
    const response = await fetch(path, {...options, cache: "no-store", signal: AbortSignal.timeout(15000)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "封面重试未完成，请稍后再试。");
    return result;
  }
  button.addEventListener("click", async () => {
    if (button.disabled || window.catalogStopped) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    feedback.hidden = false;
    feedback.classList.remove("error");
    feedback.textContent = "正在准备重新加载封面…";
    try {
      const status = await request("/api/status");
      const result = await request("/api/covers/retry", {method: "POST",
        headers: {"Content-Type": "application/json", "X-Catalog-Token": status.action_token}, body: "{}"});
      const images = document.querySelectorAll(".saved-cover img[data-cover-path]");
      for (const image of images) {
        const url = new URL(image.dataset.coverPath, location.origin);
        url.searchParams.set("retry", String(Date.now()));
        image.src = url.pathname + url.search;
      }
      feedback.textContent = images.length
        ? "正在重新加载可见封面；仍失败时，请检查「网络代理」能否访问 ehgt.org。"
        : `已解除 ${result.cleared} 张封面的失败等待；返回搜索或已记录页面，使用扩展或缩略图视图加载封面。`;
      if (result.waiting) feedback.textContent += ` ${result.waiting} 张封面仍需等待服务器冷却结束。`;
    } catch (error) {
      feedback.classList.add("error");
      feedback.textContent = error.message === "Failed to fetch" || error.name === "TimeoutError"
        ? "无法连接本地服务，请稍后重试；已有封面保留。" : error.message;
    } finally {
      button.removeAttribute("aria-busy");
      setTimeout(() => { button.disabled = Boolean(window.catalogStopped); }, 5000);
    }
  });
})();
