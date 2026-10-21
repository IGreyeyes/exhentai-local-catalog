from contextlib import closing
from pathlib import Path
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import sqlite3
import json
import tempfile
import threading
import time
import unittest

from app import Catalog, Server
from client_version import APP_VERSION
from covers import CoverCache, CoverUnavailable, MAX_IMAGE_BYTES, detect_image, download_public_cover, normalize_cover_url

JPEG = b"\xff\xd8\xff\xe0" + b"test-jpeg-data" + b"\xff\xd9"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"test"


class FakeResponse:
    def __init__(self, data, content_type="image/jpeg", content_length=None):
        self.data = data
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size=-1): return self.data if size < 0 else self.data[:size]


class FakeOpener:
    def __init__(self, response): self.response, self.requests = response, []
    def open(self, request, timeout=None): self.requests.append((request, timeout)); return self.response


class CoverUnitTests(unittest.TestCase):
    def test_partial_response_and_server_retry_after(self):
        for error, delay, blocked in (
            (IncompleteRead(b"partial"), 60, False),
            (HTTPError("https://ehgt.org/test.jpg", 429, "limited", {"Retry-After": "600"}, None), 600, True),
            (HTTPError("https://ehgt.org/test.jpg", 429, "limited", {}, None), 300, True),
            (HTTPError("https://ehgt.org/test.jpg", 503, "busy", {"Retry-After": "invalid"}, None), 300, True),
            (HTTPError("https://ehgt.org/test.jpg", 404, "missing", {}, None), 3600, False),
        ):
            with self.subTest(error=type(error).__name__, delay=delay):
                opener = FakeOpener(None)
                with patch.object(opener, "open", side_effect=error), self.assertRaises(CoverUnavailable) as raised:
                    download_public_cover("https://ehgt.org/test.jpg", opener)
                self.assertEqual(raised.exception.retry_after, delay)
                self.assertEqual(raised.exception.retry_blocked, blocked)

    def test_hosts_are_rewritten_to_public_thumbnail_host(self):
        path="/t/aa/bb/example-image.jpg"
        for host in ("ehgt.org","s.exhentai.org","exhentai.org"):
            self.assertEqual(normalize_cover_url(f"https://{host}{path}?private=value"),f"https://ehgt.org{path}")
        for value in ("http://ehgt.org/x.jpg","https://example.com/x.jpg","https://user:pass@ehgt.org/x.jpg","https://ehgt.org:444/x.jpg","",None):
            with self.assertRaises(CoverUnavailable): normalize_cover_url(value)

    def test_download_request_never_contains_credentials(self):
        opener=FakeOpener(FakeResponse(JPEG))
        data,content_type,extension=download_public_cover("https://ehgt.org/a.jpg",opener)
        request,timeout=opener.requests[0]
        headers={key.lower():value for key,value in request.header_items()}
        self.assertEqual((data,content_type,extension),(JPEG,"image/jpeg","jpg"))
        self.assertNotIn("cookie",headers)
        self.assertNotIn("authorization",headers)
        self.assertEqual(timeout,25)

    def test_content_type_size_and_magic_are_validated(self):
        for response in (FakeResponse(b"<html>blocked</html>","text/html"),FakeResponse(JPEG,content_length=MAX_IMAGE_BYTES+1),FakeResponse(b"not-an-image","image/jpeg")):
            with self.assertRaises(CoverUnavailable):download_public_cover("https://ehgt.org/a.jpg",FakeOpener(response))
        self.assertEqual(detect_image(WEBP),("image/webp","webp"))


class CoverCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.calls=[]
        def downloader(url):self.calls.append(url);return JPEG,"image/jpeg","jpg"
        self.cache=CoverCache(self.root/"covers",self.root/"covers.sqlite3",downloader=downloader,interval=0,concurrency=3)
        self.source="https://s.exhentai.org/t/aa/bb/cover.jpg"
    def tearDown(self):self.directory.cleanup()

    def test_first_request_downloads_and_second_reuses_file(self):
        first=self.cache.get(self.source);second=self.cache.get(self.source)
        self.assertEqual(first,second)
        self.assertEqual(self.calls,["https://ehgt.org/t/aa/bb/cover.jpg"])
        self.assertEqual(first[0].read_bytes(),JPEG)
        self.assertTrue(first[0].is_relative_to((self.root/"covers").resolve()))
        self.assertEqual(self.cache.stats()["cached"],1)
        self.assertEqual(self.cache.stats()["size_bytes"],len(JPEG))
        self.assertFalse(self.cache.stats()["uses_credentials"])

    def test_parallel_same_url_is_downloaded_once(self):
        barrier=threading.Barrier(5)
        results=[]
        def worker():barrier.wait();results.append(self.cache.get(self.source)[0])
        threads=[threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(set(results)),1)
        self.assertEqual(len(self.calls),1)

    def test_failure_is_cached_then_retried_after_ttl(self):
        attempts=[]
        def fail(url):attempts.append(url);raise CoverUnavailable("test failure")
        cache=CoverCache(self.root/"failed",self.root/"failed.sqlite3",downloader=fail,interval=0)
        for _ in range(2):
            with self.assertRaisesRegex(CoverUnavailable,"test failure"):cache.get(self.source)
        self.assertEqual(len(attempts),1)
        with cache.connection() as db:db.execute("UPDATE covers SET checked_at=1")
        with self.assertRaises(CoverUnavailable):cache.get(self.source)
        self.assertEqual(len(attempts),2)
        self.assertEqual(cache.stats()["failed"],1)

    def test_transient_failure_recovers_after_one_minute(self):
        success = self.cache.downloader
        with patch("covers.time.time", return_value=1000), patch.object(self.cache, "downloader", side_effect=CoverUnavailable("timeout")):
            with self.assertRaises(CoverUnavailable): self.cache.get(self.source)
        with patch("covers.time.time", return_value=1059), patch.object(self.cache, "downloader", wraps=success) as downloader:
            with self.assertRaises(CoverUnavailable): self.cache.get(self.source)
            downloader.assert_not_called()
        with patch("covers.time.time", return_value=1060):
            self.assertEqual(self.cache.get(self.source)[0].read_bytes(), JPEG)

    def test_manual_retry_preserves_success_and_server_waits(self):
        saved = self.cache.get(self.source)[0]
        with patch.object(self.cache, "downloader", side_effect=CoverUnavailable("timeout")):
            with self.assertRaises(CoverUnavailable): self.cache.get("https://ehgt.org/failed.jpg")
        with patch.object(self.cache, "downloader", side_effect=CoverUnavailable("limited", 600, True)):
            with self.assertRaises(CoverUnavailable): self.cache.get("https://ehgt.org/limited.jpg")
        self.assertEqual(self.cache.retry_failed(), {"cleared": 1, "waiting": 1})
        self.assertEqual(saved.read_bytes(), JPEG)
        with self.assertRaisesRegex(ValueError, "5 秒"): self.cache.retry_failed()
        # Saving proxy settings must also preserve the server-requested cooldown.
        self.assertEqual(self.cache.retry_failed(throttle=False), {"cleared": 0, "waiting": 1})
        with self.assertRaisesRegex(CoverUnavailable, "limited"): self.cache.get("https://ehgt.org/limited.jpg")
        self.cache.get("https://ehgt.org/failed.jpg")
        self.assertEqual(self.cache.stats()["cached"], 2)
        with self.cache.connection() as db: db.execute("UPDATE covers SET checked_at=1 WHERE status='failed'")
        self.assertEqual(self.cache.retry_failed(throttle=False), {"cleared": 1, "waiting": 0})

    def test_legacy_cache_migrates_without_discarding_images(self):
        path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("CREATE TABLE covers(source_hash TEXT PRIMARY KEY,source_url TEXT NOT NULL,relative_path TEXT,content_type TEXT,size_bytes INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,checked_at INTEGER NOT NULL,error TEXT NOT NULL DEFAULT '');")
            url = normalize_cover_url(self.source)
            db.execute("INSERT INTO covers(source_hash,source_url,status,checked_at,error) VALUES (?,?,'failed',?,'old timeout')", (CoverCache.key(url), url, int(time.time()) - 61))
            db.commit()
        cache = CoverCache(self.root / "legacy", path, downloader=self.cache.downloader, interval=0)
        self.assertEqual(cache.get(self.source)[0].read_bytes(), JPEG)
        restored = CoverCache(self.root / "legacy", path, downloader=lambda url: self.fail("Successful cache must be retained"))
        self.assertEqual(restored.get(self.source)[0].read_bytes(), JPEG)

    def test_retry_during_download_does_not_restore_an_old_failure(self):
        started, finish = threading.Event(), threading.Event()
        def download(url):
            started.set()
            if not finish.wait(3): raise AssertionError("Test download was not released")
            raise CoverUnavailable("old request failed")
        self.cache.downloader = download
        errors = []
        def worker():
            try: self.cache.get(self.source)
            except CoverUnavailable as error: errors.append(str(error))
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.cache.retry_failed()
        finally:
            finish.set(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["old request failed"])
        self.assertEqual(self.cache.stats()["failed"], 0)


class CoverRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name);self.path=self.root/"catalog.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,thumb TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO gallery VALUES(1,'abcdef0123','Cover test','','Non-H','https://exhentai.org/t/a/b/c.jpg',1,1,'4.0',0,0,0);
            """);db.commit()
        self.catalog=Catalog(self.path);self.server=Server(('127.0.0.1',0),self.catalog)
        calls=[]
        def download(url):calls.append(url);return JPEG,"image/jpeg","jpg"
        self.calls=calls;self.server.cover_cache=CoverCache(self.root/"http-covers",self.root/"http-covers.sqlite3",downloader=download,interval=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url=f"http://127.0.0.1:{self.server.server_port}"
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.directory.cleanup()

    def test_search_returns_only_local_cover_route(self):
        with urlopen(self.url+"/api/search?title=Cover") as response:
            payload=response.read().decode("utf-8")
        self.assertIn(f'"cover_path": "/api/cover/1?v={APP_VERSION}"',payload)
        self.assertNotIn("ehgt.org",payload)
        self.assertNotIn("exhentai.org/t/",payload)

    def test_route_serves_cache_and_unknown_gid_gets_placeholder(self):
        with urlopen(self.url+"/api/cover/1") as response:
            self.assertEqual(response.headers.get_content_type(),"image/jpeg");self.assertEqual(response.read(),JPEG)
        with urlopen(self.url+"/api/cover/1") as response:self.assertEqual(response.read(),JPEG)
        self.assertEqual(self.calls,["https://ehgt.org/t/a/b/c.jpg"])
        with urlopen(self.url+"/api/cover/999") as response:
            self.assertEqual(response.headers.get_content_type(),"image/svg+xml");self.assertIn(b"<svg",response.read().lower())

    def test_malformed_cover_route_is_not_a_remote_fetch(self):
        with self.assertRaises(HTTPError) as failure:urlopen(self.url+"/api/cover/not-a-number")
        self.assertEqual(failure.exception.code,404);failure.exception.close();self.assertEqual(self.calls,[])

    def test_retry_api_recovers_placeholder_without_browser_cache_or_collecting(self):
        def post(payload, authorized=True):
            headers = {"Content-Type": "application/json", "X-Catalog-Token": self.server.action_token if authorized else ""}
            return urlopen(Request(self.url + "/api/covers/retry", data=json.dumps(payload).encode(), headers=headers))
        with patch.object(self.server.cover_cache, "downloader", side_effect=CoverUnavailable("timeout")):
            with urlopen(self.url + "/api/cover/1") as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
        for payload, authorized, code in (({}, False, 403), ({"source": "https://example.invalid/"}, True, 400)):
            with self.assertRaises(HTTPError) as raised: post(payload, authorized)
            self.assertEqual(raised.exception.code, code); raised.exception.close()
        with post({}) as response: self.assertEqual(json.load(response), {"cleared": 1, "waiting": 0})
        self.assertEqual(self.calls, [])  # The action only makes visible covers eligible again.
        with urlopen(self.url + f"/api/cover/1?v={APP_VERSION}&retry=1") as response:
            self.assertEqual(response.read(), JPEG)
            self.assertEqual(response.headers["Cache-Control"], "private, max-age=86400")
        with self.server.catalog.favorites.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM favorites").fetchone()[0], 0)


if __name__=="__main__":unittest.main()
