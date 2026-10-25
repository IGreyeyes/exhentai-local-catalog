from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
import json
import tempfile
import threading
import unittest

from app import Catalog, Server
from client_version import APP_VERSION, RELEASE_NOTES, RELEASE_TITLE, REPOSITORY
from desktop_updates import ClientUpdater
from release_history import ReleaseHistory, announcement_notes, bundled_history, upgrade_notes, version_key, version_sections
from test_desktop import make_library

NEXT_VERSION = ".".join([*APP_VERSION.split(".")[:2], str(int(APP_VERSION.split(".")[2]) + 1)])


class ReleaseHistoryTests(unittest.TestCase):
    def test_release_body_contains_only_current_and_merged_versions(self):
        body=(Path(__file__).resolve().parents[1]/"RELEASE_NOTES.md").read_text(encoding="utf-8")
        self.assertLess(len(body),20000)
        sections=version_sections(body)
        self.assertIn(APP_VERSION,sections)
        self.assertNotIn("历史更新概览",body)
        self.assertEqual(set(sections),{APP_VERSION})
        self.assertEqual(body.strip(), RELEASE_NOTES.strip())
        self.assertTrue(body.startswith(f"# v{APP_VERSION} · {RELEASE_TITLE}\n"))
        previous_search = next(item for item in bundled_history() if item['version']=='1.2.5')
        self.assertEqual(previous_search['date'], '2026-10-23T12:00:02Z')
        self.assertFalse(previous_search['prerelease'])
        self.assertIn('切换页面保留搜索', previous_search['notes'])
        previous_proxy = next(item for item in bundled_history() if item['version']=='1.2.1')
        self.assertEqual(previous_proxy['date'], '2026-10-19T13:37:59Z')
        self.assertFalse(previous_proxy['prerelease'])
        self.assertIn("独立网络代理设置",previous_proxy['notes'])
        self.assertIn("一键连接测试",previous_proxy['notes'])
        previous_layout = next(item for item in bundled_history() if item['version']=='1.2.0')
        self.assertEqual(previous_layout['date'], '2026-10-19T08:17:38Z')
        self.assertFalse(previous_layout['prerelease'])
        self.assertIn("高级筛选按需展开", previous_layout['notes'])
        recent=next(item for item in bundled_history() if item['version']=='1.1.37')
        self.assertEqual(recent['date'],'2026-10-19T07:32:56Z')
        self.assertFalse(recent['prerelease'])
        self.assertIn('作品类型支持打勾多选',recent['notes'])
        merged=next(item for item in bundled_history() if item['version']=='1.1.35')
        self.assertIsNone(merged['date'])
        self.assertEqual(merged['combined_in'],'1.1.36')
        released=next(item for item in bundled_history() if item['version']=='1.1.36')
        self.assertEqual(released['date'],'2026-10-17T17:19:29Z')
        self.assertFalse(released['prerelease'])
        self.assertIn('记住请求间隔',released['notes'])
        previous=next(item for item in bundled_history() if item["version"]=="1.1.34")
        self.assertEqual(previous["date"],"2026-10-13T17:52:59Z")
        self.assertFalse(previous["prerelease"])
        self.assertIn("验证改为随机取样",previous["notes"])
        self.assertIn("1.1.11",{item["version"] for item in bundled_history()})

    def test_announcements_exclude_history_and_keep_only_actual_merged_sections(self):
        body="介绍\n\n## v1.2.0 · 本次\n- 新功能\n## v1.1.99 · 合并于本次发布\n- 合并功能\n## v1.1.98 · 已发布旧版\n- 不应显示\n## 历史更新概览\n### v1.1.97 · 合并于本次发布\n- 历史内容"
        notes,versions=announcement_notes("1.2.0",body)
        self.assertEqual(versions,["1.2.0","1.1.99"])
        self.assertIn("合并功能",notes)
        self.assertNotIn("不应显示",notes)
        self.assertNotIn("历史内容",notes)

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

    def test_installed_notes_only_show_current_announcement_and_merged_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"data").mkdir()
            (root/"data"/"client-update-state.json").write_text(json.dumps({"acknowledged_version":"1.1.10","cloud_upload_guide_seen":True}))
            updater=ClientUpdater(root)
            self.assertNotIn("## v1.1.11",updater.info()["notes"])
            self.assertIn("## v"+APP_VERSION,updater.info()["notes"])
            self.assertNotIn("## v1.1.25",updater.info()["notes"])
            self.assertNotIn("## v1.1.26",updater.info()["notes"])
            self.assertNotIn("## v1.1.27",updater.info()["notes"])
            updater.acknowledge()
            self.assertFalse(updater.info()["show_notes"])

    def test_update_check_uses_target_announcement_without_fetching_historical_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            updater=ClientUpdater(directory)
            document={"tag_name":"v"+NEXT_VERSION,"body":"- 最新功能","assets":[{"name":"ExCatalog-Windows-x64.zip","size":4,"digest":"sha256:"+"0"*64,"browser_download_url":f"https://github.com/{REPOSITORY}/releases/download/v{NEXT_VERSION}/ExCatalog-Windows-x64.zip"}]}
            from io import BytesIO
            with patch.object(updater.opener,"open",return_value=BytesIO(json.dumps(document).encode())),patch.object(updater.history,"refresh",side_effect=ValueError("模拟离线")) as refresh:
                result=updater.check()
            self.assertTrue(result["available"])
            self.assertEqual(result["sha256"],"0"*64)
            self.assertIn("最新功能",result["notes"])
            self.assertFalse(result["history_message"])
            self.assertEqual(result["included_versions"],[NEXT_VERSION])
            refresh.assert_not_called()

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
