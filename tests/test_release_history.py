from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
import json
import tempfile
import threading
import unittest

from app import Catalog, Server
from client_version import APP_VERSION, REPOSITORY
from desktop_updates import ClientUpdater
from release_history import ReleaseHistory, bundled_history, upgrade_notes, version_key, version_sections
from test_desktop import make_library


class ReleaseHistoryTests(unittest.TestCase):
    def test_legacy_release_body_fits_old_popup_and_scopes_current_section(self):
        body=(Path(__file__).resolve().parents[1]/"RELEASE_NOTES.md").read_text(encoding="utf-8")
        self.assertLess(len(body),20000)
        sections=version_sections(body)
        self.assertIn(APP_VERSION,sections)
        self.assertIn("1.1.11",sections)
        self.assertNotIn("历史更新概览",sections[APP_VERSION])
        self.assertIn("批量上传提速",sections[APP_VERSION])
        self.assertIn("上传者昵称",sections["1.1.23"])
        self.assertIn("跨版本",sections["1.1.22"])

    def test_v22_is_combined_with_v23_without_duplicate_changes(self):
        entries = bundled_history()
        v22 = next(item for item in entries if item["version"]=="1.1.22")
        self.assertIsNone(v22["date"])
        self.assertEqual(v22["combined_in"], "1.1.23")
        notes, versions = upgrade_notes(entries, "1.1.21", "1.1.23", "")
        self.assertEqual(versions, ["1.1.23", "1.1.22"])
        self.assertEqual(notes.count("**跨版本升级汇总**"), 1)
        self.assertEqual(notes.count("**填写上传者昵称**"), 1)
    def test_cross_version_range_excludes_current_and_earlier_and_keeps_all_skipped_notes(self):
        notes, versions = upgrade_notes(bundled_history(), "1.1.10", "1.1.21", "最新说明")
        self.assertEqual(versions, [f"1.1.{number}" for number in range(21, 10, -1)])
        self.assertNotIn("## v1.1.10", notes)
        self.assertIn("## v1.1.11", notes)
        self.assertIn("## v1.1.21", notes)
        self.assertIn("免贡献码", notes)
        self.assertIn("上传确认", notes)

    def test_unpublished_combined_versions_have_no_invented_release_date(self):
        entry = next(item for item in bundled_history() if item["version"] == "1.1.14")
        self.assertIsNone(entry["date"])
        self.assertEqual(entry["combined_in"], "1.1.17")
        self.assertTrue(entry["release_url"].endswith("/v1.1.17"))

    def test_remote_pagination_drafts_prereleases_and_cumulative_body(self):
        history=ReleaseHistory()
        latest={"tag_name":"v1.2.0","name":"v1.2.0 · 新功能","body":"## v1.2.0 · 新功能\n- **新功能**：内容\n## v1.1.21 · 旧版本\n- 旧说明\n","published_at":"2026-10-11T00:00:00Z"}
        draft={"tag_name":"v9.0.0","draft":True}
        prerelease={"tag_name":"v1.3.0-beta.10","prerelease":True,"body":"预发布说明"}
        first=[latest]+[draft]*99
        responses=iter([first,[prerelease]])
        urls=[]
        def request(url):urls.append(url);return next(responses)
        result=history.refresh(request)
        self.assertEqual(len(urls),2)
        self.assertIn("page=2",urls[1])
        self.assertFalse(any(item["version"]=="9.0.0" for item in result["entries"]))
        entry=next(item for item in result["entries"] if item["version"]=="1.2.0")
        self.assertNotIn("旧说明",entry["notes"])
        notes,versions=upgrade_notes(result["entries"],APP_VERSION,"1.2.0",latest["body"])
        self.assertEqual(versions,["1.2.0"])
        self.assertNotIn("预发布说明",notes)
        self.assertGreater(version_key("1.3.0-beta.10"),version_key("1.3.0-beta.2"))

    def test_refresh_failure_preserves_bundled_records(self):
        history=ReleaseHistory();original=history.entries
        with self.assertRaises(ValueError):history.refresh(lambda url: {})
        self.assertIs(history.entries,original)
        self.assertFalse(history.lock.locked())

    def test_installed_notes_cover_previous_acknowledged_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"data").mkdir()
            (root/"data"/"client-update-state.json").write_text(json.dumps({"acknowledged_version":"1.1.10","cloud_upload_guide_seen":True}))
            updater=ClientUpdater(root)
            self.assertIn("## v1.1.11",updater.info()["notes"])
            self.assertIn("## v"+APP_VERSION,updater.info()["notes"])
            updater.acknowledge()
            self.assertFalse(updater.info()["show_notes"])

    def test_update_check_falls_back_to_history_without_blocking_verified_download(self):
        with tempfile.TemporaryDirectory() as directory:
            updater=ClientUpdater(directory)
            document={"tag_name":"v1.2.0","body":"- 最新功能","assets":[{"name":"ExCatalog-Windows-x64.zip","size":4,"digest":"sha256:"+"0"*64,"browser_download_url":f"https://github.com/{REPOSITORY}/releases/download/v1.2.0/ExCatalog-Windows-x64.zip"}]}
            from io import BytesIO
            with patch.object(updater.opener,"open",return_value=BytesIO(json.dumps(document).encode())),patch.object(updater.history,"refresh",side_effect=ValueError("模拟离线")):
                result=updater.check()
            self.assertTrue(result["available"])
            self.assertEqual(result["sha256"],"0"*64)
            self.assertIn("最新功能",result["notes"])
            self.assertTrue(result["history_message"])

    def test_local_history_page_and_protected_refresh_use_temporary_library(self):
        with tempfile.TemporaryDirectory() as directory:
            server=Server(("127.0.0.1",0),Catalog(make_library(Path(directory))))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            opener=build_opener(ProxyHandler({}));base=f"http://127.0.0.1:{server.server_port}"
            try:
                with opener.open(base+"/changelog") as response:self.assertIn("更新日志".encode(),response.read())
                with opener.open(base+"/api/release-history") as response:self.assertEqual(json.load(response)["current_version"],APP_VERSION)
                def refresh(authorized):
                    return opener.open(Request(base+"/api/release-history/refresh",data=b"{}",headers={"Content-Type":"application/json","Origin":base,"X-Catalog-Token":server.action_token if authorized else ""}))
                with self.assertRaises(HTTPError) as failure:refresh(False)
                self.assertEqual(failure.exception.code,403);failure.exception.close()
                with patch.object(server.release_history,"_request",return_value=[]):
                    with refresh(True) as response:self.assertTrue(json.load(response)["refreshed"])
            finally:server.shutdown();server.server_close();thread.join(5)


if __name__ == "__main__":unittest.main()
