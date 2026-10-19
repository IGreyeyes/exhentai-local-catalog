"use strict";
(() => {
  const ui = id => document.getElementById(id);
  if (!ui("network-form")) return;
  let token = "", loaded = false, busy = false;
  const routeLabels = {proxy: "自定义代理", system: "跟随系统", direct: "直接连接"};
  const customFields = ["host", "port", "username", "password", "domains", "clear-password"];

  function message(text, state = "") {
    ui("network-result").textContent = text;
    ui("network-result").dataset.state = state;
  }
  function controls() {
    ui("network-fields").disabled = !loaded || busy;
    ui("network-reload").disabled = busy;
    for (const id of customFields) ui(`network-${id}`).disabled = !ui("network-enabled").checked;
    ui("network-password").disabled ||= ui("network-clear-password").checked;
    ui("network-test").textContent = busy ? "正在处理…" : "测试连接";
    ui("network-result").setAttribute("aria-busy", String(busy));
  }
  function values() {
    return {enabled: ui("network-enabled").checked, fallback: ui("network-fallback").value,
      host: ui("network-host").value, port: ui("network-port").value,
      username: ui("network-username").value, password: ui("network-password").value,
      domains: ui("network-domains").value, clear_password: ui("network-clear-password").checked};
  }
  function render(state) {
    for (const key of ["host", "port", "username", "fallback"]) ui(`network-${key}`).value = state[key];
    ui("network-enabled").checked = state.enabled;
    ui("network-domains").value = state.domains.join("\n");
    ui("network-password").value = "";
    ui("network-clear-password").checked = false;
    ui("network-password").placeholder = state.has_password ? "已保存；留空继续使用" : "无需认证时留空";
    ui("network-auth-hint").textContent = state.has_password ? "密码已加密保存，页面不回显；更换地址或用户名时请重新填写或清除。" : "认证信息与代理设置使用当前 Windows 用户加密保存。";
    ui("network-saved-note").textContent = state.error ? "设置需要重新保存" : `${state.saved ? "已保存" : "默认设置"} · ${state.enabled ? "自定义代理" : routeLabels[state.fallback]}`;
  }
  async function request(path, payload) {
    const response = await fetch(path, payload === undefined ? {cache: "no-store"} : {
      method: "POST", headers: {"Content-Type": "application/json", "X-Catalog-Token": token}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "操作失败，请重试。");
    return data;
  }
  async function load() {
    busy = true; controls();
    try {
      const [status, settings] = await Promise.all([request("/api/status"), request("/api/network")]);
      token = status.action_token;
      render(settings); loaded = true;
      message(settings.error || "测试使用当前表单，不会自动保存，也不会携带源站 Cookie。", settings.error ? "error" : "");
    } catch (error) { message(error.message || "无法读取代理设置，请重新读取。", "error"); }
    finally { busy = false; controls(); }
  }
  ui("network-form").addEventListener("input", event => {
    if (event.target.id === "network-target") {
      message("测试目标已变化，请重新测试。");
    } else {
      ui("network-saved-note").textContent = "有未保存的修改";
      message("设置已修改，请重新测试；保存后用于后续联网请求。");
    }
    controls();
  });
  ui("network-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !loaded) return;
    const settings = values();
    busy = true; controls();
    try {
      render(await request("/api/network/settings", settings));
      message("代理设置已保存，后续请求使用新设置。采集前请重新验证源站访问。", "success");
    } catch (error) { message(error.message || "保存失败，原设置已保留。", "error"); }
    finally { busy = false; controls(); }
  });
  ui("network-test").addEventListener("click", async () => {
    if (busy || !loaded || !ui("network-form").reportValidity()) return;
    const payload = {settings: values(), target: ui("network-target").value};
    busy = true; controls(); message("正在测试当前表单的连接方式，请稍候…");
    try {
      const result = await request("/api/network/test", payload);
      const detail = `${result.target} · ${routeLabels[result.route]} · ${result.elapsed_ms} 毫秒${result.http_status ? ` · HTTP ${result.http_status}` : ""}`;
      message(`${result.message}\n${detail}`, result.ok ? "success" : result.reachable ? "warning" : "error");
    } catch (error) { message(error.message || "测试失败，请检查本地服务。", "error"); }
    finally { busy = false; controls(); }
  });
  ui("network-reload").addEventListener("click", load);
  load();
})();
