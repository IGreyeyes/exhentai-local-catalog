"""Proxy behavior uses disposable libraries, local fake proxies and test credentials."""
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import json
import os
import tempfile
import threading
import time
import unittest

from network_settings import DEFAULTS, ExplicitProxyHandler, NetworkSettings, proxy_handler, route_for, validate


class FakeProxy(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.calls.append((self.command, self.path, dict(self.headers)))
        self.send_response(self.server.response_code)
        if self.server.redirect:
            self.send_header("Location", self.server.redirect)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()
        self.wfile.write(self.server.body)

    def do_CONNECT(self):
        self.server.calls.append((self.command, self.path, dict(self.headers)))
        self.send_error(407, "Proxy authentication required")

    def log_message(self, *args):
        pass


def start_proxy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProxy)
    server.calls, server.body, server.response_code, server.redirect = [], b"<html>Test source</html>", 200, ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.network = NetworkSettings(self.root)
        self.proxy, self.thread = start_proxy()
        self.config = {**DEFAULTS, "enabled": True, "port": self.proxy.server_port,
                       "username": "test-proxy-user", "password": "test-proxy-password"}

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def test_invalid_addresses_ports_auth_and_domains_are_rejected(self):
        cases = [{"host": "http://localhost"}, {"host": "localhost/path"}, {"host": "user@localhost"},
                 {"host": "localhost\r\nTest: bad"}, {"port": 0}, {"port": 65536}, {"port": True},
                 {"port": 1.5}, {"username": "test:bad"}, {"password": "test\ninvalid"},
                 {"domains": ["https://example.invalid"]}, {"domains": ["*.example.invalid"]},
                 {"enabled": 1}, {"clear_password": "false"}, {"unexpected": True}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**self.config, **changes})
        self.assertEqual(validate({**DEFAULTS, "host": "[::1]"})["host"], "::1")

    def test_domain_matching_loopback_and_direct_modes(self):
        config = validate({**self.config, "domains": "Example.invalid\nexample.invalid\n"})
        self.assertEqual(config["domains"], ["example.invalid"])
        for host in ("example.invalid", "sub.example.invalid"):
            self.assertEqual(route_for(config, "https://" + host), "proxy")
        for host in ("notexample.invalid", "example.invalid.evil.invalid", "localhost", "127.0.0.1", "[::1]"):
            self.assertEqual(route_for(config, "https://" + host), "direct")
        self.assertEqual(route_for(DEFAULTS, "https://example.invalid"), "system")
        self.assertEqual(route_for({**DEFAULTS, "fallback": "direct"}, "https://example.invalid"), "direct")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_encrypted_persistence_password_retention_clear_and_endpoint_change(self):
        status = self.network.save(self.config)
        self.assertTrue(status["has_password"])
        self.assertNotIn("password", status)
        raw = self.network.path.read_bytes()
        for private in (b"127.0.0.1", b"test-proxy-user", b"test-proxy-password"):
            self.assertNotIn(private, raw)
        reopened = NetworkSettings(self.root)
        self.assertEqual(reopened.snapshot(), self.config)
        self.network.save({**self.config, "password": ""})
        self.assertEqual(reopened.snapshot()["password"], self.config["password"])
        with self.assertRaisesRegex(ValueError, "重新输入密码"):
            self.network.save({**self.config, "password": "", "host": "other.example.invalid"})
        self.network.save({**self.config, "password": "", "clear_password": True})
        self.assertFalse(reopened.status()["has_password"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_failed_save_preserves_previous_file_and_broken_file_stops_network(self):
        self.network.save(self.config)
        before = self.network.path.read_bytes()
        with patch("network_settings.os.replace", side_effect=OSError("test-private-path")):
            with self.assertRaisesRegex(ValueError, "原设置已保留") as raised:
                self.network.save({**self.config, "port": 7890})
        self.assertNotIn("test-private-path", str(raised.exception))
        self.assertEqual(self.network.path.read_bytes(), before)
        self.network.path.write_bytes(b"invalid test ciphertext")
        self.assertTrue(self.network.status()["error"])
        with self.assertRaisesRegex(ValueError, "联网请求已停止"):
            self.network.handler()
        self.network.save(DEFAULTS)
        self.assertFalse(self.network.status()["error"])

    def test_http_proxy_is_used_even_with_system_bypass_and_credentials_are_separate(self):
        with patch.dict(os.environ, {"no_proxy": "*"}):
            opener = build_opener(proxy_handler(self.config))
            with opener.open("http://source.example.invalid/", timeout=2) as response:
                self.assertEqual(response.read(), self.proxy.body)
        command, url, headers = self.proxy.calls[0]
        self.assertEqual((command, url), ("GET", "http://source.example.invalid/"))
        expected = "Basic " + b64encode(b"test-proxy-user:test-proxy-password").decode()
        self.assertEqual(headers["Proxy-Authorization"], expected)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_proxy_credentials_are_not_forwarded_to_redirected_origin(self):
        origin, thread = start_proxy()
        try:
            self.proxy.response_code = 302
            self.proxy.redirect = f"http://127.0.0.1:{origin.server_port}/"
            with build_opener(proxy_handler(self.config)).open("http://source.example.invalid/", timeout=2) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(len(origin.calls), 1)
            self.assertNotIn("Proxy-Authorization", origin.calls[0][2])
        finally:
            origin.shutdown(); origin.server_close(); thread.join()

    def test_https_connect_auth_failure_is_detected_and_sanitized(self):
        result = self.network.test(self.config, "github")
        self.assertFalse(result["ok"])
        self.assertFalse(result["reachable"])
        self.assertEqual(result["http_status"], 407)
        command, url, headers = self.proxy.calls[0]
        self.assertEqual(command, "CONNECT")
        self.assertEqual(url, "api.github.com:443")
        self.assertIn("Proxy-Authorization", headers)
        self.assertNotIn("test-proxy-password", json.dumps(result))

    def test_unsaved_settings_test_success_status_failure_redirect_and_rate_limit(self):
        with patch.dict("network_settings.TEST_TARGETS", {"github": ("模拟站点", "http://source.example.invalid/")}):
            success = self.network.test(self.config, "github")
            self.assertTrue(success["ok"])
            self.assertEqual(success["route"], "proxy")
            self.assertGreaterEqual(success["elapsed_ms"], 0)
            self.assertFalse(self.network.path.exists())
            with self.assertRaisesRegex(ValueError, "5 秒"):
                self.network.test(self.config, "github")
            for code in (403, 429, 502, 302):
                self.network.test_after = 0
                self.proxy.response_code = code
                self.proxy.redirect = "http://other.example.invalid/" if code == 302 else ""
                result = self.network.test(self.config, "github")
                self.assertFalse(result["ok"])
                self.assertTrue(result["reachable"])
                self.assertEqual(result["http_status"], code)
            self.assertEqual(len(self.proxy.calls), 5)

    def test_test_timeout_and_connection_errors_do_not_echo_details(self):
        for error in (TimeoutError("test-proxy-password"), URLError("test-private-proxy-endpoint")):
            self.network.test_after = 0
            with patch("network_settings.build_opener") as opener:
                opener.return_value.open.side_effect = error
                result = self.network.test(self.config, "github")
            self.assertFalse(result["ok"])
            self.assertNotIn("test-proxy-password", json.dumps(result))
            self.assertNotIn("test-private-proxy-endpoint", json.dumps(result))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_updater_and_release_history_do_not_fallback_around_explicit_proxy(self):
        from desktop_updates import ClientUpdater
        from release_history import ReleaseHistory
        updater = ClientUpdater(self.root)
        self.network.save(self.config)
        for attempt in range(4):
            self.assertTrue(any(isinstance(handler, ExplicitProxyHandler) for handler in updater._network_opener(attempt).handlers))
        history = ReleaseHistory(network=self.network)
        handlers = []
        with patch("release_history.build_opener") as opener:
            def failing(handler):
                handlers.append(handler)
                raise URLError("test failure")
            opener.side_effect = failing
            with self.assertRaises(ValueError):
                history._request("https://api.github.com/")
        self.assertEqual(len(handlers), 2)
        self.assertTrue(all(isinstance(handler, ExplicitProxyHandler) for handler in handlers))
        self.network.save({**self.config, "enabled": False, "fallback": "direct"})
        self.assertNotIn("Proxy-Authorization", dict(Request("https://example.invalid/").header_items()))
        self.assertEqual(self.network.handler(legacy_proxy="http://127.0.0.1:7890").proxies, {})

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_https_retries_keep_tunneling_to_the_original_target(self):
        from desktop_updates import ClientUpdater, NETWORK_ATTEMPTS
        from release_history import ReleaseHistory
        self.network.save(self.config)
        updater = ClientUpdater(self.root)
        with patch("desktop_updates.time.sleep"), self.assertRaises(ValueError):
            updater.check()
        self.assertEqual(len(self.proxy.calls), NETWORK_ATTEMPTS)
        self.assertTrue(all(command == "CONNECT" and target == "api.github.com:443" for command, target, _ in self.proxy.calls))
        self.proxy.calls.clear()
        with self.assertRaises(ValueError):
            ReleaseHistory(network=self.network)._request("https://api.github.com/")
        self.assertEqual(len(self.proxy.calls), 2)
        self.assertTrue(all(command == "CONNECT" and target == "api.github.com:443" for command, target, _ in self.proxy.calls))


class NetworkApiTests(unittest.TestCase):
    def setUp(self):
        from app import Catalog, Server
        from tests.test_desktop import make_library
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # The shared fixture only creates a synthetic local catalog.
        path = make_library(self.root)
        self.server = Server(("127.0.0.1", 0), Catalog(path))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temporary.cleanup()

    def post(self, path, values, authorized=True):
        headers = {"Content-Type": "application/json", "Origin": self.base,
                   "X-Catalog-Token": self.server.action_token if authorized else ""}
        with self.opener.open(Request(self.base + path, data=json.dumps(values).encode(), headers=headers), timeout=5) as response:
            return json.load(response)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_api_authorization_persistence_and_verification_reset(self):
        with self.assertRaises(HTTPError) as raised:
            self.post("/api/network/settings", DEFAULTS, authorized=False)
        self.assertEqual(raised.exception.code, 403); raised.exception.close()
        collector = self.server.collector
        collector.verified = True
        collector.preview_data = {"test": True}
        collector.verification_unavailable.add(1)
        collector.cooldown_until = time.time() + 1000
        cooldown = collector.cooldown_until
        result = self.post("/api/network/settings", {**DEFAULTS, "fallback": "direct"})
        self.assertEqual(result["fallback"], "direct")
        self.assertFalse(collector.verified)
        self.assertIsNone(collector.preview_data)
        self.assertFalse(collector.verification_unavailable)
        self.assertEqual(collector.cooldown_until, cooldown)
        self.assertEqual(NetworkSettings(self.root).status()["fallback"], "direct")
        with self.assertRaises(HTTPError) as raised:
            self.post("/api/network/test", {"settings": DEFAULTS, "target": "exhentai"})
        self.assertEqual(raised.exception.code, 400)
        self.assertIn("保护冷却", json.load(raised.exception)["error"])
        raised.exception.close()

    def test_busy_collection_blocks_saving_and_unknown_test_target_is_rejected(self):
        collector = self.server.collector
        collector.thread = self.thread
        try:
            with self.assertRaises(HTTPError) as raised:
                self.post("/api/network/settings", DEFAULTS)
            self.assertIn("先暂停采集", json.load(raised.exception)["error"])
            raised.exception.close()
        finally:
            collector.thread = None
        with self.assertRaises(HTTPError) as raised:
            self.post("/api/network/test", {"settings": DEFAULTS, "target": "http://localhost/"})
        self.assertEqual(raised.exception.code, 400); raised.exception.close()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_collector_proxy_autosaves_without_login_and_survives_restart_and_clear(self):
        from favorites import Collector
        route = "/api/collector/proxy"
        proxy = "http://127.0.0.1:7890"
        with self.assertRaises(HTTPError) as raised:
            self.post(route, {"proxy": proxy}, authorized=False)
        self.assertEqual(raised.exception.code, 403); raised.exception.close()
        self.assertTrue(self.post(route, {"proxy": proxy})["saved"])
        collector = Collector(self.server.catalog)
        self.assertFalse(collector.status()["configured"])
        self.assertEqual(collector.status()["proxy"], proxy)
        self.assertFalse(collector.status()["has_credentials"])
        with collector.store.connection() as db:
            stored = db.execute("SELECT value FROM app_settings WHERE key='collector_proxy'").fetchone()[0]
        self.assertNotIn("127.0.0.1", stored)
        self.assertNotIn("7890", stored)
        collector.configure({"host": "e-hentai.org"})
        collector.verified = True
        self.server.collector = collector
        self.post(route, {"proxy": "http://127.0.0.1:7891"})
        self.assertEqual(collector.settings["proxy"], proxy, "Autosave must not silently change an applied session")
        self.assertTrue(collector.verified)
        collector.configure({"host": "e-hentai.org", "proxy": "http://127.0.0.1:7891"})
        self.assertFalse(collector.verified)
        self.post(route, {"proxy": ""})
        self.assertEqual(Collector(self.server.catalog).status()["proxy"], "")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_collector_proxy_validation_and_failed_save_preserve_previous_value(self):
        from favorites import Collector
        route = "/api/collector/proxy"
        proxy = "http://127.0.0.1:7890"
        self.post(route, {"proxy": proxy})
        for invalid in (None, "socks5://localhost:7890", "http://test-user:test-password@localhost:7890", "http://localhost:0", "http://localhost:99999", "http://localhost\n", "http://[bad"):
            with self.assertRaises(HTTPError) as raised:
                self.post(route, {"proxy": invalid})
            self.assertEqual(raised.exception.code, 400); raised.exception.close()
        with patch("favorites._dpapi", side_effect=OSError("test-sensitive-details")):
            with self.assertRaises(HTTPError) as raised:
                self.post(route, {"proxy": "http://127.0.0.1:7891"})
            self.assertNotIn("test-sensitive-details", json.load(raised.exception)["error"])
            raised.exception.close()
        self.assertEqual(Collector(self.server.catalog).status()["proxy"], proxy)
        self.server.collector.thread = self.thread
        try:
            with self.assertRaises(HTTPError) as raised:
                self.post(route, {"proxy": ""})
            self.assertIn("先暂停", json.load(raised.exception)["error"]); raised.exception.close()
        finally:
            self.server.collector.thread = None


if __name__ == "__main__":
    unittest.main()
