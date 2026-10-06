from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from uuid import uuid4
import json
import base64
import sqlite3
import tempfile
import threading
import unittest

from app import Catalog, Server
from favorite_transfer import FORMAT, MAX_FILE_BYTES, export_snapshot, merge_snapshot, parse_snapshot, preview_snapshot
from favorites import FavoriteStore
from maintenance import LibraryMaintenance
from collector_identity import export_identity, parse_identity


class FavoriteTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.catalog = self.data / "catalog.sqlite3"
        with closing(sqlite3.connect(self.catalog)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER);
                INSERT INTO tag VALUES(1,'language:english');
            """)
            for gid in range(1, 10):
                db.execute("INSERT INTO gallery VALUES(?,'abcdef0123','Test title','','Non-H',1,10,'4.0',0,0,0)", (gid,))
                db.execute("INSERT INTO gid_tid VALUES(?,1)", (gid,))
            db.commit()
        self.store = FavoriteStore(self.data / "favorites.sqlite3")
        self.engine = LibraryMaintenance(self.catalog, self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def item(self, gid, count, checked_at, source="e-hentai.org", collector_id="", collector_name=""):
        return {"gid": gid, "favorite_count": count, "checked_at": checked_at, "source": source, "collector_id": collector_id, "collector_name": collector_name}

    def raw(self, records):
        return json.dumps({"format": FORMAT, "version": 2, "exported_at": "2026-10-07T00:00:00Z", "records": records}, ensure_ascii=False).encode("utf-8")

    def save(self, gid, count=100, checked_at=100, collector_id="", collector_name=""):
        with self.store.connection() as db:
            db.execute("""INSERT INTO favorites(gid,favorite_count,checked_at,source,collector_id,collector_name)
                VALUES(?,?,?,'exhentai.org',?,?) ON CONFLICT(gid) DO UPDATE SET
                favorite_count=excluded.favorite_count,checked_at=excluded.checked_at,
                collector_id=excluded.collector_id,collector_name=excluded.collector_name
            """, (gid, count, checked_at, collector_id, collector_name))

    def rows(self):
        with self.store.connection() as db:
            return {row["gid"]: dict(row) for row in db.execute("SELECT * FROM favorites")}

    def prepare(self, raw):
        return self.engine.prepare_favorites(self.engine.stage_favorites(raw), "test-task")

    def test_export_only_contains_successful_counts_ids_original_times_and_sources(self):
        self.save(1, 0, 10)
        self.save(2, 123, 20)
        self.store.set_reading_state([1], "watched")
        self.store.mark_opened(1)
        self.store.set_tag_blacklist(["language:english"])
        with self.store.connection() as db:
            self.store._save_failure(db, 2, "abcdef0123", "e-hentai.org", "simulated refresh failure", 3, 7)
            self.store._save_failure(db, 3, "abcdef0123", "e-hentai.org", "never succeeded", 3, 7)
        raw, total = export_snapshot(self.store.path)
        payload = parse_snapshot(raw)
        self.assertEqual(total, 2)
        self.assertEqual(payload["records"], [self.item(1, 0, 10, "exhentai.org"), self.item(2, 123, 20, "exhentai.org")])
        self.assertEqual(set(payload), {"format", "version", "exported_at", "records"})
        for forbidden in ("token", "title", "tag_blacklist", "reading_state", "opened_count", "cookies", "job_id", "failed_at"):
            self.assertNotIn('"' + forbidden + '"', raw.decode())
        self.assertNotIn(str(self.root), raw.decode())

    def test_preview_is_read_only_and_distinguishes_every_conflict_case(self):
        for gid in range(1, 6):
            self.save(gid)
        self.save(7, 0, 100)
        incoming = [self.item(1, 101, 101), self.item(2, 99, 101), self.item(3, 100, 101), self.item(4, 200, 100), self.item(5, 200, 99), self.item(6, 0, 90), self.item(7, 1, 101), self.item(999, 20, 80)]
        before = self.rows()
        summary = preview_snapshot(self.store.path, self.catalog, incoming)
        self.assertEqual(summary, {"total": 8, "added": 2, "updated": 2, "kept": 4, "kept_older_or_equal_time": 2, "kept_count_not_increased": 2, "missing_catalog": 1, "own_records": 0, "other_records": 0, "unknown_records": 8})
        self.assertEqual(self.rows(), before)
        result = merge_snapshot(self.store.path, incoming)
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["updated"], 2)
        after = self.rows()
        self.assertEqual(after[1], self.item(1, 101, 101))
        self.assertEqual(after[6], self.item(6, 0, 90))
        self.assertEqual(after[7], self.item(7, 1, 101))
        self.assertEqual(after[999], self.item(999, 20, 80))
        for gid in (2, 3, 4, 5):
            self.assertEqual(after[gid], before[gid])

    def test_duplicate_import_is_idempotent_and_does_not_advance_timestamp(self):
        records = [self.item(1, 20, 10)]
        self.assertEqual(merge_snapshot(self.store.path, records)["added"], 1)
        self.assertEqual(merge_snapshot(self.store.path, records)["kept"], 1)
        self.assertEqual(self.rows()[1]["checked_at"], 10)

    def test_current_data_is_rechecked_inside_transaction_after_preview(self):
        self.save(1, 10, 100)
        ready = self.prepare(self.raw([self.item(1, 20, 200)]))
        self.assertEqual(ready["summary"]["updated"], 1)
        self.save(1, 30, 300)
        result = self.engine.import_favorites(ready["id"])
        self.assertEqual(result["summary"]["updated"], 0)
        self.assertEqual(result["summary"]["kept"], 1)
        self.assertEqual(self.rows()[1]["favorite_count"], 30)
        self.assertEqual(self.rows()[1]["checked_at"], 300)

    def test_same_gid_matches_despite_title_or_token_changes(self):
        self.save(1, 1, 10)
        with closing(sqlite3.connect(self.catalog)) as db:
            db.execute("UPDATE gallery SET token='1111111111',title='Changed title' WHERE gid=1")
            db.commit()
        result = merge_snapshot(self.store.path, [self.item(1, 2, 11), self.item(2, 3, 12)])
        self.assertEqual((result["updated"], result["added"]), (1, 1))

    def test_queue_reading_blacklist_are_preserved_and_only_outdated_failures_clear(self):
        self.save(1, 10, 100)
        self.save(2, 10, 100)
        self.save(3, 10, 100)
        self.store.set_reading_state([1], "watched")
        self.store.mark_opened(1)
        self.store.set_tag_blacklist(["language:english"])
        with self.store.connection() as db:
            db.execute("INSERT INTO jobs(id,query_json,host,state,total,created_at,updated_at) VALUES(7,'{}','exhentai.org','paused',1,1,1)")
            db.execute("INSERT INTO tasks(job_id,gid,token) VALUES(7,1,'abcdef0123')")
            for gid, failed_at in ((1, 150), (2, 250), (3, 150)):
                self.store._save_failure(db, gid, "abcdef0123", "exhentai.org", "simulated failure", 3, 7)
                db.execute("UPDATE collection_failures SET failed_at=? WHERE gid=?", (failed_at, gid))
        merge_snapshot(self.store.path, [self.item(1, 20, 200), self.item(2, 20, 200), self.item(3, 10, 200)])
        with self.store.connection() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT gid FROM collection_failures ORDER BY gid")], [2, 3])
            self.assertEqual(tuple(db.execute("SELECT state,opened_count FROM record_status WHERE gid=1").fetchone()), ("watched", 1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE job_id=7").fetchone()[0], 1)
        self.assertEqual(self.store.get_tag_blacklist(), ["language:english"])

    def test_invalid_fields_duplicate_ids_and_unsupported_sources_never_write(self):
        self.save(1)
        variants = [dict(self.item(2, 5, 200), gid=0), dict(self.item(2, 5, 200), gid=True), dict(self.item(2, 5, 200), favorite_count=-1), dict(self.item(2, 5, 200), favorite_count=1.5), dict(self.item(2, 5, 200), checked_at=0), dict(self.item(2, 5, 200), checked_at="200"), dict(self.item(2, 5, 200), source="example.invalid"), dict(self.item(2, 5, 200), cookies="test marker"), dict(self.item(2, 5, 200), checked_at=10**20)]
        before = self.rows()
        for item in variants:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.prepare(self.raw([self.item(3, 10, 201), item]))
            self.assertEqual(self.rows(), before)
        with self.assertRaisesRegex(ValueError, "作品 ID 重复"):
            self.prepare(self.raw([self.item(2, 5, 200), self.item(2, 6, 201)]))

    def test_invalid_json_versions_and_repeated_json_fields_reject(self):
        payload = json.loads(self.raw([]))
        payload["version"] = 3
        for raw in (b"invalid", b'{"format":"x","format":"y"}', json.dumps(payload).encode()):
            with self.assertRaises(ValueError):
                parse_snapshot(raw)
        with patch("favorite_transfer.MAX_FILE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "128 MiB"):
                parse_snapshot(self.raw([]))

    def test_empty_snapshot_is_valid_and_does_not_clear_anything(self):
        self.save(1)
        records = parse_snapshot(self.raw([]))["records"]
        result = merge_snapshot(self.store.path, records)
        self.assertEqual(result["total"], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_sql_failure_rolls_back_every_record(self):
        self.save(1, 10, 100)
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_merge BEFORE INSERT ON favorites WHEN NEW.gid=3 BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        before = self.rows()
        with self.assertRaises(sqlite3.Error):
            merge_snapshot(self.store.path, [self.item(1, 20, 200), self.item(2, 30, 200), self.item(3, 40, 200)])
        self.assertEqual(self.rows(), before)

    def test_tampered_stage_or_wrong_confirmation_id_cannot_merge(self):
        ready = self.prepare(self.raw([self.item(1, 20, 200)]))
        with self.assertRaisesRegex(ValueError, "先选择并检查"):
            self.engine.import_favorites("wrong")
        Path(ready["stage"]).write_bytes(self.raw([self.item(1, 99, 999)]))
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.engine.import_favorites(ready["id"])
        self.assertEqual(self.rows(), {})

    def test_import_creates_light_backup_and_missing_catalog_record_remains_visible(self):
        self.save(1, 10, 100)
        ready = self.prepare(self.raw([self.item(1, 20, 200), self.item(999, 0, 90)]))
        result = self.engine.import_favorites(ready["id"])
        backup = Path(result["backup"]["path"])
        self.assertFalse((backup / "data" / "catalog.sqlite3").exists())
        with closing(sqlite3.connect(backup / "data" / "favorites.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT favorite_count,checked_at FROM favorites WHERE gid=1").fetchone(), (10, 100))
        records = Catalog(self.catalog).recorded({})["items"]
        missing = next(item for item in records if item["gid"] == 999)
        self.assertFalse(missing["metadata_available"])
        self.assertEqual((missing["favorite_count"], missing["checked_at"]), (0, 90))
        self.assertIsNone(self.engine.prepared_favorites())

    def test_backup_failure_prevents_any_merge(self):
        ready = self.prepare(self.raw([self.item(1, 20, 200)]))
        with patch.object(self.engine, "backup", side_effect=OSError("simulated full disk")):
            with self.assertRaises(OSError):
                self.engine.import_favorites(ready["id"])
        self.assertEqual(self.rows(), {})

    def test_own_other_unknown_merge_matrix_and_preview_match(self):
        own_id = self.store.collector_identity()["collector_id"]
        other_id = str(uuid4())
        records, accepted = [], set()
        gid = 0
        for collector_id in (own_id, other_id, ""):
            for count in (99, 100, 101):
                for checked_at in (99, 100, 101):
                    gid += 1
                    self.save(gid, collector_id=own_id, collector_name="本地昵称")
                    records.append(self.item(gid, count, checked_at, collector_id=collector_id))
                    if count > 100 and (collector_id == other_id or checked_at > 100):
                        accepted.add(gid)
        before = self.rows()
        preview = preview_snapshot(self.store.path, self.catalog, records)
        self.assertEqual((preview["own_records"], preview["other_records"], preview["unknown_records"]), (9, 9, 9))
        self.assertEqual((preview["updated"], preview["kept"]), (5, 22))
        result = merge_snapshot(self.store.path, records)
        self.assertEqual({key: value for key, value in preview.items() if key != "missing_catalog"}, result)
        after = self.rows()
        for record in records:
            with self.subTest(gid=record["gid"]):
                self.assertEqual(after[record["gid"]], record if record["gid"] in accepted else before[record["gid"]])

    def test_rule_uses_my_current_id_not_existing_records_author(self):
        own_id = self.store.collector_identity()["collector_id"]
        other_id = str(uuid4())
        self.save(1, 10, 100, other_id, "用户乙")
        # Even if both snapshots are from B, B differs from my current identity.
        result = merge_snapshot(self.store.path, [self.item(1, 20, 90, collector_id=other_id, collector_name="用户乙")])
        self.assertEqual(result["updated"], 1)
        result = merge_snapshot(self.store.path, [self.item(1, 30, 80, collector_id=own_id)])
        self.assertEqual(result["kept"], 1)
        self.assertEqual(self.rows()[1]["checked_at"], 90)

    def test_import_reexport_keeps_original_author_and_local_fetch_sets_own_identity(self):
        other = FavoriteStore(self.data / "other.sqlite3")
        identity = other.set_collector_name("用户乙")
        with patch("favorites.time.time", return_value=10):
            other.save(1, 200, "e-hentai.org")
        self.save(1, 100, 100)
        raw, _ = export_snapshot(other.path)
        result = self.engine.import_favorites(self.prepare(raw)["id"])
        self.assertEqual(result["summary"]["updated"], 1)
        raw, _ = export_snapshot(self.store.path)
        self.assertEqual(parse_snapshot(raw)["records"], [self.item(1, 200, 10, **identity)])
        displayed = Catalog(self.catalog).recorded({})["items"][0]
        self.assertEqual((displayed["collector_id"], displayed["collector_name"]), (identity["collector_id"], "用户乙"))
        with patch("favorites.time.time", return_value=150):
            self.store.save(1, 180, "exhentai.org")
        own = self.store.collector_identity()
        self.assertEqual(self.rows()[1], self.item(1, 180, 150, "exhentai.org", **own))

    def test_legacy_file_and_schema_remain_unknown_and_keep_strict_rule(self):
        legacy = self.data / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript("CREATE TABLE favorites(gid INTEGER PRIMARY KEY,favorite_count INTEGER NOT NULL,checked_at INTEGER NOT NULL,source TEXT NOT NULL); INSERT INTO favorites VALUES(1,10,100,'e-hentai.org');")
        migrated = FavoriteStore(legacy)
        identity = migrated.collector_identity()
        self.assertEqual(FavoriteStore(legacy).collector_identity(), identity)
        with migrated.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT collector_id,collector_name FROM favorites").fetchone()), ("", ""))
        payload = json.loads(self.raw([self.item(1, 20, 90), self.item(2, 0, 80)]))
        payload["version"] = 1
        for item in payload["records"]:
            item.pop("collector_id");item.pop("collector_name")
        parsed = parse_snapshot(json.dumps(payload).encode())
        summary = merge_snapshot(legacy, parsed["records"])
        self.assertEqual((summary["added"], summary["updated"], summary["kept"], summary["unknown_records"]), (1, 0, 1, 2))

    def test_identity_is_stable_nickname_can_change_and_migration_keeps_authorship(self):
        original = self.store.collector_identity()
        self.store.save(1, 10, "e-hentai.org")
        other = {"collector_id": str(uuid4()), "collector_name": "用户乙"}
        merge_snapshot(self.store.path, [self.item(2, 20, 100, **other)])
        changed = self.store.set_collector_name("  我的新昵称  ")
        self.assertEqual(changed, {**original, "collector_name": "我的新昵称"})
        self.assertEqual(self.rows()[1]["collector_name"], "我的新昵称")
        self.assertEqual(self.rows()[2]["collector_name"], "用户乙")
        self.assertEqual(FavoriteStore(self.store.path).collector_identity(), changed)
        self.assertEqual(parse_identity(export_identity(changed)), changed)
        ready = self.prepare(self.raw([]))
        before = self.rows()
        result = self.engine.import_collector_identity(other, changed["collector_id"])
        self.assertEqual(self.store.collector_identity(), other)
        self.assertEqual(self.rows(), before)
        self.assertIsNone(self.engine.prepared_favorites())
        with closing(sqlite3.connect(Path(result["backup"]["path"]) / "data" / "favorites.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT value FROM app_settings WHERE key='collector_id'").fetchone()[0], original["collector_id"])
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.engine.import_collector_identity(original, original["collector_id"])
        self.assertEqual(self.store.collector_identity(), other)

    def test_invalid_identity_and_record_provenance_reject_before_write(self):
        own = self.store.collector_identity()
        invalid = [dict(own, collector_id="user-a"), dict(own, collector_id=True), dict(own, collector_id=""), dict(own, collector_id=own["collector_id"].upper()), dict(own, collector_name="x"*41), dict(own, collector_name="x\ny"), dict(own, collector_name=123)]
        for identity in invalid:
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    parse_identity(json.dumps({"format": "local-tag-catalog-collector-identity", "version": 1, **identity}).encode())
                if identity["collector_id"] != "":
                    with self.assertRaises(ValueError):
                        self.prepare(self.raw([self.item(1, 10, 100, **identity)]))
        with self.assertRaises(ValueError):
            self.prepare(self.raw([self.item(1, 10, 100, collector_name="未知姓名")]))
        self.assertEqual(self.rows(), {})
        self.assertEqual(self.store.collector_identity(), own)
        with self.assertRaises(ValueError):parse_identity(b'{"format":"x","format":"y"}')
        with self.assertRaises(ValueError):parse_identity(b'x'*16385)

    def test_identity_backup_failure_leaves_identity_and_records_intact(self):
        own = self.store.collector_identity()
        self.store.save(1, 10, "e-hentai.org")
        before = self.rows()
        with patch.object(self.engine, "backup", side_effect=OSError("simulated full disk")):
            with self.assertRaises(OSError):
                self.engine.import_collector_identity({"collector_id": str(uuid4()), "collector_name": ""}, own["collector_id"])
        self.assertEqual(self.store.collector_identity(), own)
        self.assertEqual(self.rows(), before)

    def test_identity_generation_is_idempotent_and_anonymous_state_is_persistent(self):
        own = self.store.collector_identity()
        self.assertFalse(self.engine.collector_identity_status()["collector_identity_initialized"])
        self.assertEqual(self.store.set_collector_name("", initialize=True), own)
        self.assertTrue(self.engine.collector_identity_status()["collector_identity_initialized"])
        self.assertEqual(self.store.set_collector_name("", initialize=True), own)
        with self.assertRaisesRegex(ValueError, "修改昵称"):
            self.store.set_collector_name("其他昵称", initialize=True)
        self.assertEqual(self.store.collector_identity(), own)
        self.assertEqual(FavoriteStore(self.store.path).collector_identity(), own)
        self.assertTrue(self.engine.collector_identity_status()["collector_identity_initialized"])
        changed = self.store.set_collector_name("新昵称")
        self.assertEqual(changed["collector_id"], own["collector_id"])

    def test_identity_and_nickname_survive_service_stop_and_restart(self):
        identity = self.store.collector_identity()
        for restart in range(3):
            server = Server(("127.0.0.1", 0), Catalog(self.catalog))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                if restart == 0:
                    with urlopen(Request(base+"/api/maintenance/collector-identity-create",data=json.dumps({"collector_name":"测试用户"}).encode(),headers={"Content-Type":"application/json","X-Catalog-Token":server.action_token}),timeout=10) as response:
                        state = json.load(response)
                    identity = {**identity, "collector_name": "测试用户"}
                else:
                    with urlopen(base+"/api/maintenance",timeout=10) as response:
                        state = json.load(response)
                self.assertEqual(state["collector_identity"], identity)
                self.assertTrue(state["collector_identity_initialized"])
                if restart == 1:
                    with urlopen(Request(base+"/api/maintenance/collector-name",data=json.dumps({"collector_name":"新昵称"}).encode(),headers={"Content-Type":"application/json","X-Catalog-Token":server.action_token}),timeout=10) as response:
                        changed = json.load(response)
                    identity = {**identity, "collector_name": "新昵称"}
                    self.assertEqual(changed["collector_identity"], identity)
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_web_identity_settings_export_check_migrate_and_authentication(self):
        server = Server(("127.0.0.1", 0), Catalog(self.catalog))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        own = self.store.collector_identity()
        self.store.save(1, 10, "e-hentai.org")
        def post(name, payload=None, authorized=True):
            return urlopen(Request(base + "/api/maintenance/" + name, data=json.dumps(payload or {}).encode(), headers={"Content-Type": "application/json", "X-Catalog-Token": server.action_token if authorized else "", "Origin": base}), timeout=10)
        try:
            for name in ("collector-identity-create", "collector-name", "collector-identity-export", "collector-identity-check", "collector-identity-import"):
                with self.subTest(name=name), self.assertRaises(HTTPError) as failure:
                    post(name, authorized=False)
                self.assertEqual(failure.exception.code, 403)
                failure.exception.close()
            for _ in range(2):
                with post("collector-identity-create", {"collector_name": "测试昵称"}) as response:
                    state = json.load(response)
                    self.assertEqual(state["collector_identity"], {**own, "collector_name": "测试昵称"})
                    self.assertTrue(state["collector_identity_initialized"])
            with self.assertRaises(HTTPError) as failure:
                post("collector-identity-create", {"collector_name": "重建昵称"})
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()
            with post("collector-name", {"collector_name": "修改后的昵称"}) as response:
                self.assertEqual(json.load(response)["collector_identity"], {**own, "collector_name": "修改后的昵称"})
            with post("collector-identity-export") as response:
                self.assertIn(".ehcollector.json", response.headers["Content-Disposition"])
                self.assertEqual(parse_identity(response.read()), {**own, "collector_name": "修改后的昵称"})
            target = {"collector_id": str(uuid4()), "collector_name": "迁移身份"}
            encoded = base64.b64encode(export_identity(target)).decode("ascii")
            with post("collector-identity-check", {"file": encoded}) as response:
                checked = json.load(response)
                self.assertEqual(checked, {"identity": target, "current_id": own["collector_id"]})
            self.assertEqual(self.store.collector_identity()["collector_id"], own["collector_id"])
            with self.assertRaises(HTTPError) as failure:
                post("collector-identity-import", {"file": encoded, "expected_id": str(uuid4())})
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()
            with post("collector-identity-import", {"file": encoded, "expected_id": checked["current_id"]}) as response:
                json.load(response)
            server.maintenance.thread.join(timeout=5)
            self.assertEqual(server.maintenance.status()["phase"], "completed")
            self.assertEqual(server.maintenance.status()["collector_identity"], target)
            self.assertEqual(self.rows()[1]["collector_id"], own["collector_id"])
            self.assertTrue(Path(server.maintenance.status()["result"]["backup"]["path"]).is_dir())
            self.assertIsNone(server.collector.preview_data)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_web_export_upload_over_32k_and_authenticated_merge_preserve_login(self):
        self.save(1, 10, 100)
        server = Server(("127.0.0.1", 0), Catalog(self.catalog))
        server.collector.configure({"host": "e-hentai.org", "interval": 3})
        server.collector.verified = True
        settings = dict(server.collector.settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def post(name, raw=b"{}", content_type="application/json", authorized=True, origin=None):
            return urlopen(Request(base + "/api/maintenance/" + name, data=raw, headers={"Content-Type": content_type, "X-Catalog-Token": server.action_token if authorized else "", "Origin": origin or base}), timeout=10)
        try:
            with urlopen(base + "/maintenance") as response:
                self.assertIn("仅导出 / 合并收藏数", response.read().decode())
            for name, raw in (("favorites-export", b"{}"), ("favorites-prepare", self.raw([]))):
                with self.assertRaises(HTTPError) as failure:
                    post(name, raw, authorized=False)
                self.assertEqual(failure.exception.code, 403)
                failure.exception.close()
            with self.assertRaises(HTTPError) as failure:
                post("favorites-export", origin="https://example.invalid")
            self.assertEqual(failure.exception.code, 403)
            failure.exception.close()
            with post("favorites-export") as response:
                self.assertIn(".ehfavorites.json", response.headers["Content-Disposition"])
                self.assertEqual(parse_snapshot(response.read())["records"], [self.item(1, 10, 100, "exhentai.org")])
            raw = self.raw([self.item(gid, 20, 200) for gid in range(1, 451)])
            self.assertGreater(len(raw), 32000)
            with post("favorites-prepare", raw, "application/octet-stream") as response:
                result = json.load(response)
            server.maintenance.thread.join(timeout=5)
            ready = server.maintenance.status()["prepared_favorites"]
            self.assertEqual(ready["task_id"], result["requested_id"])
            self.assertEqual(ready["summary"]["added"], 449)
            self.assertEqual(len(self.rows()), 1)
            with post("favorites-import", json.dumps({"prepared_id": ready["id"]}).encode()) as response:
                json.load(response)
            server.maintenance.thread.join(timeout=5)
            self.assertEqual(server.maintenance.status()["phase"], "completed")
            self.assertEqual(len(self.rows()), 450)
            self.assertEqual(self.rows()[1]["checked_at"], 200)
            self.assertEqual(server.collector.settings, settings)
            self.assertTrue(server.collector.verified)
            self.assertIsNone(server.collector.preview_data)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
