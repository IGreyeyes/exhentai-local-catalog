from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
import json
import tempfile
import threading
import time
import unittest

from cloud_sync import CloudSync, DEFAULT_ENDPOINT, SyncReset, SyncRateLimited, UploadBlocked, UploadIdentityMissing, encode_snapshot, request_json, validate_endpoint, NoRedirect
from favorites import FavoriteStore

CODE = "exc_" + "test_only_placeholder_" + "0"*21
OTHER = "00000000-0000-4000-8000-000000000002"
DATASET = "0"*32


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FavoriteStore(Path(self.temp.name)/"favorites.sqlite3")
        self.gate = threading.RLock()
        self.requests = []
        self.responses = []
        self.sync = CloudSync(lambda: self.store, self.gate, transport=self.transport, start=False)
        self.now = int(time.time())-100

    def tearDown(self):
        self.sync.close()
        self.temp.cleanup()

    def transport(self, endpoint, path, payload=None, token=""):
        self.requests.append((endpoint,path,payload,token))
        if path == "/health":
            return {"version":3,"max_upload_batch":5}
        if path == "/v3/register":
            return {"version":3,"source_id":OTHER,"status":"review"}
        if payload is not None:
            return {"version":3,"accepted":len(payload["records"]),"pending":0,"kept":0}
        result = self.responses.pop(0) if self.responses else self.page([])
        if isinstance(result,Exception):
            raise result
        return result

    def config(self, **changes):
        return {"endpoint":DEFAULT_ENDPOINT,"download_enabled":True,"upload_enabled":False,"consent":False,**changes}

    def test_uploader_name_persists_independently_and_old_settings_keep_it(self):
        self.sync.configure(self.config(uploader_name=" 测试上传者 ", download_enabled=False))
        self.sync.configure(self.config(download_enabled=False))
        restarted = CloudSync(lambda: self.store, self.gate, start=False)
        try:
            self.assertEqual(restarted.status()["uploader_name"], "测试上传者")
            self.assertFalse(restarted.status()["enabled"])
            self.assertEqual(self.read("SELECT value FROM app_settings WHERE key='collector_name'")["value"], "")
        finally:
            restarted.close()
        for name in (None, 123, "字" * 41, "中\n文", "中\x7f文"):
            with self.assertRaises(ValueError):
                self.sync.configure(self.config(uploader_name=name))
        self.assertEqual(self.sync.status()["uploader_name"], "测试上传者")

    def test_rename_registered_source_without_resending_confirmed_records(self):
        self.store.save(1,100,"e-hentai.org",20)
        config = self.config(upload_enabled=True, consent=True, download_enabled=False)
        self.sync.configure({**config, "uploader_name":"测试上传者"})
        with patch.object(self.sync.stopped,"wait",return_value=False):
            self.sync.run_once()
        first = next(r for r in self.requests if r[1]=="/v3/register")
        self.assertEqual(first[2]["uploader_name"], "测试上传者")
        self.assertEqual(first[2]["collector_name"], "")
        self.requests.clear()
        self.sync.configure({**config, "uploader_name":"改名测试"})
        self.sync.run_once()
        self.assertEqual([r[1] for r in self.requests], ["/v3/register"])
        self.assertEqual(self.requests[0][3], first[3])
        self.assertEqual(self.requests[0][2]["uploader_name"], "改名测试")
        self.assertEqual(self.sync.status()["source_id"], OTHER)
        self.assertEqual(self.sync.status()["upload_confirmed"], 1)
        with self.store.connection() as db:
            db.execute("DELETE FROM favorites")
        self.requests.clear()
        self.sync.configure({**config, "uploader_name":""})
        self.sync.run_once()
        self.assertEqual([r[1] for r in self.requests], ["/v3/register"])
        self.assertEqual(self.requests[0][2]["uploader_name"], "")
        self.requests.clear()
        self.sync.run_once()
        self.assertFalse(self.requests)

    def test_failed_rename_keeps_name_identity_and_queue_for_retry(self):
        self.store.save(1,100,"e-hentai.org",20)
        config = self.config(upload_enabled=True, consent=True)
        self.sync.configure(config)
        with patch.object(self.sync.stopped,"wait",return_value=False):
            self.sync.run_once()
        self.sync.configure({**config, "uploader_name":"网络重试测试"})
        original = self.sync.transport
        def offline(endpoint,path,payload=None,token=""):
            if path=="/v3/register":
                raise ValueError("测试网络不可用")
            return original(endpoint,path,payload,token)
        self.sync.transport = offline
        self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"], "retrying")
        self.assertEqual(self.sync.status()["uploader_name"], "网络重试测试")
        self.assertEqual(self.sync.status()["source_id"], OTHER)
        self.assertEqual(self.read("SELECT count(*) AS n FROM cloud_upload_ack")["n"], 1)
        self.requests.clear()
        self.sync.transport = original
        self.sync.run_once()
        self.assertEqual([r[1].split('?')[0] for r in self.requests], ["/v3/register", "/v3/changes"])

    def item(self, gid=1, count=100, rating=20, checked=None, revision=1):
        return {"gid":gid,"favorite_count":count,"rating_count":rating,"checked_at":checked or self.now,
                "source":"e-hentai.org","collector_id":OTHER,"collector_name":"测试采集者","revision":revision}

    def page(self, records, **changes):
        last = records[-1]["revision"] if records else 0
        records = [{"gid":r["gid"],"revision":r["revision"],"action":"snapshot","snapshot":{k:v for k,v in r.items() if k!="revision"}} for r in records]
        return {"format":"excatalog-cloud-sync","version":3,"dataset_id":DATASET,"cursor":last,"until":last,
                "head":last,"has_more":False,"records":records,**changes}

    def read(self, sql, values=()):
        with self.store.connection() as db:
            row = db.execute(sql, values).fetchone()
            return dict(row) if row else None

    def test_opt_in_persistence_and_automatic_identity(self):
        self.sync.run_once()
        self.assertFalse(self.requests)
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(upload_enabled=True,consent=True))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        token=self.read("SELECT value FROM app_settings WHERE key='cloud_upload_token'")["value"]
        self.assertTrue(token.startswith("src_"))
        self.assertNotIn(token,json.dumps(self.sync.status()))
        restarted=CloudSync(lambda:self.store,self.gate,transport=self.transport,start=False)
        try:
            self.assertTrue(restarted.status()["upload_enabled"])
            self.assertEqual(restarted.status()["source_id"],OTHER)
            self.assertEqual(restarted._upload_identity(self.config()),token)
        finally:restarted.close()

    def test_upload_only_never_downloads_and_remains_enabled_after_restart(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertTrue(any(r[1]=="/v3/contribute" for r in self.requests))
        self.assertFalse(any(r[1].startswith("/v3/changes") for r in self.requests))
        restarted=CloudSync(lambda:self.store,self.gate,transport=self.transport,start=False)
        try:
            self.assertTrue(restarted.status()["enabled"])
            self.assertFalse(restarted.status()["download_enabled"])
            self.assertTrue(restarted.status()["upload_enabled"])
        finally:restarted.close()

    def test_all_saved_upload_download_choices_survive_reopened_database(self):
        for upload,download in ((False,False),(True,True),(True,False),(False,True)):
            with self.subTest(upload=upload,download=download):
                self.sync.configure(self.config(download_enabled=download,upload_enabled=upload,consent=upload))
                reopened_store=FavoriteStore(self.store.path)
                restarted=CloudSync(lambda:reopened_store,self.gate,transport=self.transport,start=False)
                try:
                    state=restarted.status()
                    self.assertTrue(state["configured"])
                    self.assertEqual(state["upload_enabled"],upload)
                    self.assertEqual(state["download_enabled"],download)
                    self.assertEqual(state["enabled"],upload or download)
                finally:restarted.close()

    def test_first_run_has_no_network_or_active_upload_before_confirmation(self):
        state=self.sync.status()
        self.assertFalse(state["configured"])
        self.assertFalse(state["download_enabled"])
        self.assertFalse(state["upload_enabled"])
        self.sync.run_once()
        self.assertFalse(self.requests)

    def test_upload_only_failure_does_not_report_success_or_start_download(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        self.sync.transport=lambda *args,**kwargs:(_ for _ in ()).throw(ValueError("test-only upload failure"))
        self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"retrying")
        self.assertIsNone(self.sync.status()["last_success"])
        self.assertFalse(self.requests)

    def test_legacy_download_preference_is_preserved(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO app_settings VALUES('cloud_enabled','1')")
            db.execute("INSERT INTO app_settings VALUES('cloud_upload_enabled','0')")
        self.assertTrue(self.sync.status()["configured"])
        self.assertTrue(self.sync.status()["download_enabled"])
        self.assertFalse(self.sync.status()["upload_enabled"])
        self.sync.configure({"endpoint":DEFAULT_ENDPOINT,"enabled":False,"upload_enabled":True,"consent":True})
        self.assertTrue(self.sync.status()["enabled"])
        self.assertFalse(self.sync.status()["download_enabled"])

    def test_upload_requires_consent_and_rejects_extra_credentials(self):
        for config in (self.config(upload_enabled=True),self.config(upload_enabled=True,consent=True,enabled=False),
                       self.config(contribution_code="test-only-placeholder")):
            with self.assertRaises(ValueError):self.sync.configure(config)
        self.assertFalse(self.sync.status()["enabled"])

    def test_public_snapshot_merges_pair_and_preserves_personal_status_newer_failures(self):
        self.store.save(1,500,"e-hentai.org",30)
        with self.store.connection() as db:
            db.execute("UPDATE favorites SET checked_at=? WHERE gid=1",(self.now-10,))
            db.execute("INSERT INTO record_status(gid,state,opened_count) VALUES(1,'watched',7)")
            db.execute("INSERT INTO collection_failures VALUES(1,'abcdef0123','e-hentai.org',?,'test-only failure',1,0)",(self.now+10,))
        self.sync.configure(self.config())
        self.responses=[self.page([self.item(1,90,None),self.item(99,0,50,revision=2)])]
        self.sync.run_once()
        self.assertEqual(self.read("SELECT favorite_count,checked_at FROM favorites WHERE gid=1"),{"favorite_count":90,"checked_at":self.now})
        self.assertIsNone(self.read("SELECT * FROM rating_counts WHERE gid=1"))
        self.assertEqual(self.read("SELECT state,opened_count FROM record_status WHERE gid=1"),{"state":"watched","opened_count":7})
        self.assertIsNotNone(self.read("SELECT gid FROM collection_failures WHERE gid=1"))
        self.assertEqual(self.read("SELECT rating_count FROM rating_counts WHERE gid=99")["rating_count"],50)
        self.assertEqual(self.sync.status()["downloaded"],2)

    def test_newer_local_data_retained_but_equal_time_uses_canonical_public_snapshot(self):
        self.store.save(1,300,"e-hentai.org",60)
        self.store.save(2,500,"e-hentai.org",100)
        with self.store.connection() as db:
            db.execute("UPDATE favorites SET checked_at=? WHERE gid=2",(self.now,))
        self.sync.configure(self.config())
        self.responses=[self.page([self.item(),self.item(2,400,80,revision=2)])]
        self.sync.run_once()
        self.assertEqual(self.read("SELECT favorite_count FROM favorites WHERE gid=1")["favorite_count"],300)
        self.assertEqual(self.read("SELECT favorite_count FROM favorites WHERE gid=2")["favorite_count"],400)

    def test_download_validation_is_atomic_and_cursor_does_not_advance_on_error(self):
        self.sync.configure(self.config())
        for bad in ({**self.item(2,revision=2),"cookies":"test-only placeholder"},{**self.item(1,revision=2),"checked_at":True},
                    {**self.item(2,revision=2),"rating_count":True},{**self.item(2,revision=2),"checked_at":int(time.time())+1000}):
            self.responses=[self.page([self.item(),bad])]
            self.sync.run_once()
            self.assertIsNone(self.read("SELECT * FROM favorites"))
            self.assertEqual(self.read("SELECT value FROM app_settings WHERE key='cloud_cursor'")["value"],"0")
        self.responses=[self.page([self.item()],has_more=True,cursor=2,until=3,head=3)]
        self.sync.run_once()
        self.assertIsNone(self.read("SELECT * FROM favorites"))

    def test_library_upload_preserves_attribution_and_confirmed_records_are_not_resent(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.store.save(2,200,"e-hentai.org",None)
        with self.store.connection() as db:
            db.execute("UPDATE favorites SET collector_id=?,collector_name='测试原采集者' WHERE gid=2",(OTHER,))
            db.execute("INSERT INTO favorites VALUES(3,30,?,'e-hentai.org','','')",(self.now,))
        self.sync.configure(self.config(upload_enabled=True,consent=True))
        self.responses=[self.page([self.item(1,110,22,checked=int(time.time())+1)])]
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        uploads=[request for request in self.requests if request[1] == "/v3/contribute"]
        self.assertEqual(len(uploads),1)
        self.assertEqual([item["gid"] for item in uploads[0][2]["records"]],[1,2,3])
        self.assertEqual(uploads[0][2]["records"][1]["collector_id"],OTHER)
        self.assertEqual(uploads[0][2]["records"][1]["collector_name"],"测试原采集者")
        self.assertEqual(uploads[0][2]["records"][2]["collector_id"],"")
        self.assertEqual(set(uploads[0][2]["records"][0]),{"gid","favorite_count","rating_count","checked_at","source","collector_id","collector_name"})
        self.assertTrue(uploads[0][3].startswith("src_"))
        self.requests=[]
        # The newer public value can be offered once; the original self snapshot stays available for corrections.
        self.responses=[self.page([],cursor=1,until=1,head=1)]
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        forwarded=[request for request in self.requests if request[1] == "/v3/contribute"]
        self.assertEqual([item["gid"] for item in forwarded[0][2]["records"]],[1])
        self.assertEqual(forwarded[0][2]["records"][0]["collector_id"],OTHER)
        self.requests=[]
        self.responses=[self.page([],cursor=1,until=1,head=1)]
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertFalse(any(request[1] == "/v3/contribute" for request in self.requests))

    def test_imported_records_after_startup_upload_without_own_id_or_outbox(self):
        from favorite_transfer import merge_snapshot
        records=[{k:v for k,v in self.item(gid, revision=gid).items() if k not in {"rating_count","revision"}} for gid in (1,2)]
        records[1].update(collector_id="",collector_name="")
        merge_snapshot(self.store.path,records)
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        uploads=[request for request in self.requests if request[1]=="/v3/contribute"]
        self.assertEqual([item["gid"] for item in uploads[0][2]["records"]],[1,2])
        self.assertEqual([item["collector_id"] for item in uploads[0][2]["records"]],[OTHER,""])
        self.assertTrue(all(item["rating_count"] is None for item in uploads[0][2]["records"]))
        self.assertIsNone(self.read("SELECT * FROM cloud_self_snapshots"))
        self.assertEqual(self.sync.status()["upload_total"],2)
        self.assertEqual(self.sync.status()["upload_confirmed"],2)
        self.assertEqual(self.sync.status()["upload_remaining"],0)
        self.assertEqual(self.read("SELECT collector_id FROM favorites WHERE gid=1")["collector_id"],OTHER)

    def test_paired_ratings_only_and_failure_without_count_is_excluded(self):
        self.store.save(1,100,"e-hentai.org",20)
        with self.store.connection() as db:
            db.execute("UPDATE rating_counts SET checked_at=1 WHERE gid=1")
            db.execute("INSERT INTO collection_failures VALUES(2,'test-only-placeholder','e-hentai.org',?,'模拟失败',1,1)",(self.now,))
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        items=next(request[2]["records"] for request in self.requests if request[1]=="/v3/contribute")
        self.assertEqual([item["gid"] for item in items],[1])
        self.assertIsNone(items[0]["rating_count"])
        self.assertEqual(self.sync.status()["upload_total"],1)
        self.assertIsNotNone(self.read("SELECT * FROM collection_failures WHERE gid=2"))

    def test_unknown_collector_download_and_forwarding_remain_unknown(self):
        unknown=self.item()
        unknown.update(collector_id="",collector_name="")
        self.apply_events([self.event(unknown,1)])
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        item=next(request[2]["records"][0] for request in self.requests if request[1]=="/v3/contribute")
        self.assertEqual(item["collector_id"],"")
        self.assertEqual(item["collector_name"],"")

    def test_changed_library_record_resubmits_once_and_deleted_record_leaves_queue(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO favorites VALUES(1,100,?,'e-hentai.org',?,'测试原采集者')",(self.now,OTHER))
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        with self.store.connection() as db:
            db.execute("UPDATE favorites SET favorite_count=120,checked_at=checked_at+1 WHERE gid=1")
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual([request[2]["records"][0]["favorite_count"] for request in self.requests if request[1]=="/v3/contribute"],[100,120])
        with self.store.connection() as db:db.execute("DELETE FROM favorites WHERE gid=1")
        self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_total"],0)
        self.assertEqual(self.sync.status()["upload_remaining"],0)
        self.assertIsNone(self.read("SELECT * FROM cloud_upload_snapshots"))

    def test_v2_upgrade_keeps_cursor_and_acknowledged_payloads(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        with self.store.connection() as db:
            self.sync._put(db,"cloud_protocol",2)
            self.sync._put(db,"cloud_cursor",17)
            self.sync._put(db,"cloud_dataset",DATASET)
        self.requests=[]
        restarted=CloudSync(lambda:self.store,self.gate,transport=self.transport,start=False)
        try:
            restarted.run_once()
            self.assertFalse(any(request[1]=="/v3/contribute" for request in self.requests))
            self.assertEqual(self.read("SELECT value FROM app_settings WHERE key='cloud_cursor'")["value"],"17")
            self.assertEqual(restarted.status()["upload_confirmed"],1)
        finally:restarted.close()

    def test_record_changed_during_upload_continues_instead_of_reporting_complete(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(download_enabled=False,upload_enabled=True,consent=True))
        original=self.sync.transport
        changed=False
        def update_during_request(endpoint,path,payload=None,token=""):
            nonlocal changed
            if path=="/v3/contribute" and not changed:
                changed=True
                with self.store.connection() as db:
                    db.execute("UPDATE favorites SET favorite_count=120,checked_at=checked_at+1 WHERE gid=1")
            return original(endpoint,path,payload,token)
        self.sync.transport=update_during_request
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"continuing")
        self.assertEqual(self.sync.status()["upload_remaining"],1)
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"waiting")
        self.assertEqual(self.sync.status()["upload_confirmed"],1)
        self.assertEqual(self.sync.status()["session_upload_processed"],2)

    def test_pairing_null_in_own_outbox_and_seeding_legacy_own_records(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.store.save(1,110,"e-hentai.org",None)
        item=json.loads(self.read("SELECT payload FROM cloud_self_snapshots WHERE gid=1")["payload"])
        self.assertIsNone(item["rating_count"])
        with self.store.connection() as db:
            db.execute("DELETE FROM app_settings WHERE key='cloud_self_seeded'")
            db.execute("DELETE FROM cloud_self_snapshots")
            db.execute("UPDATE rating_counts SET checked_at=1")
            db.execute("INSERT INTO favorites VALUES(2,10,1,'e-hentai.org',?,'测试他人')",(OTHER,))
        self.sync._seed_self()
        self.assertIsNone(json.loads(self.read("SELECT payload FROM cloud_self_snapshots WHERE gid=1")["payload"])["rating_count"])
        self.assertIsNone(self.read("SELECT payload FROM cloud_self_snapshots WHERE gid=2"))

    def test_upload_failure_does_not_stop_download_and_unacknowledged_records_retry(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(upload_enabled=True,consent=True))
        original=self.sync.transport
        def fail(endpoint,path,payload=None,token=""):
            if path == "/v3/contribute":raise ValueError("test-only upload failure")
            return original(endpoint,path,payload,token)
        self.sync.transport=fail
        self.responses=[self.page([self.item(99)])]
        self.sync.run_once()
        self.assertIsNotNone(self.read("SELECT gid FROM favorites WHERE gid=99"))
        self.assertIsNone(self.read("SELECT * FROM cloud_upload_ack"))
        self.assertIn("上传未完成",self.sync.status()["message"])
        self.assertNotIn(CODE,json.dumps(self.sync.status()))

    def test_pagination_restart_from_saved_cursor_and_dataset_reset(self):
        self.sync.configure(self.config())
        self.responses=[self.page([self.item()],cursor=1,until=2,head=2,has_more=True),self.page([self.item(2,revision=2)],cursor=2,until=2,head=2)]
        self.sync.run_once()
        self.assertIn("after=1",self.requests[1][1]);self.assertIn("until=2",self.requests[1][1])
        self.requests=[]
        self.responses=[SyncReset("test-only reset"),self.page([self.item(3)],dataset_id="1"*32)]
        self.sync.run_once()
        self.assertIn("after=2",self.requests[0][1]);self.assertIn("after=0",self.requests[1][1])
        self.assertNotIn("dataset_id",self.requests[1][1])
        self.assertEqual(self.read("SELECT value FROM app_settings WHERE key='cloud_dataset'")["value"],"1"*32)
        self.assertIsNone(self.read("SELECT gid FROM favorites WHERE gid=1"))

    def test_changing_endpoint_clears_auth_and_ack_and_does_not_allow_credentials_in_url(self):
        self.sync.configure(self.config(upload_enabled=True,consent=True))
        self.sync.configure(self.config(endpoint="https://sync.example.invalid"))
        self.assertFalse(self.sync.status()["source_id"])
        for value in ("http://sync.example.invalid","https://user:password@sync.example.invalid","https://sync.example.invalid/v1","https://sync.example.invalid/?test=1"):
            with self.assertRaises(ValueError):validate_endpoint(value)
        with self.assertRaises(ValueError):NoRedirect().redirect_request(None,None,None,None,None,None)

    def test_maintenance_defers_updates(self):
        self.sync.configure(self.config())
        self.sync.maintenance_busy=lambda:True
        with self.assertRaises(ValueError):self.sync.run_once()
        self.assertFalse(self.requests)
        with self.assertRaises(ValueError):self.sync.configure(self.config())

    def test_http_error_never_echoes_remote_body_or_secret(self):
        from io import BytesIO
        for code in (401,429,503,409,404,426):
            failure=HTTPError("https://sync.example.invalid",code,"test",{},BytesIO(CODE.encode()))
            with patch("cloud_sync.build_opener") as opener:
                opener.return_value.open.side_effect=failure
                with self.assertRaises(ValueError) as raised:request_json("https://sync.example.invalid","/v3/contribute",{},CODE)
            self.assertNotIn(CODE,str(raised.exception))

    def test_invalid_confirmation_keeps_batch_and_valid_retry_confirms_each_record_once(self):
        self.seed_upload_queue(5)
        original=self.sync.transport
        attempts=0
        def confirmation(endpoint,path,payload=None,token=""):
            nonlocal attempts
            if path=="/v3/contribute":
                attempts+=1
                return ({"version":3,"accepted":0,"pending":6,"kept":-1} if attempts==1
                        else {"version":3,"accepted":0,"pending":3,"kept":2})
            return original(endpoint,path,payload,token)
        self.sync.transport=confirmation
        self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"retrying")
        self.assertEqual(self.sync.status()["upload_remaining"],5)
        self.assertEqual(self.sync.status()["upload_confirmed"],0)
        self.assertEqual(self.sync.status()["session_upload_processed"],0)
        self.assertIsNone(self.read("SELECT * FROM cloud_upload_ack"))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"waiting")
        self.assertEqual(self.sync.status()["upload_remaining"],0)
        self.assertEqual(self.sync.status()["upload_confirmed"],5)
        self.assertEqual(self.sync.status()["session_upload_processed"],5)
        self.assertEqual(attempts,2)

    def seed_upload_queue(self, count):
        with self.store.connection() as db:
            for gid in range(1,count+1):
                self.store._save(db,gid,100,"e-hentai.org",20)
        self.sync.configure(self.config(upload_enabled=True,consent=True))

    def fast_transport(self, endpoint, path, payload=None, token=""):
        if path=="/health":
            return {"version":3,"max_upload_batch":50}
        return self.transport(endpoint,path,payload,token)

    def preflight_transport(self, endpoint, path, payload=None, token=""):
        if path=="/health":return {"version":3,"max_upload_batch":50,"upload_preflight":True,
            "max_upload_check_batch":500,"public_correction_state":True,
            "upload_min_interval_days":getattr(self,"interval_days",14)}
        if path.startswith("/v3/upload-state?"):
            from urllib.parse import parse_qs,urlsplit
            query=parse_qs(urlsplit(path).query);dataset=query.get("dataset_id",[""])[0]
            head=getattr(self,"preflight_head",1);active=getattr(self,"preflight_dataset",DATASET)
            return {"version":3,"dataset_id":active,"reset":dataset!=active or int(query['after'][0])>head,
                "cursor":head,"until":head,"has_more":False,"corrections":[]}
        if path=="/v3/upload-check":
            self.preflight_requests.append(payload)
            return {"version":3,"dataset_id":getattr(self,"preflight_dataset",DATASET),"source_id":OTHER,"status":"review",
                "records":[{"gid":r["gid"],"skip":r["gid"] in self.public_gids,"revision":1 if r["gid"] in self.public_gids else 0} for r in payload["records"]]}
        return self.transport(endpoint,path,payload,token)

    def test_overlap_15000_skips_8000_and_only_uploads_7000(self):
        self.seed_upload_queue(15000);self.public_gids=set(range(1,8001));self.preflight_requests=[]
        self.sync.transport=self.preflight_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):
            for _ in range(20):
                self.sync.run_once()
                if self.sync.status()["phase"]=="waiting":break
        state=self.sync.status();uploads=[r for r in self.requests if r[1]=="/v3/contribute"]
        self.assertEqual(state["upload_remaining"],0);self.assertEqual(state["upload_skipped"],8000)
        self.assertEqual(state["upload_confirmed"],15000);self.assertEqual(state["session_upload_processed"],7000)
        self.assertEqual(len(uploads),140);self.assertEqual(len(self.preflight_requests),30)
        self.assertEqual([v["gid"] for r in uploads for v in r[2]["records"]],list(range(8001,15001)))
        self.sync.run_once();self.assertEqual(len(self.preflight_requests),30)

    def test_skip_cache_restart_manual_recheck_and_correction_invalidation(self):
        self.seed_upload_queue(3);self.public_gids={1,2,3};self.preflight_requests=[]
        self.sync.transport=self.preflight_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_skipped"],3)
        restarted=CloudSync(lambda:self.store,self.gate,transport=self.preflight_transport,start=False)
        try:
            restarted.run_once();self.assertEqual(len(self.preflight_requests),1)
            self.public_gids.remove(2);original=restarted.transport
            def corrected(endpoint,path,payload=None,token=""):
                if path.startswith('/v3/upload-state?'):
                    return {"version":3,"dataset_id":DATASET,"reset":False,"cursor":2,"until":2,"has_more":False,
                        "corrections":[{"gid":2,"revision":2}]}
                return original(endpoint,path,payload,token)
            restarted.transport=corrected
            with patch.object(restarted.stopped,"wait",return_value=False):restarted.run_once()
            self.assertEqual(restarted.status()["upload_skipped"],2)
            self.assertEqual(restarted.status()["session_upload_processed"],1)
            self.preflight_head=2
            restarted.transport=original
            restarted.trigger()
            with patch.object(restarted.stopped,"wait",return_value=False):restarted.run_once()
            self.assertEqual(restarted.status()["upload_skipped"],2)
        finally:restarted.close()

    def test_preflight_dataset_reset_and_expired_confirmations_are_rechecked(self):
        self.seed_upload_queue(2);self.public_gids={1,2};self.preflight_requests=[]
        self.sync.transport=self.preflight_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        with self.store.connection() as db:db.execute("UPDATE cloud_upload_skips SET verified_at=?",(int(time.time())-86401,))
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(len(self.preflight_requests),2);self.assertEqual(self.sync.status()["upload_skipped"],2)
        self.preflight_dataset="1"*32;self.public_gids.clear()
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_skipped"],0);self.assertEqual(self.sync.status()["session_upload_processed"],2)
        self.assertEqual(self.sync.status()["upload_remaining"],0)

    def test_interleaved_public_records_do_not_repeat_checks_at_upload_batch_boundaries(self):
        self.seed_upload_queue(1200);self.public_gids=set(range(3,1201,3));self.preflight_requests=[]
        self.sync.transport=self.preflight_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):
            for _ in range(4):self.sync.run_once()
        checked=[r["gid"] for p in self.preflight_requests for r in p["records"]]
        uploads=[r for r in self.requests if r[1]=="/v3/contribute"]
        self.assertEqual(len(checked),1200);self.assertEqual(len(set(checked)),1200)
        self.assertEqual(len(uploads),16);self.assertEqual(self.sync.status()["upload_skipped"],400)

    def test_changed_interval_rechecks_skip_cache_but_keeps_actual_upload_confirmations(self):
        self.seed_upload_queue(3);self.public_gids={1,2};self.preflight_requests=[]
        self.sync.transport=self.preflight_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_min_interval_days"],14)
        self.assertEqual(self.sync.status()["upload_skipped"],2)
        self.assertEqual(self.sync.status()["session_upload_processed"],1)
        self.interval_days=0;self.public_gids={1}
        self.sync.capabilities_refresh_at=0
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_min_interval_days"],0)
        self.assertEqual(self.sync.status()["upload_skipped"],1)
        self.assertEqual(self.sync.status()["session_upload_processed"],2)
        uploads=[v["gid"] for r in self.requests if r[1]=="/v3/contribute" for v in r[2]["records"]]
        self.assertEqual(uploads,[3,2])
        self.assertEqual([r["gid"] for r in self.preflight_requests[-1]["records"]],[1,2])
        restarted=CloudSync(lambda:self.store,self.gate,transport=self.preflight_transport,start=False)
        try:
            restarted.run_once()
            self.assertEqual(restarted.status()["upload_min_interval_days"],0)
            self.assertEqual(len(self.preflight_requests),2)
            self.interval_days=7;restarted.trigger()
            with patch.object(restarted.stopped,"wait",return_value=False):restarted.run_once()
            self.assertEqual(restarted.status()["upload_min_interval_days"],7)
            self.assertEqual(len(self.preflight_requests),3)
            self.assertEqual(restarted.status()["session_upload_processed"],0)
        finally:restarted.close()

    def test_submit_time_skip_is_rechecked_after_interval_change_without_resending_other_records(self):
        self.seed_upload_queue(3);self.public_gids=set();self.preflight_requests=[]
        def raced(endpoint,path,payload=None,token=""):
            if path=="/v3/contribute":
                self.requests.append((endpoint,path,payload,token))
                return {"version":3,"accepted":2,"pending":0,"kept":1,"upload_skips":[{"gid":1,"revision":1}],
                    "dataset_id":DATASET,"upload_min_interval_days":14}
            return self.preflight_transport(endpoint,path,payload,token)
        self.sync.transport=raced
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_skipped"],1)
        self.assertEqual(self.sync.status()["upload_confirmed"],3)
        self.interval_days=0;self.sync.capabilities_refresh_at=0;self.sync.transport=self.preflight_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        uploads=[[r["gid"] for r in request[2]["records"]] for request in self.requests if request[1]=="/v3/contribute"]
        self.assertEqual(uploads,[[1,2,3],[1]])
        self.assertEqual(self.sync.status()["upload_skipped"],0)

    def test_invalid_submit_time_skip_keeps_queue_without_acknowledging(self):
        self.seed_upload_queue(2);self.public_gids=set();self.preflight_requests=[]
        def invalid(endpoint,path,payload=None,token=""):
            if path=="/v3/contribute":return {"version":3,"accepted":1,"pending":0,"kept":1,
                "upload_skips":[{"gid":3,"revision":1}],"dataset_id":DATASET,"upload_min_interval_days":14}
            return self.preflight_transport(endpoint,path,payload,token)
        self.sync.transport=invalid
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"retrying")
        self.assertEqual(self.sync.status()["upload_remaining"],2)
        self.assertIsNone(self.read("SELECT * FROM cloud_upload_ack"))

    def test_unavailable_preflight_keeps_queue_and_legacy_service_clears_only_skip_confirmations(self):
        self.seed_upload_queue(2);self.public_gids={1};self.preflight_requests=[]
        self.sync.transport=self.preflight_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_skipped"],1)
        self.sync.capabilities_endpoint=None
        def offline(endpoint,path,payload=None,token=""):
            if path=="/health":raise ValueError("测试网络未连接")
            return self.preflight_transport(endpoint,path,payload,token)
        self.sync.transport=offline;self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"retrying");self.assertEqual(self.sync.status()["upload_skipped"],1)
        self.sync.transport=self.transport;self.requests.clear()
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        sent=[v["gid"] for r in self.requests if r[1]=="/v3/contribute" for v in r[2]["records"]]
        self.assertEqual(sent,[1]);self.assertEqual(self.sync.status()["upload_skipped"],0)

    def test_bad_preflight_response_does_not_acknowledge_and_data_change_during_check_stays_queued(self):
        self.seed_upload_queue(2);self.public_gids={1,2};self.preflight_requests=[]
        def invalid(endpoint,path,payload=None,token=""):
            result=self.preflight_transport(endpoint,path,payload,token)
            if path=="/v3/upload-check":result["records"][1]["gid"]=1
            return result
        self.sync.transport=invalid;self.sync.run_once()
        self.assertEqual(self.sync.status()["phase"],"retrying");self.assertIsNone(self.read("SELECT * FROM cloud_upload_ack"))
        def changing(endpoint,path,payload=None,token=""):
            result=self.preflight_transport(endpoint,path,payload,token)
            if path=="/v3/upload-check":self.store.save(1,110,"e-hentai.org",30)
            return result
        self.sync.transport=changing
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_remaining"],1)
        self.assertEqual(self.read("SELECT favorite_count FROM favorites WHERE gid=1")["favorite_count"],110)

    def test_fast_upload_batches_confirm_all_records_and_resume_without_duplicates(self):
        self.seed_upload_queue(1200)
        self.sync.transport=self.fast_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):
            self.sync.run_once()
            self.assertEqual(self.sync.status()["upload_confirmed"],1000)
            self.assertEqual(self.sync.status()["upload_remaining"],200)
            self.sync.run_once()
        uploads=[r for r in self.requests if r[1]=="/v3/contribute"]
        self.assertEqual(len(uploads),24)
        self.assertTrue(all(len(r[2]["records"])==50 for r in uploads))
        self.assertEqual([v["gid"] for r in uploads for v in r[2]["records"]],list(range(1,1201)))
        self.assertEqual(self.sync.status()["upload_remaining"],0)
        self.assertEqual(self.sync.status()["phase"],"waiting")

    def test_changed_rows_only_and_stale_ack_cannot_remove_newer_queue_payload(self):
        self.seed_upload_queue(1000)
        self.sync.transport=self.fast_transport
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        old=self.read("SELECT payload FROM cloud_upload_ack WHERE gid=1")["payload"]
        with patch("cloud_sync.encode_snapshot", wraps=encode_snapshot) as encode:
            self.sync._refresh_upload_snapshots()
            self.assertEqual(encode.call_count,0)
            self.store.save(1,110,"e-hentai.org",30)
            self.sync._refresh_upload_snapshots()
            self.assertEqual(encode.call_count,1)
        with self.store.connection() as db:
            db.execute("UPDATE cloud_upload_ack SET payload=? WHERE gid=1",(old,))
        queued=self.read("SELECT payload FROM cloud_upload_queue WHERE gid=1")
        self.assertEqual(json.loads(queued["payload"])["favorite_count"],110)
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_remaining"],0)

    def test_daily_limit_wait_keeps_downloading_and_partial_quota_uses_smaller_batch(self):
        self.seed_upload_queue(70)
        budget=55
        def limited(endpoint,path,payload=None,token=""):
            nonlocal budget
            if path=="/health":return {"version":3,"max_upload_batch":50}
            if path=="/v3/contribute":
                if len(payload["records"])>budget:
                    raise SyncRateLimited("测试日额度",7200,budget or None)
                budget-=len(payload["records"])
            return self.transport(endpoint,path,payload,token)
        self.sync.transport=limited
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_confirmed"],55)
        self.assertEqual(self.sync.status()["upload_remaining"],15)
        self.assertIsNotNone(self.sync.status()["upload_retry_at"])
        self.assertLessEqual(self.sync.next_due-time.monotonic(),900)
        self.requests.clear()
        self.responses=[self.page([self.item(99)])]
        self.sync.run_once()
        self.assertEqual([r[1].split('?')[0] for r in self.requests],["/v3/changes"])
        self.assertIsNotNone(self.read("SELECT gid FROM favorites WHERE gid=99"))
        budget=50
        self.sync.trigger()
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_remaining"],0)

    def test_http_rate_limit_uses_safe_headers_and_returns_remaining_capacity(self):
        from io import BytesIO
        failure=HTTPError("https://sync.example.invalid",429,"test",
            {"Retry-After":"3600","X-Upload-Limit-Scope":"day","X-Upload-Remaining":"7"},BytesIO(CODE.encode()))
        with patch("cloud_sync.build_opener") as opener:
            opener.return_value.open.side_effect=failure
            with self.assertRaises(SyncRateLimited) as raised:
                request_json(DEFAULT_ENDPOINT,"/v3/contribute",{},CODE)
        self.assertEqual(raised.exception.retry_after,3600)
        self.assertEqual(raised.exception.batch_limit,7)
        self.assertNotIn(CODE,str(raised.exception))

    def test_legacy_library_migration_preserves_confirmations_and_indexes_new_changes(self):
        import sqlite3
        from contextlib import closing
        legacy_path=Path(self.temp.name)/"legacy.sqlite3"
        snapshot={k:v for k,v in self.item().items() if k!="revision"}
        payload=encode_snapshot(snapshot)
        with closing(sqlite3.connect(legacy_path)) as db:
            db.executescript("""CREATE TABLE favorites(gid INTEGER PRIMARY KEY,favorite_count INTEGER,
                checked_at INTEGER,source TEXT,collector_id TEXT,collector_name TEXT);
                CREATE TABLE cloud_upload_snapshots(gid INTEGER PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE cloud_upload_ack(gid INTEGER PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO app_settings VALUES('cloud_protocol','3');""")
            db.execute("INSERT INTO favorites VALUES(?,?,?,?,?,?)",tuple(snapshot[k] for k in
                ("gid","favorite_count","checked_at","source","collector_id","collector_name")))
            for table in ("cloud_upload_snapshots","cloud_upload_ack"):
                db.execute(f"INSERT INTO {table} VALUES(1,?)",(payload,))
            db.execute("INSERT INTO cloud_upload_snapshots VALUES(2,?)",(encode_snapshot({**snapshot,"gid":2}),))
            db.commit()
        store=FavoriteStore(legacy_path)
        sync=CloudSync(lambda:store,self.gate,transport=self.transport,start=False)
        try:
            # The matching rating row is absent, so seed the legacy matching row before refresh.
            with store.connection() as db:
                db.execute("INSERT INTO rating_counts VALUES(1,?,?,?)",(snapshot["rating_count"],snapshot["checked_at"],snapshot["source"]))
            sync._refresh_upload_snapshots()
            self.assertEqual(sync._count_upload_queue(),0)
            with store.connection() as db:
                self.assertEqual(db.execute("SELECT payload FROM cloud_upload_ack").fetchone()[0],payload)
                db.execute("UPDATE favorites SET favorite_count=110 WHERE gid=1")
            sync._refresh_upload_snapshots()
            self.assertEqual(sync._count_upload_queue(),1)
        finally:sync.close()

    def test_exact_full_upload_round_finishes_without_empty_continuation(self):
        self.seed_upload_queue(100)
        with patch.object(self.sync.stopped,"wait",return_value=False):
            self.sync.run_once()
        self.assertEqual(len([request for request in self.requests if request[1] == "/v3/contribute"]),20)
        self.assertGreater(self.sync.next_due-time.monotonic(),890)
        self.assertEqual(self.sync.status()["phase"],"waiting")
        self.assertEqual(self.sync.status()["upload_remaining"],0)

    def test_large_upload_queue_advances_without_resending_or_false_completion(self):
        self.seed_upload_queue(230)
        observed=[]
        original=self.sync.transport
        def observe(endpoint,path,payload=None,token=""):
            if path == "/v3/contribute":
                observed.append(self.sync.status())
            return original(endpoint,path,payload,token)
        self.sync.transport=observe
        with patch.object(self.sync.stopped,"wait",return_value=False):
            for expected in (130,30):
                self.sync.run_once()
                state=self.sync.status()
                self.assertEqual(state["phase"],"continuing")
                self.assertEqual(state["upload_remaining"],expected)
                self.assertNotIn("同步完成",state["message"])
                self.assertLess(self.sync.next_due-time.monotonic(),6)
            self.sync.run_once()
            final=self.sync.status()
            self.assertEqual(final["phase"],"waiting")
            self.assertEqual(final["upload_remaining"],0)
            self.assertEqual(final["session_upload_processed"],230)
            self.assertIn("同步完成",final["message"])
            self.assertGreater(self.sync.next_due-time.monotonic(),890)
            self.sync.run_once()
        submitted=[item["gid"] for request in self.requests if request[1] == "/v3/contribute" for item in request[2]["records"]]
        self.assertEqual(submitted,list(range(1,231)))
        self.assertEqual([state["session_upload_processed"] for state in observed],list(range(0,230,5)))
        self.assertTrue(all(state["phase"]=="uploading" for state in observed))
        self.assertTrue(all("正在上传" in state["message"] for state in observed))

    def test_restart_preserves_acknowledged_batches_and_continues_remaining_queue(self):
        self.seed_upload_queue(150)
        with patch.object(self.sync.stopped,"wait",return_value=False):
            self.sync.run_once()
        self.assertEqual(self.sync.status()["upload_remaining"],50)
        restarted=CloudSync(lambda:self.store,self.gate,transport=self.transport,start=False)
        try:
            self.assertEqual(restarted.status()["source_id"],OTHER)
            restarted.configure(self.config(upload_enabled=True,consent=True))
            with patch.object(restarted.stopped,"wait",return_value=False):
                restarted.run_once()
            self.assertEqual(restarted.status()["phase"],"waiting")
            self.assertEqual(restarted.status()["upload_remaining"],0)
            submitted=[item["gid"] for request in self.requests if request[1] == "/v3/contribute" for item in request[2]["records"]]
            self.assertEqual(submitted,list(range(1,151)))
        finally:
            restarted.close()

    def event(self, snapshot, revision, action="snapshot", gid=1):
        if snapshot is not None:
            snapshot={k:v for k,v in snapshot.items() if k!="revision"}
        return {"gid":snapshot["gid"] if snapshot else gid,"revision":revision,"action":action,"snapshot":snapshot}

    def apply_events(self, events, previous=0):
        last=events[-1]["revision"]
        page={"format":"excatalog-cloud-sync","version":3,"dataset_id":DATASET,"cursor":last,"until":last,
              "head":last,"has_more":False,"records":events}
        self.sync._apply_page(page,previous,None,DATASET)

    def test_restore_older_cloud_snapshot_and_retract(self):
        good=self.item(count=100,checked=self.now-20)
        bad=self.item(count=1000,checked=self.now)
        self.apply_events([self.event(good,1),self.event(bad,2),self.event(good,3,"correction")])
        self.assertEqual(self.read("SELECT favorite_count FROM favorites")["favorite_count"],100)
        self.apply_events([self.event(None,4,"correction")],3)
        self.assertIsNone(self.read("SELECT * FROM favorites"))

    def test_retract_restores_local_baseline_and_preserves_personal_state(self):
        self.store.save(1,80,"e-hentai.org",10)
        with self.store.connection() as db:
            db.execute("UPDATE favorites SET checked_at=? WHERE gid=1",(self.now-50,))
            db.execute("UPDATE rating_counts SET checked_at=? WHERE gid=1",(self.now-50,))
            db.execute("DELETE FROM cloud_self_snapshots")
            db.execute("INSERT INTO record_status(gid,state) VALUES(1,'watched')")
        self.apply_events([self.event(self.item(count=999),1),self.event(None,2,"correction")])
        self.assertEqual(self.read("SELECT favorite_count FROM favorites")["favorite_count"],80)
        self.assertEqual(self.read("SELECT rating_count FROM rating_counts")["rating_count"],10)
        self.assertEqual(self.read("SELECT state FROM record_status")["state"],"watched")

    def test_correction_keeps_newer_own_capture_and_upload_outbox(self):
        self.apply_events([self.event(self.item(count=999),1)])
        self.store.save(1,120,"e-hentai.org",30)
        queued=self.read("SELECT payload FROM cloud_self_snapshots")["payload"]
        self.apply_events([self.event(self.item(count=100,checked=self.now-20),2,"correction"),self.event(None,3,"correction")],1)
        self.assertEqual(self.read("SELECT favorite_count FROM favorites")["favorite_count"],120)
        self.assertEqual(self.read("SELECT payload FROM cloud_self_snapshots")["payload"],queued)

    def test_invalid_correction_is_atomic_and_repeated_gid_history_is_valid(self):
        bad=self.event(None,2,"snapshot")
        with self.assertRaises(ValueError):self.apply_events([self.event(self.item(),1),bad])
        self.assertIsNone(self.read("SELECT * FROM favorites"))
        self.apply_events([self.event(self.item(),1),self.event(self.item(count=110),2)])
        self.assertEqual(self.read("SELECT favorite_count FROM favorites")["favorite_count"],110)

    def test_legacy_v1_cloud_value_is_corrected_by_replaying_migration_history(self):
        bad=self.item(count=999)
        with self.store.connection() as db:self.sync._write_snapshot(db,1,{k:v for k,v in bad.items() if k!="revision"})
        self.apply_events([self.event(bad,1),self.event(None,2,"correction")])
        self.assertIsNone(self.read("SELECT * FROM favorites"))

    def test_blocked_upload_updates_source_state_but_downloads_continue(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(upload_enabled=True,consent=True))
        original=self.sync.transport
        def blocked(endpoint,path,payload=None,token=""):
            if path=="/v3/contribute":raise UploadBlocked("测试来源已封禁")
            return original(endpoint,path,payload,token)
        self.sync.transport=blocked
        self.responses=[self.page([self.item(99)])]
        self.sync.run_once()
        self.assertEqual(self.sync.status()["source_status"],"blocked")
        self.assertIsNotNone(self.read("SELECT gid FROM favorites WHERE gid=99"))
        self.assertIsNone(self.read("SELECT * FROM cloud_upload_ack"))

    def test_server_lost_identity_reregisters_same_credential(self):
        self.store.save(1,100,"e-hentai.org",20)
        self.sync.configure(self.config(upload_enabled=True,consent=True))
        original=self.sync.transport
        def missing(endpoint,path,payload=None,token=""):
            if path=="/v3/contribute":raise UploadIdentityMissing("测试身份缺失")
            return original(endpoint,path,payload,token)
        self.sync.transport=missing
        self.sync.run_once()
        token=self.read("SELECT value FROM app_settings WHERE key='cloud_upload_token'")["value"]
        self.assertFalse(self.sync.status()["source_id"])
        self.sync.transport=original
        with patch.object(self.sync.stopped,"wait",return_value=False):self.sync.run_once()
        self.assertEqual(self.read("SELECT value FROM app_settings WHERE key='cloud_upload_token'")["value"],token)
        self.assertIsNotNone(self.read("SELECT * FROM cloud_upload_ack"))


if __name__ == "__main__":unittest.main()
