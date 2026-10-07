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


def startup_page(message, failed=False):
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <title>{TITLE}</title><style>
    body{{margin:0;background:#f6f8f7;color:#243a35;font-family:'Microsoft YaHei',sans-serif}}
    main{{max-width:680px;margin:15vh auto;padding:40px;background:white;border-radius:18px}}
    h1{{font-size:26px}}p{{line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere}}
    progress{{width:100%;accent-color:#247b64}}.hint{{color:#718079;font-size:14px}}
    </style><main><h1>{'暂时无法启动' if failed else '正在准备资料库'}</h1>
    <p id="progress-message">{html.escape(message)}</p>{'' if failed else '<progress></progress>'}
    <p class="hint">{'请处理上述问题后重新打开应用。' if failed else '首次解压和建立索引可能需要几分钟，请保持窗口打开。'}</p></main></html>"""


class DesktopApi:
    def __init__(self, session):
        self._session = session
        self._window = None
        self._dialog_lock = threading.Lock()

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
            return {"cancelled": False, "record_count": int(count) if count is not None else None}
        finally:
            self._dialog_lock.release()


def open_desktop(root):
    if not webview2_available():
        notice("未检测到 Microsoft Edge WebView2 运行环境。\n请在微软官方网站安装 Evergreen Runtime 后重新打开应用。\n无需安装 Python。")
        webbrowser.open(WEBVIEW_HELP)
        return 1
    import webview
    from desktop_service import DesktopSession
    session = DesktopSession(root)
    api = DesktopApi(session)
    window = webview.create_window(TITLE, html=startup_page("正在检查本地资料…"), js_api=api, width=1360, height=900, min_size=(1000, 650), background_color="#f6f8f7")
    api._window = window
    state = {"starting": True, "closing": False, "closed": False}
    gate = threading.Lock()

    def progress(message):
        try:
            window.evaluate_js("document.getElementById('progress-message').textContent=" + json.dumps(str(message)))
        except Exception:
            logging.exception("Unable to show startup progress")

    def start():
        try:
            if window.gui.renderer != "edgechromium":
                raise RuntimeError("无法启用 WebView2。请更新 Microsoft Edge WebView2 运行环境及 Windows 系统组件后重试。")
            url = session.start(progress)
            window.load_url(url)
        except Exception as error:
            logging.exception("Desktop startup failed")
            window.load_html(startup_page(str(error), failed=True))
        finally:
            state["starting"] = False

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
            if not state["closing"]:
                state["closing"] = True
                threading.Thread(target=stop, name="safe-desktop-exit", daemon=False).start()
            return False

    window.events.closing += closing
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["ALLOW_DOWNLOADS"] = False  # Exports use the native save dialog.
    try:
        webview.start(start, gui="edgechromium", private_mode=True)
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
    if remaining and remaining[0] == "--verify-desktop":
        from desktop_verify import verify_desktop
        return verify_desktop(Path(remaining[1]))
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
