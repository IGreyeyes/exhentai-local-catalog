from pathlib import Path
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
from unittest.mock import patch
import hashlib
import io
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

import desktop_updates as updates
from client_version import APP_VERSION, PACKAGE_NAME, REPOSITORY


def archive_bytes(extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("ExCatalog/ExCatalog.exe", b"new-exe")
        archive.writestr("ExCatalog/_internal/python314.dll", b"new-runtime")
        archive.writestr("ExCatalog/README.md", "public instructions")
        for name, value in extra:
            archive.writestr(name, value)
    return output.getvalue()


class DownloadResponse(io.BytesIO):
    def __init__(self, raw, status=200, content_range=None, interruption=None):
        super().__init__(raw)
        self.status = status
        self.headers = {"Content-Range": content_range} if content_range is not None else {}
        self.interruption = interruption

    def read(self, size=-1):
        if self.interruption is not None and self.tell() == len(self.getvalue()):
            raise self.interruption
        return super().read(size)


class ClientUpdateTests(unittest.TestCase):
    def setUp(self):
        history_network = patch("release_history.ReleaseHistory._request", return_value=[])
        history_network.start()
        self.addCleanup(history_network.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.updater = updates.ClientUpdater(self.root)

    def tearDown(self):
        if self.updater.workspace:
            import shutil
            shutil.rmtree(self.updater.workspace, ignore_errors=True)
        self.directory.cleanup()

    def release(self, raw=b"test"):
        return {"tag_name": "v1.2.0", "body": "更新内容", "assets": [{"name": PACKAGE_NAME, "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/v1.2.0/{PACKAGE_NAME}"}]}

    def test_release_selection_and_version_comparison(self):
        result = updates.release_info(self.release())
        self.assertTrue(result["available"])
        self.assertEqual(result["latest_version"], "1.2.0")
        self.assertFalse(updates.release_info({"tag_name": "v1.0.0"})["available"])
        self.assertFalse(updates.release_info({"tag_name": APP_VERSION})["available"])

    def test_reject_missing_checksum_and_wrong_repository(self):
        for key, value in (("digest", ""), ("size", True), ("browser_download_url", "https://github.com/other/repo/releases/download/v1.2.0/package.zip")):
            document = self.release()
            document["assets"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                updates.release_info(document)
        for version in ("v1.2.0-beta", "v1.2.0/../evil", None):
            with self.assertRaises(ValueError):
                updates.version_tuple(version)

    def test_read_notes_persists_per_version_across_instances(self):
        self.assertTrue(self.updater.info()["show_notes"])
        self.updater.acknowledge()
        self.assertFalse(updates.ClientUpdater(self.root).info()["show_notes"])
        with patch("desktop_updates.APP_VERSION", "1.2.0"):
            self.assertTrue(self.updater.info()["show_notes"])
        (self.root / "data" / "client-update-state.json").write_text("broken", encoding="utf-8")
        self.assertTrue(self.updater.info()["show_notes"])

    def test_upload_guide_persists_until_visit_and_is_not_repeated_in_future_versions(self):
        with patch("desktop_updates.APP_VERSION","1.1.16"):
            self.assertTrue(self.updater.info()["upload_guide_url"].startswith("/maintenance"))
            with self.assertRaises(ValueError):self.updater.acknowledge_upload_guide()
            self.assertTrue(self.updater.acknowledge()["upload_guide_url"])
            reopened=updates.ClientUpdater(self.root)
            self.assertFalse(reopened.info()["show_notes"])
            self.assertTrue(reopened.info()["upload_guide_url"])
            reopened.acknowledge_upload_guide()
            self.assertFalse(updates.ClientUpdater(self.root).info()["upload_guide_url"])
            reopened.acknowledge()
            self.assertFalse(reopened.info()["upload_guide_url"])
        with patch("desktop_updates.APP_VERSION","1.1.17"):
            self.assertTrue(self.updater.info()["show_notes"])
            self.assertFalse(self.updater.info()["upload_guide_url"])

    def test_startup_check_runs_once_and_notification_is_consumed_once(self):
        document=json.dumps(self.release()).encode()
        entered,finish=threading.Event(),threading.Event()
        def response(*args,**kwargs):
            entered.set()
            if not finish.wait(5):raise TimeoutError("Test release check timed out")
            return io.BytesIO(document)
        with patch.object(self.updater.opener,"open",side_effect=response) as network:
            self.updater.start_startup_check()
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.updater.start_startup_check()["phase"],"checking")
            finish.set();self.updater._startup_thread.join(5)
            self.assertFalse(self.updater._startup_thread.is_alive())
            self.assertEqual(self.updater.start_startup_check()["phase"],"completed")
            self.assertEqual(network.call_count,1)
        self.assertTrue(self.updater.take_startup_prompt()["prompt"])
        self.assertFalse(self.updater.take_startup_prompt()["prompt"])
        self.assertIsNone(self.updater.workspace)
        reopened=updates.ClientUpdater(self.root)
        with patch.object(reopened.opener,"open",return_value=io.BytesIO(document)) as network:
            reopened.start_startup_check();reopened._startup_thread.join(5)
            self.assertEqual(network.call_count,1)
        self.assertTrue(reopened.take_startup_prompt()["prompt"])

    def test_startup_check_without_update_or_when_offline_stays_quiet(self):
        with patch.object(self.updater.opener,"open",return_value=io.BytesIO(json.dumps({"tag_name":APP_VERSION}).encode())):
            self.updater.start_startup_check();self.updater._startup_thread.join(5)
        self.assertEqual(self.updater.startup_status()["phase"],"completed")
        self.assertFalse(self.updater.take_startup_prompt()["prompt"])
        offline=updates.ClientUpdater(self.root)
        with patch.object(offline.opener,"open",side_effect=TimeoutError("Offline test")) as network, patch.object(offline.direct_opener,"open",side_effect=TimeoutError("Offline test")) as direct, patch("desktop_updates.time.sleep"):
            offline.start_startup_check();offline._startup_thread.join(5)
            self.assertEqual(offline.start_startup_check()["phase"],"failed")
            self.assertEqual(network.call_count + direct.call_count, updates.NETWORK_ATTEMPTS)
        self.assertFalse(offline.take_startup_prompt()["prompt"])

    def unpack(self, raw):
        archive = self.root / "package.zip"
        archive.write_bytes(raw)
        return updates.unpack_package(archive, self.root / "payload")

    def test_valid_package_and_private_files_are_rejected_before_extraction(self):
        for name in ("ExCatalog/data/favorites.sqlite3", "ExCatalog/../secret", "ExCatalog/_internal/../../data/secret",
                     "ExCatalog/_internal/C:secret", "ExCatalog/_internal/CON.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.unpack(archive_bytes([(name, b"bad")]))
            self.assertFalse((self.root / "payload").exists())
        payload = self.unpack(archive_bytes())
        self.assertEqual((payload / "ExCatalog.exe").read_bytes(), b"new-exe")

    def test_raw_backslash_path_rejected(self):
        raw = archive_bytes([("ExCatalog/_internal/a-secret", b"bad")]).replace(b"a-secret", b"a\\secret")
        with self.assertRaises(ValueError):
            self.unpack(raw)

    def test_symlink_and_duplicate_members_rejected(self):
        symlink = zipfile.ZipInfo("ExCatalog/_internal/link")
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extras in ([(symlink, "../../data")], [("ExCatalog/EXCATALOG.EXE", b"duplicate")]):
            with self.assertRaises(ValueError):
                self.unpack(archive_bytes(extras))

    def installed_fixture(self):
        target = self.root / "installed"
        (target / "_internal").mkdir(parents=True)
        (target / "_internal" / "python314.dll").write_bytes(b"old-runtime")
        (target / "ExCatalog.exe").write_bytes(b"old-exe")
        (target / "data").mkdir()
        (target / "data" / "favorites.sqlite3").write_bytes(b"private-records")
        (target / "README.md").write_bytes(b"old-readme")
        return target

    def test_install_preserves_personal_files_and_saves_old_program(self):
        target = self.installed_fixture()
        payload = self.unpack(archive_bytes())
        backup = self.root / "previous-program"
        updates.replace_program(payload, target, backup)
        self.assertEqual((target / "data" / "favorites.sqlite3").read_bytes(), b"private-records")
        self.assertEqual((target / "ExCatalog.exe").read_bytes(), b"new-exe")
        self.assertEqual((backup / "ExCatalog.exe").read_bytes(), b"old-exe")

    def test_partial_install_rolls_back_all_program_files(self):
        target = self.installed_fixture()
        payload = self.unpack(archive_bytes())
        with patch("desktop_updates.shutil.copytree", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                updates.replace_program(payload, target, self.root / "previous-program")
        self.assertEqual((target / "ExCatalog.exe").read_bytes(), b"old-exe")
        self.assertEqual((target / "_internal" / "python314.dll").read_bytes(), b"old-runtime")
        self.assertEqual((target / "README.md").read_bytes(), b"old-readme")
        self.assertEqual((target / "data" / "favorites.sqlite3").read_bytes(), b"private-records")

    def download(self, raw, checksum=None):
        program = self.installed_fixture()
        self.updater.release = updates.release_info(self.release(raw))
        if checksum:
            self.updater.release["sha256"] = checksum
        with patch.object(self.updater.opener, "open", return_value=io.BytesIO(raw)), patch("sys.frozen", True, create=True), patch("sys.executable", str(program / "ExCatalog.exe")):
            self.updater.prepare()
            self.assertTrue(self.updater.lock.acquire(timeout=5))
            self.updater.lock.release()
        return self.updater.state

    def test_updater_uses_system_proxy_with_separate_direct_fallback(self):
        with patch("desktop_updates.ProxyHandler", wraps=ProxyHandler) as handler:
            updates.ClientUpdater(self.root)
        self.assertEqual(handler.call_args_list[0].args, ())
        self.assertEqual(handler.call_args_list[1].args, ({},))

    def test_release_check_recovers_from_reset_and_uses_direct_fallback(self):
        with patch.object(self.updater.opener, "open", side_effect=URLError(ConnectionResetError(10054, "simulated reset"))), patch.object(self.updater.direct_opener, "open", return_value=io.BytesIO(json.dumps(self.release()).encode())) as direct, patch("desktop_updates.time.sleep"):
            self.assertTrue(self.updater.check()["available"])
        self.assertEqual(direct.call_count, 1)
        self.assertFalse(self.updater.lock.locked())

    def test_incomplete_release_response_is_retried(self):
        document = json.dumps(self.release()).encode()
        short = DownloadResponse(document[:37])
        short.headers["Content-Length"] = str(len(document))
        with patch.object(self.updater.opener, "open", return_value=short), patch.object(self.updater.direct_opener, "open", return_value=io.BytesIO(document)), patch("desktop_updates.time.sleep"):
            self.assertTrue(self.updater.check()["available"])

    def simulated_download(self, raw, responses, checksum=None):
        program = self.installed_fixture()
        self.updater.release = updates.release_info(self.release(raw))
        if checksum:
            self.updater.release["sha256"] = checksum
        requests = []
        def open_response(request, **kwargs):
            requests.append(request)
            value = responses[len(requests) - 1]
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(self.updater.opener, "open", side_effect=open_response), patch.object(self.updater.direct_opener, "open", side_effect=open_response), patch("desktop_updates.time.sleep"), patch("sys.frozen", True, create=True), patch("sys.executable", str(program / "ExCatalog.exe")):
            self.updater.prepare()
            self.assertTrue(self.updater.lock.acquire(timeout=5))
            self.updater.lock.release()
        self.assertEqual((program / "ExCatalog.exe").read_bytes(), b"old-exe")
        self.assertEqual((program / "data" / "favorites.sqlite3").read_bytes(), b"private-records")
        return requests

    def test_connection_reset_before_download_is_retried(self):
        raw = archive_bytes()
        requests = self.simulated_download(raw, [ConnectionResetError(10054, "simulated reset"), DownloadResponse(raw)])
        self.assertEqual(self.updater.state["phase"], "ready")
        self.assertEqual(len(requests), 2)
        self.assertIsNone(requests[1].get_header("Range"))

    def test_reset_during_download_resumes_at_saved_offset(self):
        raw, offset = archive_bytes(), 37
        requests = self.simulated_download(raw, [DownloadResponse(raw[:offset], interruption=ConnectionResetError(10054, "simulated reset")), DownloadResponse(raw[offset:], 206, f"bytes {offset}-{len(raw)-1}/{len(raw)}")])
        self.assertEqual(self.updater.state["phase"], "ready")
        self.assertEqual(requests[1].get_header("Range"), f"bytes={offset}-")
        self.assertEqual((self.updater.workspace / PACKAGE_NAME).read_bytes(), raw)

    def test_short_response_is_retried_and_server_ignoring_range_restarts(self):
        raw = archive_bytes()
        requests = self.simulated_download(raw, [DownloadResponse(raw[:37]), DownloadResponse(raw)])
        self.assertEqual(self.updater.state["phase"], "ready")
        self.assertEqual(requests[1].get_header("Range"), "bytes=37-")
        self.assertEqual((self.updater.workspace / PACKAGE_NAME).read_bytes(), raw)

    def test_disconnect_after_all_bytes_still_requires_valid_checksum(self):
        raw = archive_bytes()
        requests = self.simulated_download(raw, [DownloadResponse(raw, interruption=IncompleteRead(b"", 0))], checksum="0" * 64)
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.updater.state["phase"], "error")
        self.assertIn("SHA-256", self.updater.state["message"])

    def test_invalid_resume_range_fails_without_more_requests(self):
        raw = archive_bytes()
        requests = self.simulated_download(raw, [DownloadResponse(raw[:37]), DownloadResponse(raw[37:], 206, f"bytes 38-{len(raw)-1}/{len(raw)}")])
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.updater.state["phase"], "error")
        self.assertIn("续传范围", self.updater.state["message"])
        self.assertIsNone(self.updater.workspace)

    def test_changed_bytes_after_resume_fail_checksum(self):
        raw = archive_bytes()
        self.simulated_download(raw, [DownloadResponse(raw[:37]), DownloadResponse(b"x" * (len(raw)-37), 206, f"bytes 37-{len(raw)-1}/{len(raw)}")])
        self.assertEqual(self.updater.state["phase"], "error")
        self.assertIn("SHA-256", self.updater.state["message"])
        self.assertIsNone(self.updater.workspace)

    def test_repeated_reset_exhausts_retries_and_cleans_workspace(self):
        requests = self.simulated_download(archive_bytes(), [ConnectionResetError(10054, "simulated reset")] * updates.NETWORK_ATTEMPTS)
        self.assertEqual(len(requests), updates.NETWORK_ATTEMPTS)
        self.assertEqual(self.updater.state["phase"], "error")
        self.assertIn("已自动重试 3 次", self.updater.state["message"])
        self.assertIn("手动下载", self.updater.state["message"])
        self.assertIn("保留 data、logs、backups", self.updater.state["message"])
        self.assertIsNone(self.updater.workspace)

    def test_nontransient_http_and_certificate_errors_are_not_retried(self):
        for error in (HTTPError("https://example.invalid", 404, "not found", {}, None), ssl.SSLCertVerificationError("test certificate"), URLError(ssl.SSLCertVerificationError("test certificate")), OSError("simulated disk failure")):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(updates.retryable_network_error(error))
            if isinstance(error, HTTPError):
                error.close()
        with patch.object(self.updater.opener, "open", side_effect=HTTPError("https://example.invalid", 404, "not found", {}, None)) as network, patch.object(self.updater.direct_opener, "open") as direct:
            with self.assertRaises(HTTPError) as failed:
                self.updater.check()
            failed.exception.close()
        self.assertEqual(network.call_count, 1)
        direct.assert_not_called()
        self.assertFalse(self.updater.lock.locked())

    def test_temporary_http_failure_retries_but_oversized_package_does_not(self):
        raw = archive_bytes()
        requests = self.simulated_download(raw, [HTTPError("https://example.invalid", 503, "unavailable", {}, None), DownloadResponse(raw + b"extra")])
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.updater.state["phase"], "error")
        self.assertIn("大小", self.updater.state["message"])
        self.assertIsNone(self.updater.workspace)

    def test_real_http_interruption_resumes_and_checks_complete_archive(self):
        raw, ranges = archive_bytes(), []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                requested = self.headers.get("Range")
                ranges.append(requested)
                if requested:
                    start = int(requested.removeprefix("bytes=").removesuffix("-"))
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(raw)-1}/{len(raw)}")
                    self.send_header("Content-Length", str(len(raw)-start))
                    self.end_headers()
                    self.wfile.write(raw[start:])
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw[:37])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.updater.opener = build_opener(ProxyHandler({}))
            self.updater.release = updates.release_info(self.release(raw))
            self.updater.release["url"] = f"http://127.0.0.1:{server.server_port}/package.zip"
            with patch("desktop_updates.time.sleep"):
                self.updater._download_archive(self.root / "download.zip")
            self.assertEqual(ranges, [None, "bytes=37-"])
            self.assertEqual((self.root / "download.zip").read_bytes(), raw)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_checksum_failure_leaves_original_program_untouched(self):
        state = self.download(archive_bytes(), "0" * 64)
        self.assertEqual(state["phase"], "error")
        self.assertIn("SHA-256", state["message"])
        self.assertIsNone(self.updater.workspace)
        self.assertEqual((self.root / "installed" / "ExCatalog.exe").read_bytes(), b"old-exe")

    def test_download_prepares_trusted_helper_without_modifying_installation(self):
        self.assertEqual(self.download(archive_bytes())["phase"], "ready")
        self.assertEqual((self.updater.workspace / "helper" / "ExCatalog.exe").read_bytes(), b"old-exe")
        self.assertEqual((self.updater.workspace / "payload" / "ExCatalog.exe").read_bytes(), b"new-exe")
        self.updater.state["phase"] = "installing"
        with patch("desktop_updates.subprocess.Popen") as launch:
            self.updater.launch_installer()
        self.assertEqual(launch.call_args.args[0][1],"--apply-client-update")
        self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"],"1")

    @unittest.skipUnless(os.name == "nt", "Windows update helper")
    def test_native_helper_waits_for_exited_parent_and_installs(self):
        target = self.installed_fixture()
        workspace = self.root / "excatalog-update-test"
        workspace.mkdir()
        archive = workspace / "package.zip"
        archive.write_bytes(archive_bytes())
        updates.unpack_package(archive,workspace / "payload")
        parent = subprocess.Popen([sys.executable,"-c","pass"],creationflags=subprocess.CREATE_NO_WINDOW)
        parent.wait(timeout=5)
        plan = workspace / "plan.json"
        plan.write_text(json.dumps({"parent_pid":parent.pid,"target":str(target),"data_root":str(target)}),encoding="utf-8")
        self.assertEqual(updates.apply_update(plan,relaunch=False),0)
        self.assertEqual((target / "ExCatalog.exe").read_bytes(),b"new-exe")
        self.assertEqual((target / "data" / "favorites.sqlite3").read_bytes(),b"private-records")


if __name__ == "__main__":
    unittest.main()
