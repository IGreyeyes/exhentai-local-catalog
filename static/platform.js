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
  });
})();
