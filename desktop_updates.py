"""Verified GitHub release downloads and rollback-capable desktop installation."""

from pathlib import Path, PurePosixPath
from http.client import IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
from network_settings import NetworkSettings
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from client_version import APP_VERSION, PACKAGE_NAME, RELEASE_NOTES, REPOSITORY
from release_history import ReleaseHistory, announcement_notes
from github_release_page import parse_latest_page, parse_package_asset

UPLOAD_GUIDE_URL = "/maintenance?upload-guide=1#cloud-sync-section"

PROGRAM_FILES = ("ExCatalog.exe", "_internal", "README.md", "DESKTOP.md", "DEVELOPMENT.md", "THIRD_PARTY_NOTICES.md", "RELEASE_NOTES.md")
MAX_PACKAGE_BYTES = 256 * 1024**2
MAX_UNPACKED_BYTES = 1024**3
NETWORK_ATTEMPTS = 4


def retryable_network_error(error):
    if isinstance(error, HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    if isinstance(error, URLError):
        return not isinstance(error.reason, ssl.SSLCertVerificationError)
    if isinstance(error, (ConnectionError, TimeoutError, IncompleteRead, RemoteDisconnected, ssl.SSLEOFError)):
        return True
    return isinstance(error, OSError) and (
        error.errno in {errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH, 10054}
        or getattr(error, "winerror", None) in {10051, 10053, 10054, 10060, 10065})


def network_failure_message(action):
    return (f"{action}失败：GitHub 连接中断、超时或服务暂不可用，已自动重试 {NETWORK_ATTEMPTS - 1} 次。"
            "请确认系统代理已开启且可访问 GitHub，然后重试；也可在本项目发布页手动下载 "
            f"{PACKAGE_NAME}。手动更新前关闭应用，只替换程序文件，保留 data、logs、backups 和作品目录压缩包。")


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("发布版本号格式不正确，应为 v主版本.次版本.修订版本。")
    return tuple(map(int, value.lstrip("v").split(".")))


def asset_url(value):
    parsed = urlparse(value or "")
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
            or not parsed.path.startswith(f"/{REPOSITORY}/releases/download/")):
        raise ValueError("更新包必须来自本项目的 GitHub Releases。")
    return value


def release_info(document):
    if not isinstance(document, dict) or document.get("draft") or document.get("prerelease"):
        raise ValueError("未找到可用的正式发布。")
    version = document.get("tag_name", "")
    newer = version_tuple(version) > version_tuple(APP_VERSION)
    result = {"current_version": APP_VERSION, "latest_version": version.lstrip("v"), "available": newer,
              "notes": str(document.get("body") or "请查看 GitHub 发布页中的更新说明。")[:20000],
              "release_url": f"https://github.com/{REPOSITORY}/releases/tag/{version}"}
    if newer:
        package = next((asset for asset in document.get("assets", []) if asset.get("name") == PACKAGE_NAME), None)
        if not package:
            raise ValueError("新版发布尚未上传完整客户端压缩包，请稍后重试。")
        digest = package.get("digest") or ""
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise ValueError("发布包缺少 GitHub SHA-256 校验值，请等待维护者重新上传。")
        size = package.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_PACKAGE_BYTES:
            raise ValueError("客户端压缩包大小不正确。")
        result.update(url=asset_url(package.get("browser_download_url")), sha256=digest[7:].lower(), size=size)
    return result


def unpack_package(archive, destination):
    """Validate the entire archive before extracting any file."""
    destination = Path(destination).resolve()
    entries, seen, total = [], set(), 0
    with zipfile.ZipFile(archive) as package:
        if len(package.infolist()) > 10000:
            raise ValueError("更新包包含过多文件。")
        for entry in package.infolist():
            name = entry.orig_filename
            path = PurePosixPath(name)
            if ("\\" in name or "\x00" in name or ":" in name or name.startswith("/") or ".." in path.parts
                    or len(path.parts) < 1 or path.parts[0] != "ExCatalog"
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("更新包包含不安全的路径。")
            if len(path.parts) == 1 and entry.is_dir():
                continue
            relative = PurePosixPath(*path.parts[1:])
            if not relative.parts or relative.parts[0] not in PROGRAM_FILES or (len(relative.parts) > 1 and relative.parts[0] != "_internal"):
                raise ValueError("更新包包含用户资料或未知文件，已拒绝安装。")
            if any(part.rstrip(" .") != part or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in relative.parts):
                raise ValueError("更新包包含无效的 Windows 文件名。")
            key = str(relative).casefold()
            if key in seen:
                raise ValueError("更新包包含重复文件。")
            seen.add(key)
            total += entry.file_size
            if total > MAX_UNPACKED_BYTES:
                raise ValueError("更新包解压后超过大小限制。")
            entries.append((entry, relative))
        if "excatalog.exe" not in seen or "_internal/python314.dll" not in seen:
            raise ValueError("更新包缺少程序或内置运行环境。")
        destination.mkdir(parents=True, exist_ok=True)
        for entry, relative in entries:
            target = destination.joinpath(*relative.parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    return destination


def replace_program(payload, target, backup):
    """Only replace allowlisted program files; restore all old files on failure."""
    payload, target, backup = map(lambda path: Path(path).resolve(), (payload, target, backup))
    if not (payload / "ExCatalog.exe").is_file() or not (payload / "_internal" / "python314.dll").is_file():
        raise ValueError("待安装客户端不完整。")
    if payload == target or backup == target or payload.is_relative_to(target) or backup.is_relative_to(target):
        raise ValueError("更新暂存区不能位于正在替换的程序目录。")
    for name in PROGRAM_FILES:
        if (target / name).is_symlink() or ((target / name).exists() and (target / name).resolve().parent != target):
            raise ValueError("程序文件路径不是普通文件或目录，请手动更新。")
    backup.mkdir(parents=True, exist_ok=False)
    saved, installed = [], []
    try:
        for name in PROGRAM_FILES:
            old = target / name
            if old.exists():
                shutil.move(str(old), str(backup / name))
                saved.append(name)
            new = payload / name
            if new.exists():
                installed.append(name)
                if new.is_dir():
                    shutil.copytree(new, old)
                else:
                    shutil.copy2(new, old)
    except BaseException:
        for name in reversed(installed):
            path = target / name
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        for name in saved:
            shutil.move(str(backup / name), str(target / name))
        raise


class ClientUpdater:
    def __init__(self, data_root):
        self.root = Path(data_root)
        self.release = None
        self.workspace = None
        self.state = {"phase": "idle", "message": "", "percent": 0}
        self.lock = threading.Lock()
        self._notes_lock = threading.RLock()
        self._startup_lock = threading.Lock()
        self._startup_phase = "idle"
        self._startup_release = None
        self._startup_error = ""
        self._startup_prompted = False
        self._startup_thread = None
        self.network = NetworkSettings(data_root)
        # Kept as injectable system-mode openers for the existing retry policy.
        self.opener = build_opener(ProxyHandler())
        self.direct_opener = build_opener(ProxyHandler({}))
        self.history = ReleaseHistory(network=self.network)
        self._api_limited_until = 0

    def _network_opener(self, attempt):
        config = self.network.snapshot()
        if config["enabled"] or config["fallback"] == "direct":
            return build_opener(self.network.handler())
        return self.opener if attempt % 2 == 0 else self.direct_opener

    @staticmethod
    def _retry_delay(attempt):
        if attempt:
            time.sleep(min(2 ** (attempt - 1), 4))

    def start_startup_check(self):
        with self._startup_lock:
            if self._startup_phase == "idle":
                self._startup_phase = "checking"
                self._startup_thread = threading.Thread(target=self._check_at_startup, name="client-startup-update-check", daemon=True)
                self._startup_thread.start()
        return self.startup_status()

    def _check_at_startup(self):
        try:
            release = self.check()
            with self._startup_lock:
                self._startup_release = release
                self._startup_phase = "completed"
        except Exception as error:
            with self._startup_lock:
                self._startup_error = str(error)
                self._startup_phase = "failed"

    def startup_status(self):
        with self._startup_lock:
            return {"phase": self._startup_phase, "release": self._startup_release, "error": self._startup_error}

    def take_startup_prompt(self):
        with self._startup_lock:
            if self._startup_phase == "completed" and self._startup_release and self._startup_release["available"] and not self._startup_prompted:
                self._startup_prompted = True
                return {"prompt": True, "release": self._startup_release}
            return {"prompt": False}

    def _notes_state(self):
        try:
            state = json.loads((self.root / "data" / "client-update-state.json").read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_notes_state(self, state):
        path = self.root / "data" / "client-update-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _guide_url(state):
        return UPLOAD_GUIDE_URL if version_tuple(APP_VERSION) >= (1,1,16) and state.get("cloud_upload_guide_seen") is not True else ""

    def info(self):
        with self._notes_lock:
            state = self._notes_state()
            notes, _ = announcement_notes(APP_VERSION, RELEASE_NOTES, self.history.entries)
            return {"version": APP_VERSION, "notes": notes,
                    "releases_url": f"https://github.com/{REPOSITORY}/releases",
                    "show_notes": state.get("acknowledged_version") != APP_VERSION,
                    "upload_guide_url": self._guide_url(state),
                    "can_install": bool(getattr(sys, "frozen", False))}

    def acknowledge(self):
        with self._notes_lock:
            state = self._notes_state()
            state["acknowledged_version"] = APP_VERSION
            self._save_notes_state(state)
            return {"acknowledged": True, "upload_guide_url": self._guide_url(state)}

    def acknowledge_upload_guide(self):
        with self._notes_lock:
            state = self._notes_state()
            if state.get("acknowledged_version") != APP_VERSION:
                raise ValueError("请先阅读本次更新公告。")
            state["cloud_upload_guide_seen"] = True
            self._save_notes_state(state)
            return {"acknowledged": True}

    def _remember_api_limit(self, error):
        headers=error.headers or {}
        limited=error.code==429 or headers.get('X-RateLimit-Remaining')=='0' or 'rate limit' in str(error.reason).lower()
        if limited:
            now=time.time()
            reset=str(headers.get('X-RateLimit-Reset','') or '')
            retry=str(headers.get('Retry-After','') or '')
            until=float(reset) if reset.isdecimal() else now+float(retry) if retry.isdecimal() else now+300
            self._api_limited_until=max(now+60,min(until,now+3600))

    def _public_request(self, url, method='GET', limit=2*1024**2):
        for attempt in range(NETWORK_ATTEMPTS):
            self._retry_delay(attempt)
            request=Request(url,method=method,headers={'User-Agent':'ExCatalog/'+APP_VERSION,'Accept-Encoding':'identity'})
            try:
                with self._network_opener(attempt).open(request,timeout=25) as response:
                    final_url=response.geturl()
                    if method=='HEAD':
                        if getattr(response,'status',200)!=200 or response.headers.get('Content-Type','').split(';')[0].strip().lower()=='text/html':
                            raise ValueError('客户端大小查询返回了无效响应，请打开发布页手动核对。')
                        return None,response.headers,final_url
                    raw=response.read(limit+1)
                    length=response.headers.get('Content-Length','')
                    if length.isdecimal() and len(raw)<min(int(length),limit+1):
                        raise IncompleteRead(b'',int(length)-len(raw))
                    if len(raw)>limit:raise ValueError('公开发布信息超过大小限制，未继续处理。')
                    return raw,response.headers,final_url
            except Exception as error:
                if not retryable_network_error(error):raise
                if isinstance(error,HTTPError):error.close()
                if attempt==NETWORK_ATTEMPTS-1:raise ValueError(network_failure_message('读取公开发布页')) from error

    def _check_public_release(self):
        try:
            raw,_,url=self._public_request(f'https://github.com/{REPOSITORY}/releases/latest')
            document=parse_latest_page(raw,url)
            if version_tuple(document['tag_name'])>version_tuple(APP_VERSION):
                tag=document['tag_name']
                raw,_,assets_url=self._public_request(f'https://github.com/{REPOSITORY}/releases/expanded_assets/{tag}')
                if assets_url!=f'https://github.com/{REPOSITORY}/releases/expanded_assets/{tag}':
                    raise ValueError('发布附件地址与本项目不符，未继续处理。')
                package=parse_package_asset(raw,tag)
                _,headers,_=self._public_request(asset_url(package['browser_download_url']),method='HEAD')
                size=headers.get('Content-Length','')
                if not size.isdecimal() or not 0<int(size)<=MAX_PACKAGE_BYTES:
                    raise ValueError('无法核对客户端压缩包的实际大小，请打开发布页手动下载。')
                package['size']=int(size)
                document['assets']=[package]
            release=release_info(document)
            release['check_message']='已通过本项目公开发布页完成更新检查。'
            return release
        except UnicodeError:
            raise ValueError('公开发布页内容无法读取，请稍后重新检查，或打开发布页手动下载。') from None
        except ValueError:
            raise
        except (HTTPError,URLError,OSError,UnicodeError) as error:
            if isinstance(error,HTTPError):error.close()
            raise ValueError('GitHub 更新接口暂时受限，公开发布页也未能完成检查。请稍后重新检查，或打开本项目发布页手动下载完整客户端包。') from None

    def _check_api_release(self):
        for attempt in range(NETWORK_ATTEMPTS):
            self._retry_delay(attempt)
            request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", headers={
                "Accept": "application/vnd.github+json", "User-Agent": "ExCatalog/" + APP_VERSION})
            try:
                with self._network_opener(attempt).open(request, timeout=25) as response:
                    raw = response.read(1024**2 + 1)
                    length = getattr(response, "headers", {}).get("Content-Length", "")
                    if length.isdecimal() and len(raw) < min(int(length), 1024**2 + 1):
                        raise IncompleteRead(b"", int(length) - len(raw))
                break
            except Exception as error:
                if isinstance(error,HTTPError) and error.code in {403,429}:
                    self._remember_api_limit(error)
                    error.close()
                    return self._check_public_release()
                if not retryable_network_error(error):raise
                if isinstance(error,HTTPError):error.close()
                if attempt==NETWORK_ATTEMPTS-1:raise ValueError(network_failure_message('检查更新')) from error
        if len(raw)>1024**2:raise ValueError('发布信息超过大小限制。')
        return release_info(json.loads(raw))

    def check(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError("客户端更新正在处理，请稍候。")
        try:
            self.release = None
            self.release = self._check_public_release() if time.time()<self._api_limited_until else self._check_api_release()
            if self.release["available"]:
                self.release["history_message"] = ""
                self.release["notes"], self.release["included_versions"] = announcement_notes(
                    self.release["latest_version"], self.release["notes"], self.history.entries)
            else:
                self.release["notes"], self.release["included_versions"] = announcement_notes(APP_VERSION, RELEASE_NOTES, self.history.entries)
            return self.release
        finally:
            self.lock.release()

    def prepare(self):
        if not getattr(sys, "frozen", False):
            raise ValueError("源码运行请使用 GitHub Desktop 更新；自动安装仅供打包后的桌面客户端使用。")
        if not self.release or not self.release["available"]:
            raise ValueError("请先检查更新，确认存在新版客户端。")
        if not self.lock.acquire(blocking=False):
            raise ValueError("客户端更新正在处理，请稍候。")
        if self.state["phase"] == "ready":
            self.lock.release()
            return dict(self.state)
        self.state = {"phase": "downloading", "message": "正在下载并校验客户端…", "percent": 0}
        threading.Thread(target=self._download, name="client-update-download", daemon=False).start()
        return dict(self.state)

    def _download_archive(self, archive):
        """Resume interrupted transfers, then verify the complete release digest."""
        digest, count = hashlib.sha256(), 0
        size = self.release["size"]
        for attempt in range(NETWORK_ATTEMPTS):
            if attempt:
                self.state["message"] = f"连接中断，正在第 {attempt}/{NETWORK_ATTEMPTS - 1} 次重试下载，保留已下载进度…"
            self._retry_delay(attempt)
            headers = {"User-Agent": "ExCatalog/" + APP_VERSION, "Accept-Encoding": "identity"}
            if count:
                headers["Range"] = f"bytes={count}-"
            request = Request(self.release["url"], headers=headers)
            try:
                with self._network_opener(attempt).open(request, timeout=45) as response:
                    status = getattr(response, "status", 200)
                    response_end = size
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not match:
                            raise ValueError("更新包续传响应缺少有效的字节范围，已拒绝安装。")
                        start, end, total = map(int, match.groups())
                        if start != count or total != size or not start <= end < size:
                            raise ValueError("更新包续传范围与已下载进度不符，已拒绝安装。")
                        response_end = end + 1
                    elif status == 200:
                        if count:
                            count, digest = 0, hashlib.sha256()
                            self.state["percent"] = 0
                    else:
                        raise ValueError("更新包下载响应不正确，已拒绝安装。")
                    self.state["message"] = "正在下载并校验客户端…"
                    with archive.open("ab" if count else "wb") as output:
                        while chunk := response.read(256 * 1024):
                            count += len(chunk)
                            if count > response_end or count > MAX_PACKAGE_BYTES:
                                raise ValueError("下载包大小与 GitHub 发布信息不符。")
                            output.write(chunk)
                            digest.update(chunk)
                            self.state["percent"] = min(95, int(count * 95 / size))
                if count != size:
                    raise IncompleteRead(b"", size - count)
                break
            except Exception as error:
                if not retryable_network_error(error):
                    raise
                if isinstance(error, HTTPError):
                    error.close()
                if count == size:
                    break
                if attempt == NETWORK_ATTEMPTS - 1:
                    raise ValueError(network_failure_message("下载更新包")) from error
        if digest.hexdigest() != self.release["sha256"]:
            raise ValueError("客户端 SHA-256 校验失败，已拒绝安装。")

    def _download(self):
        try:
            if self.workspace:
                shutil.rmtree(self.workspace)
            self.workspace = Path(tempfile.mkdtemp(prefix="excatalog-update-"))
            archive = self.workspace / PACKAGE_NAME
            self._download_archive(archive)
            self.state["message"] = "校验通过，正在准备更新…"
            unpack_package(archive, self.workspace / "payload")
            # Run the current trusted updater from a copy, outside the target directory.
            program = Path(sys.executable).resolve().parent
            helper = self.workspace / "helper"
            helper.mkdir()
            shutil.copy2(sys.executable, helper / "ExCatalog.exe")
            shutil.copytree(program / "_internal", helper / "_internal")
            plan = {"parent_pid": os.getpid(), "target": str(program), "data_root": str(self.root.resolve())}
            (self.workspace / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            self.state = {"phase": "ready", "message": "新版已准备好，点击「安装并重新打开」完成更新。", "percent": 100}
        except Exception as error:
            self.state = {"phase": "error", "message": str(error), "percent": 0}
            if self.workspace:
                shutil.rmtree(self.workspace, ignore_errors=True)
                self.workspace = None
        finally:
            self.lock.release()

    def launch_installer(self):
        if self.state["phase"] not in {"ready", "installing"} or not self.workspace:
            raise ValueError("请先下载并校验新版客户端。")
        environment = os.environ.copy()
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen([str(self.workspace / "helper" / "ExCatalog.exe"), "--apply-client-update", str(self.workspace / "plan.json")],
                         env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def apply_update(plan_file, relaunch=True):
    """Packaged helper: wait for the old process, update with rollback, relaunch."""
    plan_file = Path(plan_file).resolve()
    if plan_file.name != "plan.json" or not plan_file.parent.name.startswith("excatalog-update-"):
        raise ValueError("更新计划路径不正确。")
    workspace = plan_file.parent
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    if os.name != "nt":
        raise ValueError("自动客户端更新仅支持 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, int(plan["parent_pid"]))
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, 120000) != 0:
                raise ValueError("原客户端尚未退出，更新已取消。")
        finally:
            kernel.CloseHandle(handle)
    elif ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: parent already exited.
        raise OSError("无法确认原客户端已退出，更新已取消。")
    target = Path(plan["target"]).resolve()
    from runtime_paths import library_lock
    try:
        with library_lock(Path(plan["data_root"])):
            replace_program(workspace / "payload", target, workspace / "previous-program")
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, f"客户端更新未完成，已保留或恢复原程序。\n{error}", "藏目 · 客户端更新", 0x40)
        raise
    if relaunch:
        environment = os.environ.copy()
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen([str(target / "ExCatalog.exe"), "--data-root", plan["data_root"]], cwd=target,
                         env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    # Keep the previous program in the temp folder for manual recovery if needed.
    return 0
