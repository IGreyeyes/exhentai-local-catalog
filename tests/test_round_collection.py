from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import sqlite3
import tempfile
import threading
import unittest

from app import Catalog, Server
from favorites import Collector, GalleryUnavailable


class RoundCollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'catalog.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english');
            """)
            for gid in range(1,24):
                db.execute("INSERT INTO gallery(gid,token,title,title_jpn,category,posted,filecount,rating) VALUES (?,'abcdef0123','Test book','','Non-H',1760000000,10,?)",(gid,str(gid/10)))
                db.execute('INSERT INTO gid_tid VALUES(?,1)',(gid,))
            db.commit()
        self.catalog = Catalog(self.path)
        self.calls = []
        self.collector = Collector(self.catalog,self.fetch)
        self.query = {'tag':['language:english']}
        self.configure()

    def tearDown(self):
        self.collector.close()
        self.directory.cleanup()

    def fetch(self,settings,gid,token):
        self.calls.append(gid)
        return gid

    def configure(self,**options):
        self.collector.configure({'host':'e-hentai.org','collection_mode':'rounds','batch_size':10,'skip_existing':True,'rating_priority':True,**options})
        # All source requests are mocked; production intervals remain >= 3 seconds.
        self.collector.verified = True
        self.collector.settings['interval'] = 0.00001

    def finish(self):
        self.collector.thread.join(timeout=5)
        self.assertFalse(self.collector.thread.is_alive())

    def start(self):
        preview = self.collector.preview(self.query)
        self.collector.start(self.query,preview['preview_id'])
        self.finish()

    def next_round(self):
        preview = self.collector.preview({'tag':['nonexistent']},'next_round')
        self.collector.next_round(preview['preview_id'])
        self.finish()
        return preview

    def test_preview_counts_full_plan_without_requests_and_accepts_custom_sizes(self):
        preview = self.collector.preview({**self.query,'page':['999'],'limit':['1']})
        self.assertEqual((preview['total'],preview['queue_total'],preview['pending'],preview['deferred'],preview['round_count']),(23,23,10,13,3))
        self.assertIsNone(self.catalog.favorites.latest())
        self.assertEqual(self.calls,[])
        for size in (1,50,1000,5000,100000):
            self.configure(batch_size=size)
            preview = self.collector.preview(self.query)
            self.assertEqual(preview['round_count'],(23+size-1)//size)
        for value in (0,100001,True,10.5,'1e3','bad'):
            with self.assertRaises(ValueError):self.configure(batch_size=value)

    def test_each_round_stops_and_last_round_is_shorter_without_duplicate_requests(self):
        self.start()
        job = self.collector.store.latest()
        job_id = job['id']
        self.assertEqual((job['state'],job['succeeded'],job['pending']),('awaiting_next',10,13))
        self.assertEqual(job['round_progress']['completed'],1)
        self.assertEqual(self.calls,list(range(23,13,-1)))
        with self.assertRaises(ValueError):self.collector.next_round()
        with self.assertRaises(ValueError):self.collector.preview(self.query)
        preview = self.next_round()
        self.assertEqual((preview['round_number'],preview['pending'],preview['deferred']),(2,10,3))
        self.assertEqual((self.collector.store.latest()['state'],len(self.calls)),('awaiting_next',20))
        preview = self.next_round()
        self.assertEqual((preview['round_number'],preview['pending']),(3,3))
        final = self.collector.store.latest()
        self.assertEqual((final['id'],final['state'],final['succeeded'],final['pending']),(job_id,'completed',23,0))
        self.assertEqual(final['round_progress']['completed'],3)
        self.assertEqual(self.calls,list(range(23,0,-1)))
        with self.assertRaises(ValueError):self.collector.preview({},'next_round')

    def test_restart_after_two_rounds_keeps_queue_and_verifies_without_search(self):
        self.start();self.next_round()
        self.collector.close()
        self.collector = Collector(self.catalog,self.fetch)
        state = self.collector.status()
        self.assertFalse(state['configured'])
        self.assertEqual((state['batch_size'],state['collection_mode'],state['skip_existing']),(10,'rounds',True))
        self.assertEqual((state['job']['current_round'],state['job']['pending'],state['job']['round_progress']['completed']),(2,3,2))
        # New settings must not change the frozen plan, including its per-round size.
        self.configure(batch_size=5000,collection_mode='full')
        self.collector.verified = False
        result = self.collector.verify(None)
        self.assertTrue(result['verified'])
        self.assertEqual(result['gid'],3)
        preview = self.next_round()
        self.assertEqual((preview['batch_size'],preview['total'],preview['pending'],preview['skipped']),(10,3,2,1))
        final = self.collector.store.latest()
        self.assertEqual((final['state'],final['succeeded'],final['cached']),('completed',22,1))
        self.assertEqual(self.calls,list(range(23,0,-1)))

    def test_mid_round_pause_and_restart_resumes_only_that_round(self):
        entered,release = threading.Event(),threading.Event()
        def blocked(settings,gid,token):
            entered.set();release.wait(timeout=3)
            return self.fetch(settings,gid,token)
        self.collector.fetcher = blocked
        preview = self.collector.preview(self.query)
        try:
            self.collector.start(self.query,preview['preview_id'])
            self.assertTrue(entered.wait(timeout=2))
            self.collector.pause();release.set();self.finish()
        finally:release.set()
        self.assertEqual(self.collector.store.latest()['succeeded'],1)
        self.collector.close()
        # Simulate an unclean shutdown's running marker on the saved queue.
        with self.collector.store.connection() as db:db.execute("UPDATE jobs SET state='running'")
        self.collector = Collector(self.catalog,self.fetch)
        self.assertEqual(self.collector.store.latest()['state'],'paused')
        self.configure(batch_size=1000)
        preview = self.collector.preview({},'resume')
        self.assertEqual((preview['batch_size'],preview['pending'],preview['deferred']),(10,9,13))
        self.collector.resume(preview_id=preview['preview_id']);self.finish()
        self.assertEqual((self.collector.store.latest()['state'],len(self.calls)),('awaiting_next',10))

    def test_cached_works_are_excluded_before_splitting_and_zero_work_has_no_rounds(self):
        for gid in range(19,24):self.collector.store.save(gid,0,'e-hentai.org')
        preview = self.collector.preview(self.query)
        self.assertEqual((preview['queue_total'],preview['round_count'],preview['skipped']),(18,2,5))
        self.start();self.next_round()
        self.assertEqual(len(self.calls),18)
        preview = self.collector.preview(self.query)
        self.assertEqual((preview['pending'],preview['round_count']),(0,0))
        self.collector.verified = False
        self.collector.start(self.query,preview['preview_id'])
        self.assertEqual(self.collector.store.latest()['state'],'completed')
        self.assertEqual(len(self.calls),18)

    def test_failures_are_separate_and_previous_round_failures_can_be_retried(self):
        completed = []
        self.collector.on_completed = lambda job_id:completed.append(job_id)
        def unavailable(settings,gid,token):
            if gid==23:raise GalleryUnavailable('test unavailable')
            return self.fetch(settings,gid,token)
        self.collector.fetcher = unavailable
        self.start()
        self.assertEqual((self.collector.store.latest()['state'],self.collector.store.latest()['failed']),('awaiting_next',1))
        failed=self.catalog.recorded({'collection':['failed']})
        self.assertEqual((failed['total'],failed['items'][0]['gid']),(1,23))
        self.assertEqual(failed['items'][0]['source_url'],'https://e-hentai.org/g/23/abcdef0123/')
        self.assertEqual(self.collector.store.latest()['recent_errors'][0]['source_url'],failed['items'][0]['source_url'])
        self.assertEqual(completed,[])
        self.next_round()
        self.collector.fetcher = self.fetch
        preview = self.collector.preview({},'retry')
        self.assertEqual(preview['pending'],1)
        self.collector.resume(retry_failed=True,preview_id=preview['preview_id']);self.finish()
        self.assertEqual(self.catalog.recorded({'collection':['failed']})['total'],0)
        job = self.collector.store.latest()
        self.assertEqual((job['state'],job['current_round'],job['failed'],job['pending']),('awaiting_next',2,0,3))
        self.assertEqual(completed,[])
        self.next_round()
        self.assertEqual(completed,[job['id']])
        self.assertEqual(len(set(self.calls)),23)

    def test_catalog_changes_do_not_replace_saved_future_queue_and_cancel_ends_plan(self):
        self.start()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DELETE FROM gid_tid')
            db.execute("UPDATE gallery SET rating='99',token='0123abcdef'")
            db.commit()
        preview = self.next_round()
        self.assertEqual(preview['pending'],10)
        self.assertEqual(self.calls,list(range(23,3,-1)))
        self.collector.pause(cancel=True)
        self.assertEqual((self.collector.store.latest()['state'],self.collector.store.latest()['pending']),('cancelled',3))
        with self.assertRaises(ValueError):self.collector.preview({},'next_round')

    def test_next_round_api_requires_preview_and_original_host(self):
        self.start()
        server = Server(('127.0.0.1',0),self.catalog)
        server.collector.fetcher = self.fetch
        server.collector.configure({'host':'e-hentai.org','collection_mode':'rounds','batch_size':1000,'skip_existing':True})
        server.collector.verified = True
        server.collector.settings['interval'] = 0.00001
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        def post(route,payload):
            req = Request(root+'/api/collector/'+route,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token,'Origin':root})
            with urlopen(req) as response:return json.load(response)
        try:
            with self.assertRaises(HTTPError) as failure:post('next_round',{})
            self.assertEqual(failure.exception.code,400);failure.exception.close()
            before = len(self.calls)
            preview = post('preview',{'operation':'next_round','query':'tag=nonexistent'})
            self.assertEqual(preview['query'],self.collector.store.latest()['query'])
            self.assertEqual(len(self.calls),before)
            post('next_round',{'preview_id':preview['preview_id']})
            server.collector.thread.join(timeout=5)
            self.assertEqual((server.collector.store.latest()['current_round'],len(self.calls)),(2,20))
            post('settings',{'host':'exhentai.org','ipb_member_id':'1','ipb_pass_hash':'fake','igneous':'fake'})
            with self.assertRaises(HTTPError) as failure:post('preview',{'operation':'next_round'})
            self.assertEqual(failure.exception.code,400);failure.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
