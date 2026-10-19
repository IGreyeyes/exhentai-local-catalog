"""Private Windows proxy preferences and shared, per-request network routing."""

from base64 import b64encode
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import ipaddress
import json
import os
import re
import ssl
import tempfile
import threading
import time

from credentials import _dpapi

DEFAULTS = {"enabled": False, "fallback": "system", "host": "127.0.0.1", "port": 7890,
            "username": "", "password": "", "domains": []}
TEST_TARGETS = {"e-hentai": ("E-Hentai", "https://e-hentai.org/"),
                "exhentai": ("ExHentai", "https://exhentai.org/"),
                "github": ("GitHub", "https://api.github.com/")}


def hostname(value):
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError("代理地址或域名格式不正确。")
    value = value.strip().lower().rstrip(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("代理地址或域名格式不正确。") from None
    if not value or len(value) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split(".")):
        raise ValueError("请填写 IP 或域名，不包含协议、端口、路径或通配符。")
    return value


def validate(values, previous=None):
    if not isinstance(values, dict) or set(values) - (set(DEFAULTS) | {"clear_password"}):
        raise ValueError("网络设置包含不支持的字段。")
    result = {**DEFAULTS, **values}
    if type(result["enabled"]) is not bool or result["fallback"] not in {"system", "direct"}:
        raise ValueError("请选择有效的代理启用状态和关闭后的连接方式。")
    result["host"] = hostname(result["host"])
    if type(result["port"]) not in {str, int} or not re.fullmatch(r"[0-9]{1,5}", str(result["port"])) or not 1 <= int(result["port"]) <= 65535:
        raise ValueError("代理端口须为 1～65535 的整数。")
    result["port"] = int(result["port"])
    for key in ("username", "password"):
        value = result[key]
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("代理认证信息过长或包含无效字符。")
    if ":" in result["username"]:
        raise ValueError("代理用户名不能包含冒号。")
    clear = result.pop("clear_password", False)
    if type(clear) is not bool:
        raise ValueError("清除密码须为开关值。")
    if clear:
        result["password"] = ""
    elif not result["password"] and previous and previous["password"]:
        if any(result[key] != previous[key] for key in ("host", "port", "username")):
            raise ValueError("代理地址、端口或用户名已变化，请重新输入密码，或勾选清除已保存密码。")
        result["password"] = previous["password"]
    if result["password"] and not result["username"]:
        raise ValueError("填写代理密码时也需填写用户名。")
    domains = result["domains"]
    if isinstance(domains, str) and len(domains) <= 8000:
        domains = domains.splitlines()
    if not isinstance(domains, list) or len(domains) > 100 or any(not isinstance(item, str) for item in domains):
        raise ValueError("代理列表最多填写 100 个域名，每行一个。")
    result["domains"] = list(dict.fromkeys(hostname(item) for item in domains if item.strip()))
    return result


def route_for(config, url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost" or host.endswith(".localhost")
    if local:
        return "direct"
    if not config["enabled"]:
        return config["fallback"]
    if config["domains"] and not any(host == domain or host.endswith("." + domain) for domain in config["domains"]):
        return "direct"
    return "proxy"


class ExplicitProxyHandler(ProxyHandler):
    """Apply an explicit proxy even when the OS no-proxy list says otherwise."""

    def __init__(self, config):
        self.config = config
        super().__init__({"http": "configured", "https": "configured"})

    def proxy_open(self, request, proxy, protocol):
        if route_for(self.config, request.full_url) != "proxy":
            return None
        host = self.config["host"]
        address = (f"[{host}]" if ":" in host else host) + ":" + str(self.config["port"])
        if self.config["username"]:
            token = b64encode((self.config["username"] + ":" + self.config["password"]).encode("utf-8")).decode("ascii")
            # Do not copy proxy credentials when the origin redirects elsewhere.
            # urllib also removes this header from HTTPS requests after CONNECT.
            request.add_unredirected_header("Proxy-Authorization", "Basic " + token)
        request.set_proxy(address, "http")
        return None


def proxy_handler(config):
    if config["enabled"]:
        return ExplicitProxyHandler(config)
    return ProxyHandler({}) if config["fallback"] == "direct" else ProxyHandler()


def network_urlopen(request, timeout=40, network=None):
    return build_opener(network.handler() if network is not None else ProxyHandler()).open(request, timeout=timeout)


class NoTestRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NetworkSettings:
    def __init__(self, root):
        self.path = Path(root) / "data" / "network-settings.ehproxy"
        self.lock = threading.RLock()
        self.test_lock = threading.Lock()
        self.test_after = 0
        self._signature = None
        self._values = dict(DEFAULTS)
        self._error = ""

    def _load(self):
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            self._signature, self._values, self._error = None, dict(DEFAULTS), ""
            return
        except OSError:
            self._error = "无法读取网络设置，请检查文件权限并重新保存。"
            return
        if signature == self._signature:
            return
        try:
            if stat.st_size > 65536:
                raise ValueError
            document = json.loads(_dpapi(self.path.read_bytes(), False))
            if document.get("version") != 1:
                raise ValueError
            self._values = validate(document["settings"])
            self._error = ""
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self._error = "无法解密网络设置，请在当前 Windows 用户下重新填写并保存；联网请求已停止。"
        self._signature = signature

    def snapshot(self):
        with self.lock:
            self._load()
            if self._error:
                raise ValueError(self._error)
            return {**self._values, "domains": list(self._values["domains"])}

    def status(self):
        with self.lock:
            self._load()
            values = DEFAULTS if self._error else self._values
            return {**{key: value for key, value in values.items() if key != "password"},
                    "has_password": bool(values["password"]), "error": self._error,
                    "saved": self._signature is not None}

    def candidate(self, values):
        with self.lock:
            self._load()
            return validate(values, None if self._error else self._values)

    def save(self, values):
        with self.lock:
            candidate = self.candidate(values)
            temporary = None
            try:
                raw = _dpapi(json.dumps({"version": 1, "settings": candidate}, ensure_ascii=True).encode(), True)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=self.path.parent, suffix=".partial", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            except (ValueError, OSError):
                raise ValueError("无法保存代理设置。请确认使用 Windows，且资料目录可写；原设置已保留。") from None
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            self._signature = None
            return self.status()

    def handler(self, legacy_proxy="", direct_fallback=False):
        config = self.snapshot()
        if not config["enabled"] and config["fallback"] == "system":
            if legacy_proxy:
                return ProxyHandler({"http": legacy_proxy, "https": legacy_proxy})
            if direct_fallback:
                return ProxyHandler({})
        return proxy_handler(config)

    def test(self, values, target):
        if not isinstance(target, str) or target not in TEST_TARGETS:
            raise ValueError("请选择 E-Hentai、ExHentai 或 GitHub 作为测试目标。")
        config = self.candidate(values)
        if not self.test_lock.acquire(blocking=False):
            raise ValueError("连接测试正在进行，请稍候。")
        try:
            if time.monotonic() < self.test_after:
                raise ValueError("请间隔至少 5 秒再测试。")
            self.test_after = time.monotonic() + 5
            label, url = TEST_TARGETS[target]
            route = route_for(config, url)
            started = time.monotonic()
            result = {"target": label, "route": route, "ok": False, "reachable": False, "http_status": None}
            try:
                request = Request(url, headers={"User-Agent": "ExCatalog-Connection-Test/1", "Accept-Encoding": "identity"})
                with build_opener(proxy_handler(config), NoTestRedirect()).open(request, timeout=15) as response:
                    result.update(http_status=response.status, reachable=True)
                    body = response.read(16384).lower()
                    blocked = any(marker in body for marker in (b"cf-chl-", b"just a moment...", b"excessive pageloads", b"temporarily banned"))
                    result["ok"] = 200 <= response.status < 300 and not blocked
                    result["message"] = "连接成功。此测试不检查账号登录或作品访问权限。" if result["ok"] else "已收到响应，但目标要求浏览器验证或限制了访问。"
            except HTTPError as error:
                code = error.code
                error.close()
                result.update(http_status=code, reachable=code != 407)
                result["message"] = {407: "代理认证失败，请核对用户名和密码。", 401: "已连接到目标，目标要求登录。",
                                     403: "已收到目标响应，但访问被拒绝；可能需要登录或浏览器验证。",
                                     429: "目标正在限流，请稍后再试。"}.get(code, "目标返回跳转，尚未验证目标可用。" if 300 <= code < 400 else "目标返回错误响应，请稍后再试。")
            except (URLError, OSError, HTTPException) as error:
                reason = getattr(error, "reason", error)
                if "407" in str(reason):
                    result.update(http_status=407, message="代理认证失败，请核对用户名和密码。")
                elif isinstance(reason, ssl.SSLError):
                    result["message"] = "TLS 证书验证失败，请检查网络与系统时间。"
                elif isinstance(reason, TimeoutError):
                    result["message"] = "连接超时，请检查代理服务、端口及网络。"
                else:
                    result["message"] = "连接失败，请检查代理是否已启动、地址端口及网络是否正确。"
            result["elapsed_ms"] = max(0, round((time.monotonic() - started) * 1000))
            return result
        finally:
            self.test_lock.release()
