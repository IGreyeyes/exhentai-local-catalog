"""Scheduled breaks use isolated libraries and a loopback source on either OS."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import json
import sqlite3
import tempfile
import threading
import time
import unittest

from app import Catalog, Server
from favorites import Collector, CollectionPaused, RetryableFetch, parse_gallery_counts


class AutoPauseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)/"catalog.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english');
            """)
            for gid in range(1, 7):
                db.execute("INSERT INTO gallery VALUES(?,'abcdef0123','Test','','Non-H',1,10,'4.0',0,0,0)", (gid,))
                db.execute("INSERT INTO gid_tid VALUES(?,1)", (gid,))
            db.commit()
        self.catalog = Catalog(path)
        self.calls = []
        self.collector = Collector(self.catalog, self.fetch)
        self.addCleanup(lambda: self.collector.close())
        self.query = {"tag":["language:english"]}
        self.configure()

    def fetch(self, settings, gid, token):
        self.calls.append((gid, time.monotonic()))
        return gid*10

    def configure(self, **options):
        result = self.collector.configure({"host":"e-hentai.org", "collection_mode":"full",
            "auto_pause_enabled":True, "auto_pause_work_minutes":60, "auto_pause_rest_minutes":10, **options})
        self.collector.verified = True
        self.collector.settings["interval"] = 0.00001
        return result

    def expire(self):
        with self.collector.lock:
            self.collector.schedule["deadline"] = time.monotonic()-1

    def wait_phase(self, phase):
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            state = self.collector.status()
            if state["schedule"] and state["schedule"]["phase"]==phase:
                return state
            time.sleep(0.01)
        self.fail("Scheduled phase was not reached: "+phase)

    def finish(self):
        self.collector.thread.join(timeout=10)
        self.assertFalse(self.collector.thread.is_alive())

    def start(self):
        preview = self.collector.preview(self.query)
        self.collector.start(self.query, preview["preview_id"])

    def rest_after_first(self, rest_seconds=0.4):
        original = self.fetch
        def fetch(settings, gid, token):
            result = original(settings, gid, token)
            if len(self.calls)==1:
                self.expire()
            return result
        self.collector.fetcher = fetch
        self.collector.settings["auto_pause_rest_minutes"] = rest_seconds/60
        self.start()
        return self.wait_phase("resting")

    def test_defaults_validation_and_saved_settings_survive_restart(self):
        fresh = Collector(self.catalog, self.fetch)
        self.assertTrue(fresh.status()["auto_pause_enabled"])
        self.assertFalse(fresh.status()["configured"])
        self.configure(auto_pause_enabled=False, auto_pause_work_minutes="12.5", auto_pause_rest_minutes="0.1")
        for field in ("auto_pause_work_minutes", "auto_pause_rest_minutes"):
            for value in (True, None, [], 0, 0.09, 1441, "nan", "inf", "bad"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.collector.configure({"host":"e-hentai.org", field:value})
        with self.assertRaises(ValueError):
            self.collector.configure({"host":"e-hentai.org", "auto_pause_enabled":"true"})
        restarted = Collector(self.catalog, self.fetch)
        state = restarted.configure({"host":"e-hentai.org"})
        self.assertEqual((state["auto_pause_enabled"], state["auto_pause_work_minutes"], state["auto_pause_rest_minutes"]), (False, 12.5, 0.1))
        with restarted.store.connection() as db:
            db.execute("DELETE FROM app_settings WHERE key='collector_auto_pause'")
        self.assertFalse(Collector(self.catalog, self.fetch).status()["auto_pause_enabled"])

    def test_repeated_breaks_send_no_requests_and_keep_queue_order(self):
        self.collector.settings["auto_pause_rest_minutes"] = 0.002
        def fetch(settings, gid, token):
            result = self.fetch(settings, gid, token)
            self.expire()
            return result
        self.collector.fetcher = fetch
        self.start()
        self.finish()
        self.assertEqual([gid for gid, _ in self.calls], [6,5,4,3,2,1])
        self.assertTrue(all(b[1]-a[1]>=0.12 for a,b in zip(self.calls,self.calls[1:])))
        state = self.collector.status()
        self.assertEqual((state["job"]["state"], state["job"]["succeeded"]), ("completed", 6))
        self.assertIsNone(state["schedule"])

    def test_in_flight_request_finishes_and_saves_before_rest(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(settings,gid,token):
            entered.set()
            release.wait(timeout=10)
            return self.fetch(settings,gid,token)
        self.collector.fetcher = fetch
        self.collector.settings["auto_pause_rest_minutes"] = 1/60
        try:
            self.start()
            self.assertTrue(entered.wait(timeout=5))
            self.expire()
            state = self.collector.status()
            self.assertEqual(state["schedule"]["phase"], "collecting")
            self.assertEqual(state["job"]["succeeded"], 0)
            release.set()
            state = self.wait_phase("resting")
            self.assertEqual((state["job"]["succeeded"],len(self.calls)), (1,1))
            self.collector.pause()
            self.finish()
        finally:
            release.set()

    def test_manual_pause_cancel_and_close_interrupt_rest_without_waking(self):
        for action in ("pause", "cancel", "close"):
            with self.subTest(action=action):
                self.calls.clear()
                self.configure(skip_existing=False)
                # Re-request cached works when creating each independent fixture task.
                with self.collector.store.connection() as db:
                    db.execute("DELETE FROM favorites")
                state = self.rest_after_first(1)
                self.assertEqual((state["job"]["succeeded"], len(self.calls)), (1, 1))
                self.assertGreater(state["schedule"]["remaining_seconds"], 0)
                if action=="close":
                    self.collector.close()
                else:
                    self.collector.pause(cancel=action=="cancel")
                self.finish()
                time.sleep(0.05)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.collector.status()["job"]["state"], "cancelled" if action=="cancel" else "paused")
                self.assertIsNone(self.collector.status()["schedule"])
                if action=="pause":
                    self.collector.pause(cancel=True)

    def test_restart_during_rest_requires_manual_resume_and_resets_work_period(self):
        state = self.rest_after_first(1)
        job_id = state["job"]["id"]
        self.collector.close()
        # Simulate an unclean stop while the scheduled break's job was running.
        with self.collector.store.connection() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
        self.collector = Collector(self.catalog, self.fetch)
        state = self.collector.status()
        self.assertEqual(state["job"]["state"], "paused")
        self.assertTrue(state["auto_pause_enabled"])
        self.assertFalse(state["configured"])
        self.assertFalse(state["busy"])
        self.assertIsNone(state["schedule"])
        self.configure()
        preview = self.collector.preview({}, "resume")
        self.collector.resume(preview_id=preview["preview_id"])
        self.finish()
        self.assertEqual([gid for gid,_ in self.calls], [6,5,4,3,2,1])
        self.assertEqual(self.collector.status()["job"]["id"], job_id)

    def test_limit_and_access_errors_stop_automatic_continuation(self):
        def fetch(settings, gid, token):
            self.calls.append((gid,time.monotonic()))
            raise CollectionPaused("Test source limit", 60)
        self.collector.fetcher = fetch
        self.start()
        self.finish()
        state = self.collector.status()
        self.assertEqual(state["job"]["state"], "paused")
        self.assertFalse(state["verified"])
        self.assertIsNone(state["schedule"])
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(state["cooldown_until"], time.time())

    def test_request_interval_can_trigger_rest_before_the_next_request(self):
        self.collector.settings.update(interval=0.4, auto_pause_work_minutes=0.001, auto_pause_rest_minutes=0.003)
        self.start()
        self.wait_phase("resting")
        self.assertEqual(len(self.calls),1)
        self.collector.pause()
        self.finish()

    def test_backoff_can_rest_but_never_retries_before_its_deadline(self):
        def fetch(settings, gid, token):
            self.calls.append((gid,time.monotonic()))
            self.expire()
            raise RetryableFetch("Test temporary network error")
        self.collector.fetcher = fetch
        self.collector.settings["auto_pause_rest_minutes"] = 0.002
        self.start()
        self.wait_phase("resting")
        time.sleep(0.2)
        self.assertEqual(len(self.calls),1)
        self.assertGreaterEqual(self.wait_phase("collecting")["schedule"]["completed_rests"],1)
        self.collector.pause()
        self.finish()

    def test_round_boundary_stays_stopped_and_disabled_setting_never_rests(self):
        self.configure(collection_mode="rounds", batch_size=1)
        self.collector.fetcher = lambda settings,gid,token: (self.expire(),self.fetch(settings,gid,token))[1]
        self.start()
        self.finish()
        self.assertEqual(self.collector.status()["job"]["state"],"awaiting_next")
        self.assertIsNone(self.collector.status()["schedule"])
        self.collector.pause(cancel=True)
        self.configure(auto_pause_enabled=False)
        self.collector.fetcher = self.fetch
        self.start()
        self.finish()
        self.assertEqual(self.collector.status()["job"]["state"],"completed")
        self.assertIsNone(self.collector.status()["schedule"])

    def test_preview_includes_estimated_rest_and_changes_require_new_confirmation(self):
        self.configure(auto_pause_work_minutes=0.1, auto_pause_rest_minutes=0.1, interval=3)
        self.collector.settings["interval"] = 3
        preview = self.collector.preview(self.query)
        self.assertEqual((preview["estimated_rest_seconds"], preview["estimated_seconds"]), (24,54))
        self.configure(auto_pause_work_minutes=0.2)
        with self.assertRaises(ValueError):
            self.collector.start(self.query, preview["preview_id"])
        self.assertEqual(self.calls, [])

    def test_loopback_source_and_http_settings_search_and_saved_queue(self):
        received = []
        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                gid = int(self.path.split('/')[2])
                received.append(gid)
                body = b'<div id="favcount">42 times</div><span id="rating_count">7</span>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self,*args): pass
        source = ThreadingHTTPServer(("127.0.0.1",0), Source)
        server = Server(("127.0.0.1",0), self.catalog)
        server.collector.close()
        server.collector = self.collector
        opener = build_opener(ProxyHandler({}))
        def fetch(settings,gid,token):
            with opener.open(f"http://127.0.0.1:{source.server_port}/g/{gid}/{token}/",timeout=5) as response:
                count = parse_gallery_counts(response.read().decode(),gid)
            if len(received)==1: self.expire()
            return count
        self.collector.fetcher = fetch
        workers = [threading.Thread(target=item.serve_forever,daemon=True) for item in (source,server)]
        for worker in workers: worker.start()
        root = f"http://127.0.0.1:{server.server_port}"
        def get(path):
            with opener.open(root+path,timeout=10) as response: return json.load(response)
        token = get('/api/status')['action_token']
        def post(path,payload):
            request = Request(root+'/api/collector/'+path,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json","X-Catalog-Token":token})
            with opener.open(request,timeout=10) as response: return json.load(response)
        try:
            saved = post('settings', {"host":"e-hentai.org", "auto_pause_enabled":True,"auto_pause_work_minutes":0.1,"auto_pause_rest_minutes":0.1})
            self.assertEqual(saved['auto_pause_rest_minutes'],0.1)
            with self.assertRaises(HTTPError) as invalid:
                post('settings',{"host":"e-hentai.org","auto_pause_rest_minutes":0})
            invalid.exception.close()
            self.assertEqual(get('/api/search?tag=language%3Aenglish')['total'],6)
            self.collector.verified = True
            self.collector.settings.update(interval=0.00001,auto_pause_rest_minutes=0.003)
            preview = post('preview',{"query":"tag=language%3Aenglish"})
            post('start',{"query":"tag=language%3Aenglish","preview_id":preview['preview_id']})
            self.wait_phase('resting')
            self.finish()
            self.assertEqual(received,[6,5,4,3,2,1])
            self.assertEqual(get('/api/collector')['job']['succeeded'],6)
            with self.collector.store.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM rating_counts WHERE rating_count=7').fetchone()[0],6)
        finally:
            for item in (server,source): item.shutdown(); item.server_close()
            for worker in workers: worker.join(timeout=10)


if __name__ == '__main__':
    unittest.main()
