from compression import zstd
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import sqlite3
import tempfile
import unittest

from first_run import catalog_prepared, preparation_lock, prepare_library
from initialize_catalog import initialize


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.database = self.data / "catalog.sqlite3"
        self.messages = []

    def tearDown(self):
        self.temporary.cleanup()

    def make_database(self, path):
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER);
                INSERT INTO gallery VALUES(1,'abcdef0123','Test record','','Non-H',1,10,'4.0',0,0,0);
                INSERT INTO tag VALUES(1,'language:english');
                INSERT INTO gid_tid VALUES(1,1);
            """)
            db.commit()

    def prepare(self):
        with redirect_stdout(io.StringIO()):
            prepare_library(self.root, self.messages.append)

    def test_first_run_extracts_initializes_and_prepares_dictionary(self):
        source = self.root / "source.sqlite3"
        self.make_database(source)
        (self.root / "e-hentai.db.zstd").write_bytes(zstd.compress(source.read_bytes()))
        def translations(**kwargs):
            kwargs["destination"].write_text('{"data":[]}', encoding="utf-8")
        with patch("first_run.update", side_effect=translations) as download:
            self.prepare()
        self.assertTrue(catalog_prepared(self.database))
        self.assertEqual(download.call_count, 1)
        self.assertTrue((self.data / "tag-translations.json").is_file())

    def test_existing_ready_installation_does_not_reindex_redownload_or_replace_records(self):
        self.make_database(self.database)
        with redirect_stdout(io.StringIO()):
            initialize(self.database)
        (self.data / "tag-translations.json").write_text("existing dictionary", encoding="utf-8")
        favorites = self.data / "favorites.sqlite3"
        favorites.write_bytes(b"private data sentinel")
        before = self.database.read_bytes()
        with patch("first_run.initialize") as index, patch("first_run.update") as download:
            self.prepare()
        index.assert_not_called()
        download.assert_not_called()
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(favorites.read_bytes(), b"private data sentinel")

    def test_incomplete_index_preparation_can_be_resumed(self):
        self.make_database(self.database)
        (self.data / "tag-translations.json").write_text("existing dictionary", encoding="utf-8")
        self.assertFalse(catalog_prepared(self.database))
        self.prepare()
        self.assertTrue(catalog_prepared(self.database))

    def test_dictionary_network_failure_does_not_block_ready_catalog(self):
        self.make_database(self.database)
        with patch("first_run.update", side_effect=ValueError("simulated offline")):
            self.prepare()
        self.assertTrue(catalog_prepared(self.database))
        self.assertTrue(any("继续启动英文标签搜索" in message for message in self.messages))

    def test_missing_archive_and_partial_extraction_give_actionable_errors(self):
        with self.assertRaisesRegex(ValueError, "请先下载"):
            self.prepare()
        (self.root / "e-hentai.db.zstd").write_bytes(b"unused")
        partial = self.database.with_suffix(".sqlite3.partial")
        partial.write_bytes(b"incomplete")
        with self.assertRaisesRegex(ValueError, "上次解压未完成"):
            self.prepare()
        self.assertEqual(partial.read_bytes(), b"incomplete")
        self.assertFalse(self.database.exists())

    def test_existing_recovery_journal_is_left_for_server_recovery(self):
        journal = self.data / "catalog-swap.json"
        journal.write_text("recovery sentinel", encoding="utf-8")
        with patch("first_run.prepare") as extract, patch("first_run.initialize") as index:
            self.prepare()
        extract.assert_not_called()
        index.assert_not_called()
        self.assertEqual(journal.read_text(encoding="utf-8"), "recovery sentinel")

    def test_second_preparation_is_blocked_and_lock_is_reusable(self):
        with preparation_lock(self.data):
            with self.assertRaisesRegex(ValueError, "正在运行"):
                with preparation_lock(self.data):
                    pass
        with preparation_lock(self.data):
            pass


if __name__ == "__main__":
    unittest.main()
