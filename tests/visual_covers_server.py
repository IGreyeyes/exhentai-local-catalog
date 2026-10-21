"""Disposable cover-recovery fixture; all image downloads are simulated."""
from contextlib import closing, redirect_stdout
from pathlib import Path
import io
import json
import sqlite3
import struct
import sys
import zlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Catalog, Handler, Server
from covers import CoverUnavailable
from initialize_catalog import initialize
from tests.test_covers import FakeOpener, FakeResponse


def image_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    pixels = b"".join(b"\0" + bytes((85 + y // 4, 130 + y // 8, 95)) * 120 for y in range(160))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 120, 160, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def main():
    root = Path(sys.argv[1]).resolve()
    data = root / "data"
    data.mkdir()
    path = data / "catalog.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,thumb TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
            CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
            CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
            INSERT INTO tag VALUES(1,'language:english');
        """)
        for gid, title in ((1, "网络恢复后的示例封面"), (2, "服务器冷却中的示例封面"), (3, "已缓存的示例封面")):
            db.execute("INSERT INTO gallery VALUES (?,'abcdef0123',?,'','Non-H',?,1760000000,10,'4.0',0,0,0)", (gid, title, f"https://ehgt.org/test-{gid}.png"))
            db.execute("INSERT INTO gid_tid VALUES (?,1)", (gid,))
        db.commit()
    with redirect_stdout(io.StringIO()): initialize(path)
    catalog = Catalog(path)
    for gid in (1, 2, 3): catalog.favorites.save(gid, gid * 10, "e-hentai.org")
    server = Server(("127.0.0.1", 0), catalog)
    calls = {}
    def download(url):
        name = url.rsplit("/", 1)[-1]
        calls[name] = calls.get(name, 0) + 1
        if name == "test-1.png" and calls[name] == 1:
            raise CoverUnavailable("封面下载失败或超时。")
        if name == "test-2.png":
            raise CoverUnavailable("封面服务器返回 HTTP 429。", 300, True)
        return image_bytes(), "image/png", "png"
    server.cover_cache.downloader = download
    probe_calls = []
    original_test = server.network.test
    def test_network(values, target, cover_url=None):
        if target != "cover": raise AssertionError("Fixture only tests cover images")
        probe_calls.append(cover_url)
        with patch("network_settings.build_opener", return_value=FakeOpener(FakeResponse(image_bytes(), "image/png"))):
            return original_test(values, target, cover_url)
    server.network.test = test_network
    class FixtureHandler(Handler):
        def do_GET(self):
            if self.path == "/test/cover-attempts": self.reply(calls)
            elif self.path == "/test/cover-probes": self.reply({"count": len(probe_calls)})
            else: super().do_GET()
    server.RequestHandlerClass = FixtureHandler
    print(json.dumps({"base": f"http://127.0.0.1:{server.server_port}"}), flush=True)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == "__main__": main()
