from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Catalog, Server


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.path = Path(cls.directory.name) / "catalog.sqlite3"
        db = sqlite3.connect(cls.path)
        db.executescript("""
            CREATE TABLE gallery (gid INTEGER PRIMARY KEY, token TEXT, title TEXT, title_jpn TEXT, category TEXT, posted INTEGER, filecount INTEGER, rating TEXT, removed INTEGER DEFAULT 0, replaced INTEGER DEFAULT 0, expunged INTEGER DEFAULT 0);
            CREATE TABLE tag (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE gid_tid (gid INTEGER, tid INTEGER, PRIMARY KEY (gid,tid));
            CREATE INDEX local_tid_gid ON gid_tid(tid,gid);
            INSERT INTO tag VALUES (1,'language:english'),(2,'other:anthology'),(3,'language:chinese');
            INSERT INTO gallery VALUES (1,'abcdef0123','First book','第一冊','Non-H',100,10,'4.1',0,0,0),(2,'abcdef0123','Second book','第二冊','Non-H',200,20,'4.9',0,0,0),(3,'abcdef0123','Archived book','','Non-H',300,30,'5.0',0,1,0),(4,'abcdef0123','100%_ literal','','Manga',200,40,'3.5',0,0,0),(5,'abcdef0123','Removed book','','Non-H',400,50,'4.0',1,0,0);
            INSERT INTO gid_tid VALUES (1,1),(1,2),(2,1),(3,1),(4,3),(5,1);
        """)
        db.commit()
        db.close()
        translations = {
            "version": 7,
            "head": {"sha": "test-data", "committer": {"when": "2026-10-02T00:00:00Z"}},
            "data": [
                {"namespace": "rows", "data": {"language": {"name": "语言"}, "other": {"name": "其他"}}},
                {"namespace": "language", "data": {"chinese": {"name": "汉语"}, "english": {"name": "英语"}, "absent": {"name": "未在目录中"}}},
                {"namespace": "other", "data": {"anthology": {"name": "选集"}}},
            ],
        }
        cls.dictionary = cls.path.with_name("tag-translations.json")
        cls.dictionary.write_text(json.dumps(translations, ensure_ascii=False), encoding="utf-8")
        cls.catalog = Catalog(cls.path)
        cls.server = Server(("127.0.0.1", 0), cls.catalog)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def search(self, **kwargs):
        return self.catalog.search({key: value if isinstance(value, list) else [str(value)] for key, value in kwargs.items()})

    def test_tag_intersection_and_alias(self):
        result = self.search(tag=["l:english$", "other:anthology"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["gid"], 1)
        self.assertIsNone(result["items"][0]["favorite_count"])

    def test_unknown_tag_cannot_silently_broaden_query(self):
        with self.assertRaisesRegex(ValueError, "没有这个标签"):
            self.search(tag=["language:english", "no:such-tag"])

    def test_excluded_tags_and_persistent_blacklist(self):
        self.catalog.set_preferences({"tag_blacklist": []})
        try:
            temporary = self.search(tag="language:english", exclude_tag="other:anthology")
            self.assertEqual([item["gid"] for item in temporary["items"]], [2])
            self.assertEqual(temporary["exclude_tags"], ["other:anthology"])

            saved = self.catalog.set_preferences({"tag_blacklist": ["合集"]})
            self.assertEqual(saved["tag_blacklist"], ["other:anthology"])
            self.assertEqual([item["gid"] for item in self.search(tag="language:english")["items"]], [2])
            self.assertEqual(self.search(tag="language:english", use_blacklist=0)["total"], 2)

            reopened = Catalog(self.path)
            self.assertEqual(reopened.preferences()["tag_blacklist"], ["other:anthology"])
            self.assertEqual(reopened.search({"tag": ["language:english"]})["total"], 1)
            with self.assertRaisesRegex(ValueError, "同时包含和排除"):
                self.search(tag="other:anthology", exclude_tag="合集")
        finally:
            self.catalog.set_preferences({"tag_blacklist": []})

    def test_collection_plan_freezes_effective_blacklist(self):
        self.catalog.set_preferences({"tag_blacklist": ["other:anthology"]})
        try:
            canonical, where, params = self.catalog.collection_plan({"tag": ["language:english"]})
            self.assertEqual(canonical["exclude_tag"], ["other:anthology"])
            self.assertEqual(canonical["use_blacklist"], ["0"])
            self.catalog.set_preferences({"tag_blacklist": []})
            repeated, repeated_where, repeated_params = self.catalog.collection_plan(canonical)
            self.assertEqual((repeated, repeated_where, repeated_params), (canonical, where, params))
        finally:
            self.catalog.set_preferences({"tag_blacklist": []})

    def test_stable_pagination_and_exact_total(self):
        first = self.search(title="book", limit=1)
        second = self.search(title="book", limit=1, page=2)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["pages"], 2)
        self.assertEqual(first["items"][0]["gid"], 2)
        self.assertEqual(second["items"][0]["gid"], 1)
        self.assertEqual(self.search(title="book", limit=1, page=999)["page"], 2)

    def test_history_filter_and_sort(self):
        result = self.search(tag="language:english", include_inactive=1, sort="rating")
        self.assertEqual(result["total"], 4)
        self.assertEqual([row["gid"] for row in result["items"]], [3,2,1,5])
        self.assertEqual(self.search(tag="language:english", sort="oldest")["items"][0]["gid"], 1)

    def test_literals_and_injection_remain_text(self):
        self.assertEqual(self.search(title="%_")["total"], 1)
        self.assertEqual(self.search(title="%' OR 1=1 --")["total"], 0)
        with self.assertRaises(ValueError):
            self.search(title="book", sort="rating; DROP TABLE gallery")

    def test_category_and_original_title(self):
        self.assertEqual(self.search(title="第一冊", category="Non-H")["total"], 1)
        self.assertEqual(self.search(tag="language:english", category="Manga")["total"], 0)

    def test_autocomplete_and_readonly_connection(self):
        self.assertIn("language:english", self.catalog.suggest("eng")["items"])
        with self.catalog.connection() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM gallery")

    def test_chinese_search_equals_original_tag(self):
        chinese = self.search(tag=["英语", "合集"])
        english = self.search(tag=["language:english", "other:anthology"])
        self.assertEqual(chinese["total"], english["total"])
        self.assertEqual(chinese["items"], english["items"])
        self.assertEqual(chinese["tags"], ["language:english", "other:anthology"])
        self.assertEqual(chinese["tag_labels"]["language:english"]["name"], "英语")
        self.assertEqual(self.search(tag="语言：中文")["tags"], ["language:chinese"])

    def test_chinese_suggestions_only_include_catalog_tags(self):
        result = self.catalog.suggest("中文")
        self.assertEqual(result["items"], ["language:chinese"])
        self.assertEqual(result["labels"]["language:chinese"]["name"], "汉语")
        self.assertEqual(self.catalog.suggest("未在目录中")["items"], [])

    def test_ambiguous_chinese_name_requires_selection(self):
        from translations import Translations
        payload = {"data": [
            {"namespace": "artist", "data": {"example": {"name": "同名"}}},
            {"namespace": "group", "data": {"example": {"name": "同名"}}},
        ]}
        path = self.path.with_name("ambiguous.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        translator = Translations(path, {"artist:example", "group:example"})
        with self.assertRaisesRegex(ValueError, "多个标签"):
            translator.resolve("同名")
        self.assertEqual(translator.resolve("artist:同名"), "artist:example")
        self.assertEqual(translator.matches("同名"), ["artist:example", "group:example"])

    def test_absent_dictionary_preserves_english_search(self):
        catalog = Catalog(self.path, self.path.with_name("missing.json"))
        self.assertFalse(catalog.status()["translations"]["available"])
        self.assertEqual(catalog.search({"tag": ["language:english"]})["total"], 2)
        self.assertFalse(catalog.tag_labels(["language:english"])["labels"]["language:english"]["translated"])

    def test_chinese_http_json_is_utf8(self):
        with urlopen(self.url + "/api/tags?q=%E4%B8%AD%E6%96%87") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["labels"]["language:chinese"]["name"], "汉语")
        with urlopen(self.url + "/api/tag-labels?tag=language%3Aenglish") as response:
            self.assertEqual(json.load(response)["labels"]["language:english"]["namespace_name"], "语言")

    def test_preferences_route_requires_token_and_saves_blacklist(self):
        payload = json.dumps({"tag_blacklist": ["合集"]}, ensure_ascii=False).encode("utf-8")
        try:
            with self.assertRaises(HTTPError) as failure:
                urlopen(Request(self.url + "/api/preferences", data=payload, headers={"Content-Type":"application/json"}))
            self.assertEqual(failure.exception.code, 403)
            failure.exception.close()
            request = Request(self.url + "/api/preferences", data=payload, headers={
                "Content-Type":"application/json", "X-Catalog-Token":self.server.action_token, "Origin":self.url,
            })
            with urlopen(request) as response:
                self.assertEqual(json.load(response)["tag_blacklist"], ["other:anthology"])
            with urlopen(self.url + "/api/preferences") as response:
                self.assertEqual(json.load(response)["tag_blacklist"], ["other:anthology"])
        finally:
            self.catalog.set_preferences({"tag_blacklist": []})

    def test_http_routes_and_error_responses(self):
        with urlopen(self.url + "/api/status") as response:
            self.assertEqual(json.load(response)["gallery_count"], 5)
        with urlopen(self.url + "/api/search?tag=language%3Aenglish") as response:
            self.assertEqual(json.load(response)["total"], 2)
        for path, expected in (("/api/search",400),("/../app.py",404),("/api/search?"+"&".join("tag=x" for _ in range(50)),400)):
            with self.assertRaises(HTTPError) as failure:
                urlopen(self.url + path)
            self.assertEqual(failure.exception.code, expected)
            failure.exception.close()
        with self.assertRaises(HTTPError) as failure:
            urlopen(Request(self.url + "/api/shutdown", data=b"", method="POST"))
        self.assertEqual(failure.exception.code, 403)
        failure.exception.close()


if __name__ == "__main__":
    unittest.main()
