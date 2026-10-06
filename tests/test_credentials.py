from contextlib import closing
from pathlib import Path
from urllib.request import Request, urlopen
import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from app import Catalog, Server
from credentials import export_credentials, import_credentials


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class CredentialFileTests(unittest.TestCase):
    def test_dpapi_roundtrip_does_not_leave_values_in_file(self):
        settings = {
            "host": "exhentai.org", "proxy": "http://127.0.0.1:7890",
            "cookies": "ipb_member_id=12345; ipb_pass_hash=test-hash-value; igneous=test-igneous-value",
        }
        raw = export_credentials(settings)
        self.assertNotIn(b"test-hash-value", raw)
        self.assertEqual(import_credentials(raw), {
            "host": "exhentai.org", "proxy": "http://127.0.0.1:7890",
            "ipb_member_id": "12345", "ipb_pass_hash": "test-hash-value", "igneous": "test-igneous-value",
        })
        damaged = bytearray(raw)
        damaged[-10] = ord("A") if damaged[-10] != ord("A") else ord("B")
        with self.assertRaises(ValueError):
            import_credentials(bytes(damaged))

    def test_export_and_import_routes_restore_in_memory_settings(self):
        directory = tempfile.TemporaryDirectory()
        try:
            path = Path(directory.name) / "catalog.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                    CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                    CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                """)
            server = Server(("127.0.0.1", 0), Catalog(path))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            root = f"http://127.0.0.1:{server.server_port}"
            headers = {"Content-Type":"application/json", "X-Catalog-Token":server.action_token, "Origin":root}
            def post(route, payload):
                request = Request(root + route, data=json.dumps(payload).encode(), headers=headers)
                return urlopen(request)
            try:
                with post("/api/collector/settings", {
                    "host":"exhentai.org", "ipb_member_id":"12345",
                    "ipb_pass_hash":"test-hash-value", "igneous":"test-igneous-value",
                }) as response:
                    self.assertTrue(json.load(response)["has_credentials"])
                with post("/api/credentials/export", {}) as response:
                    raw = response.read()
                    self.assertIn("exhentai-login.ehcred", response.headers["Content-Disposition"])
                self.assertNotIn(b"test-hash-value", raw)
                with server.collector.lock:
                    server.collector.settings = None
                    server.collector.verified = False
                with post("/api/credentials/import", {"file":base64.b64encode(raw).decode(), "settings":{"refresh_days":30}}) as response:
                    restored = json.load(response)
                self.assertTrue(restored["configured"])
                self.assertTrue(restored["has_credentials"])
                self.assertFalse(restored["verified"])
                self.assertEqual((restored["host"], restored["refresh_days"]), ("exhentai.org", 30))
            finally:
                server.shutdown();server.server_close();thread.join()
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
