"""Embedded Windows desktop entry point; also dispatches packaged helpers."""

from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
import argparse
import ctypes
import html
import json
import logging
import os
import secrets
import sys
import threading
import webbrowser

from runtime_paths import library_root, resource_root
from desktop_updates import ClientUpdater

TITLE = "藏目 · ExHentai 资料库"
WEBVIEW_HELP = "https://developer.microsoft.com/microsoft-edge/webview2/"
EXPORTS = {
    "/api/credentials/export": ("exhentai-login.ehcred", "加密登录文件 (*.ehcred)"),
    "/api/maintenance/favorites-export": ("favorites.ehfavorites.json", "收藏数文件 (*.json)"),
    "/api/maintenance/collector-identity-export": ("collector-identity.ehcollector.json", "采集身份文件 (*.json)"),
}


def webview2_available():
    if os.name != "nt":
        return False
    import winreg
    client = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, client, 0, winreg.KEY_READ | view) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                    if version and str(version) != "0.0.0.0":
                        return True
            except OSError:
                continue
    return False


def notice(message):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, str(message), TITLE, 0x40)
    else:
        print(message, file=sys.stderr)


def startup_page(message, failed=False, missing_root=None):
    from first_run import CATALOG_ARCHIVE, CATALOG_DOWNLOAD_URL
    missing = missing_root is not None
    heading = "未找到数据库" if missing else "暂时无法启动" if failed else "正在准备资料库"
    guidance = ""
    if missing:
        guidance = f"""<section id="startup-guidance">
        <ol><li>前往作品数据库下载页，下载 <strong>{CATALOG_ARCHIVE}</strong>。</li>
        <li>无需解压，将文件放入下面的程序资料目录（通常与 ExCatalog.exe 同级）：</li></ol>
        <p class="directory">{html.escape(str(Path(missing_root).resolve()))}</p>
        <p class="hint">已有数据库应放在该目录的 data/catalog.sqlite3；不要放入 _internal。下载后请保留准确文件名。</p>
        <div class="actions"><button disabled onclick="startupAction('open_catalog_download')">下载数据库</button>
        <button disabled onclick="startupAction('open_catalog_directory')">打开存放目录</button>
        <button disabled class="primary" onclick="startupAction('retry_startup')">重新检测</button></div>
        <p class="hint">下载入口：<a href="{CATALOG_DOWNLOAD_URL}" target="_blank" rel="noopener noreferrer">URenko/e-hentai-db · nightly</a></p>
        <p id="startup-feedback" role="status" aria-live="polite"></p></section>"""
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{TITLE}</title><style>
    body{{margin:0;background:#f6f8f7;color:#243a35;font-family:'Microsoft YaHei',sans-serif}}
    main{{max-width:680px;margin:7vh auto;padding:32px;background:white;border-radius:18px}}[hidden]{{display:none!important}}
    h1{{font-size:26px}}p{{line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere}}
    progress{{width:100%;accent-color:#247b64}}.hint{{color:#718079;font-size:14px}}
    li{{line-height:1.9}}.directory{{background:#edf5f0;padding:12px;border-radius:8px}}a{{color:#247b64}}
    .actions{{display:flex;gap:10px;flex-wrap:wrap}}button{{padding:10px 16px;border:1px solid #bfd3c9;border-radius:8px;background:white;color:#245c49;cursor:pointer;font:inherit}}
    button.primary{{background:#247b64;color:white}}button:disabled{{opacity:.6;cursor:wait}}
    @media(max-width:600px){{main{{margin:16px;padding:20px}}h1{{font-size:23px}}}}
    </style><main><h1 id="startup-title">{heading}</h1>
    <p id="progress-message" role="status" aria-live="polite">{html.escape(message)}</p><progress id="startup-progress" {'hidden' if failed or missing else ''}></progress>
    {guidance}<p id="startup-hint" class="hint">{'放好数据库后点击「重新检测」，也可直接关闭窗口，稍后继续。' if missing else '请处理上述问题后重新打开应用。' if failed else '首次解压和建立索引可能需要几分钟，请保持窗口打开。'}</p></main>
    <script>
    function bridgeReady(){{document.querySelectorAll('.actions button').forEach(button=>button.disabled=false);}}
    addEventListener('pywebviewready',bridgeReady);
    if(window.pywebview?.api)bridgeReady();
    async function startupAction(method){{
      const feedback=document.getElementById('startup-feedback');
      document.querySelectorAll('.actions button').forEach(button=>button.disabled=true);
      try{{const result=await window.pywebview.api[method]();if(result.error)throw new Error(result.error);
        feedback.textContent=method==='retry_startup'?'正在重新检测…':method==='open_catalog_download'?'已打开下载页。下载完成后请放入指定目录，再点击「重新检测」。':'已打开数据库存放目录。';
      }}catch(error){{feedback.textContent=error.message;}}
      finally{{bridgeReady();}}
    }}
    </script></html>"""


class DesktopApi:
    def __init__(self, session):
        self._session = session
        self._window = None
        self._dialog_lock = threading.Lock()
        self._updates = ClientUpdater(session.root)
        self._install_callback = None
        self._startup_callback = None

    def _startup_action(self, action):
        try:
            if self._session.base_url or not self._startup_callback:
                raise ValueError("请在首次配置窗口中操作。")
            return action()
        except Exception as error:
            return {"error": str(error)}

    def open_catalog_download(self):
        from first_run import CATALOG_DOWNLOAD_URL
        return self._startup_action(lambda: {"opened": webbrowser.open(CATALOG_DOWNLOAD_URL)})

    def open_catalog_directory(self):
        def open_directory():
            os.startfile(self._session.root)
            return {"opened": True}
        return self._startup_action(open_directory)

    def retry_startup(self):
        return self._startup_action(lambda: self._startup_callback())

    def _local_page(self):
        current = urlparse(self._window.get_current_url() or "")
        base = urlparse(self._session.base_url or "")
        if current.scheme != "http" or not base.netloc or current.netloc != base.netloc:
            raise ValueError("请从本机资料库页面操作客户端更新。")

    def _update_action(self, action):
        try:
            self._local_page()
            if self._session.server:
                self._updates.history = self._session.server.release_history
            return action()
        except Exception as error:
            return {"error": str(error)}

    def client_info(self):
        return self._update_action(self._updates.info)

    def acknowledge_client_notes(self):
        return self._update_action(self._updates.acknowledge)

    def acknowledge_cloud_upload_guide(self):
        def acknowledge():
            if urlparse(self._window.get_current_url() or "").path != "/maintenance":
                raise ValueError("请进入资料库维护的上传页面查看新功能。")
            return self._updates.acknowledge_upload_guide()
        return self._update_action(acknowledge)

    def check_client_update(self):
        return self._update_action(self._updates.check)

    def start_client_update_check(self):
        return self._update_action(self._updates.start_startup_check)

    def startup_client_update_status(self):
        return self._update_action(self._updates.startup_status)

    def take_startup_update_prompt(self):
        return self._update_action(self._updates.take_startup_prompt)

    def prepare_client_update(self):
        return self._update_action(self._updates.prepare)

    def client_update_status(self):
        return self._update_action(lambda: dict(self._updates.state))

    def install_client_update(self):
        return self._update_action(lambda: self._install_callback() if self._install_callback else {"error": "更新入口尚未准备好。"})

    def save_attachment(self, endpoint):
        try:
            return self._save_attachment(endpoint)
        except Exception as error:
            return {"error": str(error)}

    def _save_attachment(self, endpoint):
        if not isinstance(endpoint, str) or endpoint not in EXPORTS:
            raise ValueError("不支持的导出文件。")
        current = urlparse(self._window.get_current_url() or "")
        base = urlparse(self._session.base_url or "")
        if current.scheme != "http" or current.netloc != base.netloc:
            raise ValueError("请从本机资料库页面导出。")
        if not self._dialog_lock.acquire(blocking=False):
            raise ValueError("文件保存窗口已打开，请先完成保存。")
        try:
            import webview
            filename, file_type = EXPORTS[endpoint]
            selected = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename, file_types=(file_type,))
            if not selected:
                return {"cancelled": True}
            destination = Path(selected[0]).resolve()
            suffix = Path(filename).suffix
            if not destination.suffix:
                destination = destination.with_suffix(suffix)
            if destination.suffix.lower() != suffix:
                raise ValueError(f"请将文件保存为 {suffix} 格式。")
            for name in ("data", "logs", "backups", "_internal", "static"):
                if destination.is_relative_to(self._session.root / name):
                    raise ValueError("请将导出文件保存到独立文件夹，避免覆盖程序或资料库文件。")
            if destination.is_relative_to(resource_root() / "static") or (getattr(sys, "frozen", False) and destination.is_relative_to(resource_root())):
                raise ValueError("不能将导出文件保存到程序运行文件夹。")
            request = Request(self._session.base_url + endpoint, data=b"{}", method="POST", headers={"Content-Type": "application/json", "X-Catalog-Token": self._session.server.action_token})
            try:
                with build_opener(ProxyHandler({})).open(request, timeout=60) as response:
                    body = response.read(128*1024**2 + 1)
                    count = response.headers.get("X-Favorite-Record-Count")
                    share_info = json.loads(response.headers.get("X-Favorite-Share-Info", "null"))
            except HTTPError as error:
                with error:
                    raise ValueError(json.load(error).get("error", "文件导出失败。")) from error
            if len(body) > 128*1024**2:
                raise ValueError("导出文件超过支持的大小。")
            temporary = destination.with_name(destination.name + "." + secrets.token_hex(6) + ".partial")
            try:
                with temporary.open("xb") as output:
                    output.write(body)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return {"cancelled": False, "record_count": int(count) if count is not None else None, "share_info": share_info}
        finally:
            self._dialog_lock.release()


def open_desktop(root):
    from first_run import MissingCatalogError, missing_catalog
    if not webview2_available():
        notice("未检测到 Microsoft Edge WebView2 运行环境。\n请在微软官方网站安装 Evergreen Runtime 后重新打开应用。\n无需安装 Python。")
        webbrowser.open(WEBVIEW_HELP)
        return 1
    import webview
    from desktop_service import DesktopSession
    session = DesktopSession(root)
    api = DesktopApi(session)
    needs_catalog = missing_catalog(root)
    initial_page = startup_page("程序包不包含作品数据库，请先下载并放入指定目录。", missing_root=root) if needs_catalog else startup_page("正在检查本地资料…")
    window = webview.create_window(TITLE, html=initial_page, js_api=api, width=1360, height=900, min_size=(1000, 650), background_color="#f6f8f7")
    api._window = window
    state = {"starting": not needs_catalog, "closing": False, "closed": False}
    gate = threading.Lock()

    def progress(message):
        try:
            window.evaluate_js("document.getElementById('progress-message').textContent=" + json.dumps(str(message)))
        except Exception:
            logging.exception("Unable to show startup progress")

    def start():
        failed_page = None
        try:
            if not window.events.loaded.wait(20):
                raise RuntimeError("准备页面加载超时，请关闭应用后重试。")
            if window.gui.renderer != "edgechromium":
                raise RuntimeError("无法启用 WebView2。请更新 Microsoft Edge WebView2 运行环境及 Windows 系统组件后重试。")
            window.evaluate_js("document.getElementById('startup-title').textContent='正在准备资料库';document.getElementById('startup-progress').hidden=false;document.getElementById('startup-guidance')?.setAttribute('hidden','');document.getElementById('startup-hint').textContent='首次解压和建立索引可能需要几分钟，请保持窗口打开。';")
            progress("正在检查本地资料…")
            url = session.start(progress)
            window.load_url(url)
        except MissingCatalogError:
            failed_page = startup_page("未检测到数据库或作品目录压缩包，请确认下载已完成、文件名和位置正确。", missing_root=root)
        except Exception as error:
            logging.exception("Desktop startup failed")
            failed_page = startup_page(str(error), failed=True)
        finally:
            with gate:
                state["starting"] = False
        if failed_page:
            window.load_html(failed_page)

    def retry_startup():
        with gate:
            if state["starting"] or state["closing"] or state["closed"]:
                return {"error": "正在处理，请稍候。"}
            state["starting"] = True
        threading.Thread(target=start, name="desktop-startup-retry", daemon=False).start()
        return {"starting": True}

    api._startup_callback = retry_startup

    def stop():
        try:
            window.set_title("正在保存进度并退出…")
            session.close()
            state["closed"] = True
            window.destroy()
        except Exception as error:
            logging.exception("Desktop close was deferred")
            window.set_title(TITLE)
            notice(str(error))
            state["closing"] = False

    def closing():
        with gate:
            if state["closed"]:
                return True
            if state["starting"]:
                notice("正在准备资料库，请等待准备完成后再关闭窗口。")
                return False
            if api._dialog_lock.locked():
                notice("文件保存窗口已打开，请先完成保存或取消，再退出应用。")
                return False
            if api._updates.lock.locked() and api._updates.startup_status()["phase"] != "checking":
                notice("正在检查或下载客户端更新，请等待处理完成后退出。")
                return False
            if not state["closing"]:
                state["closing"] = True
                threading.Thread(target=stop, name="safe-desktop-exit", daemon=False).start()
            return False

    def install_update():
        with gate:
            if state["closing"] or state["starting"] or api._dialog_lock.locked():
                raise ValueError("请等待当前窗口操作结束，再安装更新。")
            if api._updates.state["phase"] != "ready":
                raise ValueError("请先下载并校验新版客户端。")
            if session.server.maintenance.status().get("busy") or session.server.maintenance.picker_lock.locked():
                raise ValueError("请先完成维护任务或关闭文件选择窗口，再安装更新。")
            state["closing"] = True
            api._updates.state = {"phase": "installing", "message": "正在保存采集进度并安装更新…", "percent": 100}

        def install():
            try:
                window.set_title("正在保存进度并更新客户端…")
                session.close()
                api._updates.launch_installer()
                state["closed"] = True
                window.destroy()
            except Exception as error:
                logging.exception("Client update was deferred")
                state["closing"] = False
                api._updates.state = {"phase": "ready", "message": "本次安装未开始：" + str(error), "percent": 100}
                window.set_title(TITLE)
                notice(str(error))

        threading.Thread(target=install, name="safe-client-update", daemon=False).start()
        return {"installing": True}

    api._install_callback = install_update

    window.events.closing += closing
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["ALLOW_DOWNLOADS"] = False  # Exports use the native save dialog.
    try:
        webview.start(lambda: None if needs_catalog else start(), gui="edgechromium", private_mode=True)
    finally:
        session.close(wait_for_maintenance=True)
    return 0


def pick_directory(arguments):
    import tkinter as tk
    from tkinter import filedialog
    result_file, title, initial, purpose = arguments
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(parent=root, title=title, initialdir=initial, mustexist=purpose == "restore")
        Path(result_file).write_text(json.dumps({"path": path}, ensure_ascii=False), encoding="utf-8")
    finally:
        root.destroy()


def main():
    # Set the data root before importing modules whose defaults depend on it.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path)
    options, remaining = parser.parse_known_args()
    if remaining and remaining[0] == "--apply-client-update":
        from desktop_updates import apply_update
        return apply_update(Path(remaining[1]))
    if remaining and remaining[0] == "--verify-desktop":
        from desktop_verify import verify_desktop
        return verify_desktop(Path(remaining[1]), Path(remaining[2]) if len(remaining)>2 else None)
    if remaining and remaining[0] == "--verify-first-run":
        from desktop_verify import verify_first_run
        return verify_first_run(Path(remaining[1]))
    if options.data_root:
        os.environ["EH_CATALOG_DATA_ROOT"] = str(options.data_root.resolve())
    root = library_root()
    if remaining and remaining[0] == "--pick-directory":
        pick_directory(remaining[1:])
        return 0
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    logging.basicConfig(filename=root / "logs" / "desktop.log", level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    if getattr(sys, "frozen", False):
        if sys.stdout is None:
            sys.stdout = (root / "logs" / "desktop-output.log").open("a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = sys.stdout
    if remaining:
        mode, *arguments = remaining
        sys.argv = [sys.argv[0], *arguments]
        if mode == "--serve":
            from app import main as serve
            serve()
        elif mode in ("--browser", "--ensure-service", "--stop"):
            from launch import main as launch
            sys.argv = [sys.argv[0], *(["--no-browser"] if mode == "--ensure-service" else ["--stop"] if mode == "--stop" else [])]
            launch()
        else:
            raise ValueError(f"未知启动参数：{mode}")
        return 0
    return open_desktop(root)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except Exception as error:
        logging.exception("Desktop failed")
        notice(str(error))
        raise SystemExit(1)
