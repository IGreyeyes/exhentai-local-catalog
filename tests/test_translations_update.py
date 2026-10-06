from contextlib import closing, redirect_stdout
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from unittest.mock import patch
import gzip
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from app import Catalog, Server
from maintenance import LibraryMaintenance
from update_translations import RELEASE_URL, check_latest, compare_version, local_status, update


def dictionary(revision, name):
    return {"version": 7, "head": {"sha": revision, "committer": {"when": "2026-10-06T10:00:00Z"}}, "data": [{"namespace": "language", "data": {"english": {"name": name}}}]}


class TranslationUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.destination = self.data / "tag-translations.json"
        self.old = json.dumps(dictionary("a" * 40, "旧译名"), ensure_ascii=False).encode("utf-8")
        self.new = json.dumps(dictionary("b" * 40, "新译名"), ensure_ascii=False).encode("utf-8")
        self.compressed = gzip.compress(self.new)
        self.download_url = "https://github.com/EhTagTranslation/Database/releases/download/test-release/db.text.json.gz"
        self.release = {"tag_name": "test-release", "target_commitish": "b" * 40, "published_at": "2026-10-06T10:00:00Z", "assets": [{"name": "db.text.json.gz", "browser_download_url": self.download_url, "digest": "sha256:" + hashlib.sha256(self.compressed).hexdigest()}]}
        self.destination.write_bytes(self.old)
        self.metadata = self.data / "tag-translations-info.json"
        self.old_metadata = json.dumps({"revision": "a" * 40, "archive_sha256": hashlib.sha256(gzip.compress(self.old)).hexdigest(), "dictionary_sha256": hashlib.sha256(self.old).hexdigest()}).encode()
        self.metadata.write_bytes(self.old_metadata)
        self.requests = []

    def tearDown(self):
        self.temporary.cleanup()

    def remote(self, request, **kwargs):
        self.requests.append(request)
        self.assertNotIn("Cookie", dict(request.header_items()))
        self.assertNotIn("Authorization", dict(request.header_items()))
        if request.full_url == RELEASE_URL:
            return io.BytesIO(json.dumps(self.release).encode())
        self.assertEqual(request.full_url, self.download_url)
        return io.BytesIO(self.compressed)

    def install(self, **options):
        return update(self.download_url, hashlib.sha256(self.compressed).hexdigest(), self.destination, progress=lambda text: None, **options)

    def catalog(self):
        path = self.data / "catalog.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0,thumb TEXT);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER);
                INSERT INTO gallery VALUES(1,'abcdef0123','Test record','','Non-H',1,10,'4.0',0,0,0,NULL);
                INSERT INTO tag VALUES(1,'language:english');
                INSERT INTO gid_tid VALUES(1,1);
            """)
            db.commit()
        return Catalog(path)

    def test_check_fetches_only_metadata_and_does_not_install(self):
        with patch("update_translations.urlopen", side_effect=self.remote):
            result = check_latest(self.destination)
        self.assertEqual(result["state"], "update_available")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertEqual(self.metadata.read_bytes(), self.old_metadata)

    def test_installed_release_is_detected_by_checksum_without_redownloading(self):
        with patch("update_translations.urlopen", side_effect=self.remote):
            self.install(release={"tag": "test-release"})
            self.requests.clear()
            self.assertEqual(check_latest(self.destination)["state"], "up_to_date")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(local_status(self.destination)["release_tag"], "test-release")
        self.assertEqual(self.destination.with_suffix(".previous.json").read_bytes(), self.old)

    def test_missing_invalid_and_unrecorded_dictionaries_are_distinct(self):
        self.destination.unlink()
        with patch("update_translations.urlopen", side_effect=self.remote):
            self.assertEqual(check_latest(self.destination)["state"], "not_installed")
            self.destination.write_text('{"data":[]}', encoding="utf-8")
            self.assertEqual(check_latest(self.destination)["state"], "not_installed")
            self.destination.write_bytes(json.dumps({"data": dictionary("", "英语")["data"]}).encode())
            self.metadata.unlink()
            self.assertEqual(check_latest(self.destination)["state"], "unknown")

    def test_changed_file_cannot_reuse_stale_archive_checksum(self):
        self.destination.write_bytes(self.new)
        with patch("update_translations.urlopen", side_effect=self.remote):
            self.assertEqual(check_latest(self.destination)["state"], "unknown")
        self.assertEqual(local_status(self.destination)["archive_sha256"], "")

    def test_old_metadata_can_compare_an_exact_release_revision(self):
        self.assertEqual(compare_version({"available": True, "revision": "b" * 40}, {"revision": "b" * 40}), "up_to_date")
        self.assertEqual(compare_version({"available": True, "revision": "a" * 40}, {"revision": "master"}), "unknown")

    def test_network_checksum_and_invalid_payload_failures_keep_originals(self):
        cases = [URLError("simulated offline"), io.BytesIO(b"invalid gzip"), io.BytesIO(gzip.compress(b'{"data":[]}'))]
        for response in cases:
            with self.subTest(response=type(response).__name__):
                with patch("update_translations.urlopen", side_effect=response if isinstance(response, Exception) else lambda *args, **kwargs: response):
                    with self.assertRaises(Exception):
                        update(self.download_url, destination=self.destination, progress=lambda text: None)
                self.assertEqual(self.destination.read_bytes(), self.old)
                self.assertEqual(self.metadata.read_bytes(), self.old_metadata)
        with patch("update_translations.urlopen", side_effect=self.remote):
            with self.assertRaisesRegex(ValueError, "校验失败"):
                update(self.download_url, "0" * 64, self.destination, progress=lambda text: None)
        self.assertEqual(self.destination.read_bytes(), self.old)

    def test_size_limit_applies_before_installation(self):
        with patch("update_translations.urlopen", side_effect=self.remote), patch("update_translations.MAX_JSON_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "大小限制"):
                self.install()
        self.assertEqual(self.destination.read_bytes(), self.old)

    def test_failure_writing_metadata_rolls_back_dictionary_and_preserves_previous(self):
        previous = self.destination.with_suffix(".previous.json")
        previous.write_bytes(b"previous version")
        replace = os.replace
        def failing_replace(source, target):
            if Path(source).name == "new-1":
                raise OSError("simulated metadata write failure")
            return replace(source, target)
        with patch("update_translations.urlopen", side_effect=self.remote), patch("update_translations.os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertEqual(self.metadata.read_bytes(), self.old_metadata)
        self.assertEqual(previous.read_bytes(), b"previous version")

    def test_reload_failure_restores_both_files_before_reloading_old_version(self):
        seen = []
        def reload():
            seen.append(self.destination.read_bytes())
            if seen[-1] == self.new:
                raise ValueError("simulated reload failure")
        with patch("update_translations.urlopen", side_effect=self.remote):
            with self.assertRaisesRegex(ValueError, "reload failure"):
                self.install(on_installed=reload)
        self.assertEqual(seen, [self.new, self.old])
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertEqual(self.metadata.read_bytes(), self.old_metadata)

    def test_untrusted_release_asset_url_is_rejected(self):
        self.release["assets"][0]["browser_download_url"] = "https://example.invalid/db.text.json.gz"
        with patch("update_translations.urlopen", side_effect=self.remote):
            with self.assertRaisesRegex(ValueError, "下载地址"):
                check_latest(self.destination)
        self.assertEqual(len(self.requests), 1)

    def test_check_state_invalidates_after_restoring_another_dictionary(self):
        catalog = self.catalog()
        engine = LibraryMaintenance(catalog.path, self.root)
        with patch("update_translations.urlopen", side_effect=self.remote):
            engine.check_translations()
        self.assertEqual(engine.translation_status()["state"], "update_available")
        self.destination.write_bytes(self.new)
        self.assertEqual(engine.translation_status()["state"], "unchecked")

    def test_successful_install_is_not_reported_failed_when_optional_check_record_cannot_be_saved(self):
        catalog = self.catalog()
        engine = LibraryMaintenance(catalog.path, self.root)
        with patch("update_translations.urlopen", side_effect=self.remote), patch("maintenance.save_json", side_effect=OSError("simulated full disk")):
            result = engine.update_translations(lambda: None)
        self.assertTrue(result["changed"])
        self.assertIn("更新成功", result["message"])
        self.assertIn("检查记录未能保存", result["message"])
        self.assertEqual(self.destination.read_bytes(), self.new)
        self.assertEqual(engine.translation_status()["state"], "up_to_date")

    def test_web_update_changes_search_labels_without_resetting_collector_or_records(self):
        catalog = self.catalog()
        catalog.favorites.save(1, 37, "e-hentai.org")
        catalog.favorites.set_reading_state([1], "watched")
        server = Server(("127.0.0.1", 0), catalog)
        server.collector.configure({"host": "e-hentai.org", "interval": 3})
        server.collector.verified = True
        settings = dict(server.collector.settings)
        with catalog.favorites.connection() as db:
            db.execute("INSERT INTO jobs(id,query_json,host,state,total,created_at,updated_at) VALUES(7,'{}','e-hentai.org','paused',1,1,1)")
            db.execute("INSERT INTO tasks(job_id,gid,token) VALUES(7,1,'abcdef0123')")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def post(name, payload=None, authorized=True):
            return urlopen(Request(base + "/api/maintenance/" + name, data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json", "X-Catalog-Token": server.action_token if authorized else "", "Origin": base}), timeout=10)
        try:
            with self.assertRaises(HTTPError) as failure:
                post("translations-update", authorized=False)
            self.assertEqual(failure.exception.code, 403)
            failure.exception.close()
            with self.assertRaises(HTTPError) as failure:
                post("translations-update", {"url": "https://example.invalid/"})
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()
            with patch("update_translations.urlopen", side_effect=self.remote):
                with post("translations-check") as response:
                    self.assertIn("requested_id", json.load(response))
                server.maintenance.thread.join(timeout=5)
                self.assertEqual(server.maintenance.status()["translations"]["state"], "update_available")
                with post("translations-update") as response:
                    json.load(response)
                server.maintenance.thread.join(timeout=5)
                self.assertEqual(server.maintenance.status()["phase"], "completed")
                self.assertEqual(server.maintenance.status()["translations"]["state"], "up_to_date")
                self.requests.clear()
                with post("translations-update") as response:
                    json.load(response)
                server.maintenance.thread.join(timeout=5)
                self.assertFalse(server.maintenance.status()["result"]["changed"])
                self.assertEqual(len(self.requests), 1)
            with urlopen(base + "/api/tags?q=" + quote("新译名")) as response:
                self.assertEqual(json.load(response)["items"], ["language:english"])
            self.assertEqual(server.collector.settings, settings)
            self.assertTrue(server.collector.verified)
            with catalog.favorites.connection() as db:
                self.assertEqual(db.execute("SELECT favorite_count FROM favorites WHERE gid=1").fetchone()[0], 37)
                self.assertEqual(db.execute("SELECT state FROM record_status WHERE gid=1").fetchone()[0], "watched")
                self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=7").fetchone()[0], "paused")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE job_id=7").fetchone()[0], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_failed_check_reports_error_and_preserves_last_good_dictionary(self):
        catalog = self.catalog()
        server = Server(("127.0.0.1", 0), catalog)
        try:
            with patch("update_translations.urlopen", side_effect=URLError("simulated offline")):
                server.maintenance.start("translations-check")
                server.maintenance.thread.join(timeout=5)
            status = server.maintenance.status()
            self.assertEqual(status["phase"], "failed")
            self.assertIn("无法连接", status["message"])
            self.assertTrue(status["translations"]["local"]["available"])
            self.assertEqual(server.catalog.translations.resolve("旧译名"), "language:english")
            self.assertEqual(self.destination.read_bytes(), self.old)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
