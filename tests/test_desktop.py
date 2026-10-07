from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from app import Catalog
from desktop import DesktopApi
from desktop_service import DesktopSession
from initialize_catalog import initialize
from maintenance import LibraryMaintenance
from runtime_paths import library_lock, library_root, resource_root


def make_library(root):
    data = Path(root) / "data"
    data.mkdir()
    database = data / "catalog.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.executescript("""
            CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
            CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
            CREATE TABLE gid_tid(gid INTEGER,tid INTEGER);
            INSERT INTO gallery VALUES(1,'abcdef0123','Desktop test','','Non-H',1,10,'4.0',0,0,0);
            INSERT INTO tag VALUES(1,'language:english');
            INSERT INTO gid_tid VALUES(1,1);
        """)
    with redirect_stdout(io.StringIO()):
        initialize(database)
    (data / "tag-translations.json").write_text('{"data":[]}', encoding="utf-8")
    catalog = Catalog(database)
    catalog.favorites.save(1, 123, "exhentai.org")
    catalog.favorites.set_reading_state([1], "watched")
    return database


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = make_library(self.root)
        self.session = DesktopSession(self.root)

    def tearDown(self):
        self.session.close(wait_for_maintenance=True)
        self.directory.cleanup()

    def start(self):
        return self.session.start(lambda message: None)

    def test_start_search_and_exit_preserve_catalog_and_personal_records(self):
        before = hashlib.sha256(self.database.read_bytes()).digest()
        url = self.start()
        with urlopen(url + "/api/search?tag=language%3Aenglish") as response:
            result = json.load(response)
        self.assertEqual(result["total"], 1)
        with urlopen(url + "/platform.js") as response:
            self.assertIn(b"catalogSaveAttachment", response.read())
        self.session.close()
        self.assertFalse(self.session.thread.is_alive())
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before)
        with closing(sqlite3.connect(self.root / "data" / "favorites.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT favorite_count FROM favorites WHERE gid=1").fetchone()[0], 123)
            self.assertEqual(db.execute("SELECT state FROM record_status WHERE gid=1").fetchone()[0], "watched")

    def test_second_service_cannot_initialize_the_same_library(self):
        self.start()
        other = DesktopSession(self.root)
        with self.assertRaisesRegex(ValueError, "已有服务"):
            other.start()
        self.session.close()
        with library_lock(self.root):
            pass

    def test_browser_launcher_cannot_prepare_an_active_desktop_library(self):
        import launch
        self.start()
        with patch("launch.ROOT", self.root), patch("launch.running", return_value=False), patch("launch.prepare_library") as prepare, patch("sys.argv", ["launch.py", "--no-browser"]):
            with self.assertRaisesRegex(ValueError, "已有服务"):
                launch.main()
        prepare.assert_not_called()

    def test_close_refuses_active_maintenance_and_waits_for_collector(self):
        self.start()
        self.session.server.maintenance.state["busy"] = True
        with self.assertRaisesRegex(ValueError, "维护任务"):
            self.session.close()
        self.assertTrue(self.session.thread.is_alive())
        self.session.server.maintenance.state["busy"] = False
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(10))
        self.session.server.collector.thread = worker
        worker.start()
        entered = threading.Event()
        def close():
            entered.set()
            self.session.close()
        closer = threading.Thread(target=close)
        closer.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(worker.is_alive())
        release.set()
        closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertFalse(worker.is_alive())

    def test_native_export_cancel_and_save_and_protected_paths(self):
        self.start()
        api = DesktopApi(self.session)
        chosen = [None]
        api._window = SimpleNamespace(get_current_url=lambda: self.session.base_url, create_file_dialog=lambda *args, **kwargs: chosen[0])
        fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(SAVE=30))
        with patch.dict("sys.modules", {"webview": fake_webview}):
            self.assertTrue(api.save_attachment("/api/maintenance/favorites-export")["cancelled"])
            chosen[0] = [str(self.root / "export.json")]
            result = api.save_attachment("/api/maintenance/favorites-export")
            self.assertFalse(result["cancelled"])
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(json.loads((self.root / "export.json").read_text())["records"][0]["favorite_count"], 123)
            chosen[0] = [str(self.root / "data" / "catalog_info.json")]
            before = (self.root / "data" / "catalog_info.json").read_bytes()
            self.assertIn("error", api.save_attachment("/api/maintenance/favorites-export"))
            self.assertEqual((self.root / "data" / "catalog_info.json").read_bytes(), before)
            chosen[0] = [str(self.database)]
            self.assertIn("error", api.save_attachment("/api/maintenance/favorites-export"))
            runtime = self.root / "program" / "_internal"
            runtime.mkdir(parents=True)
            protected = runtime / "settings.json"
            protected.write_bytes(b"runtime sentinel")
            chosen[0] = [str(protected)]
            with patch("sys.frozen", True, create=True), patch("sys._MEIPASS", str(runtime), create=True):
                self.assertIn("error", api.save_attachment("/api/maintenance/favorites-export"))
            self.assertEqual(protected.read_bytes(), b"runtime sentinel")
        self.assertIn("error", api.save_attachment("https://example.com"))

    def test_packaged_backups_include_exe_and_runtime_without_changing_database(self):
        exe = self.root / "ExCatalog.exe"
        exe.write_bytes(b"test-executable")
        runtime = self.root / "_internal"
        runtime.mkdir()
        (runtime / "python314.dll").write_bytes(b"test-runtime")
        engine = LibraryMaintenance(self.database, self.root)
        with patch("sys.frozen", True, create=True), patch("sys.executable", str(exe)):
            backup = Path(engine.backup()["path"])
        self.assertEqual((backup / "ExCatalog.exe").read_bytes(), b"test-executable")
        self.assertEqual((backup / "_internal" / "python314.dll").read_bytes(), b"test-runtime")
        self.assertTrue((backup / "data" / "favorites.sqlite3").is_file())

    def test_frozen_resource_and_data_paths_remain_separate(self):
        resources = self.root / "_internal"
        with patch.dict(os.environ, {"EH_CATALOG_DATA_ROOT": str(self.root)}), patch("sys.frozen", True, create=True), patch("sys._MEIPASS", str(resources), create=True):
            self.assertEqual(library_root(), self.root)
            self.assertEqual(resource_root(), resources)


if __name__ == "__main__":
    unittest.main()
