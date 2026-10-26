"""Full-library rating collection uses only disposable data and a simulated source."""
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import sqlite3
import tempfile
import threading
import unittest

from app import Catalog, Server
from favorites import Collector


class RatingLibraryCollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'catalog.sqlite3'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english'),(2,'other:blocked');
                INSERT INTO gid_tid VALUES(1,1),(2,1),(10,2);
            """)
            ratings = ['4.0','5.0','4.5','5.0',None,'3.0','5.0','5.0','5.0','5.0','0','4.9']
            for gid, rating in enumerate(ratings, 1):
                db.execute("INSERT INTO gallery VALUES(?,'abcdef0123',?,'',?, ?,24,?,?,?,?)",
                           (gid, f'Library sample {gid}', 'Manga' if gid % 2 else 'Non-H', gid, rating,
                            int(gid == 7), int(gid == 8), int(gid == 9)))
        self.catalog = Catalog(self.path)
        self.catalog.favorites.set_tag_blacklist(['other:blocked'])
        self.catalog.favorites.save(3, 0, 'e-hentai.org')
        self.catalog.favorites.save(6, 99, 'e-hentai.org')
        self.catalog.favorites.set_reading_state([3], 'planned')
        with self.catalog.favorites.connection() as db:
            db.execute('UPDATE favorites SET checked_at=1 WHERE gid=6')
        self.calls = []
        self.collector = Collector(self.catalog, self.fetch)
        self.configure()

    def tearDown(self):
        self.collector.close()
        self.directory.cleanup()

    def fetch(self, settings, gid, token):
        self.calls.append(gid)
        return gid * 10

    def configure(self, **values):
        self.collector.configure({'host':'e-hentai.org','collection_mode':'rating_full',
                                  'skip_existing':True,'rating_priority':False,'batch_size':1,**values})
        self.collector.settings['interval'] = 0.00001

    def finish(self):
        self.collector.thread.join(timeout=5)
        self.assertFalse(self.collector.thread.is_alive())

    def test_preview_ignores_search_and_uses_saved_blacklist_without_requests(self):
        query = {'tag':['nonexistent:tag'],'title':['no matching title'],'category':['Manga'],
                 'include_inactive':['1'],'use_blacklist':['0'],'page':['99'],'limit':['1']}
        preview = self.collector.preview(query)
        self.assertEqual((preview['total'],preview['pending'],preview['skipped'],preview['deferred']), (8,6,2,0))
        self.assertEqual(preview['collection_mode'], 'rating_full')
        self.assertTrue(preview['rating_priority'])
        self.assertEqual(preview['query']['scope'], ['library'])
        self.assertEqual(preview['query']['exclude_tag'], ['other:blocked'])
        self.assertEqual(preview['query']['tag'], [])
        self.assertEqual(preview['query']['title'], [''])
        self.assertEqual(preview['query']['category'], [])
        self.assertEqual(preview['query']['include_inactive'], ['0'])
        self.assertIsNone(self.collector.preview_data['candidates'])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.catalog.favorites.latest())
        self.configure(collection_mode='full')
        with self.assertRaises(ValueError): self.collector.preview({})
        self.assertEqual(self.collector.preview({'tag':['language:english']})['total'], 2)

    def test_worker_keeps_global_score_order_with_ties_unrated_and_previous_failures(self):
        with self.catalog.favorites.connection() as db:
            db.execute("INSERT INTO jobs(id,query_json,host,state,total,created_at,updated_at) VALUES(1,?,'e-hentai.org','completed_with_errors',1,1,1)", (json.dumps({'tag':['language:english']}),))
            db.execute("INSERT INTO tasks(job_id,gid,token,state,attempts) VALUES(1,12,'abcdef0123','failed',3)")
        self.collector.verified = True
        preview = self.collector.preview({})
        self.collector.start({}, preview['preview_id'])
        self.finish()
        self.assertEqual(self.calls, [4,2,12,1,11,5])
        job = self.collector.status()['job']
        self.assertEqual((job['state'],job['total'],job['succeeded'],job['cached'],job['pending']), ('completed',8,6,2,0))
        self.assertEqual(job['collection_mode'], 'rating_full')
        self.assertEqual(job['query']['scope'], ['library'])
        saved = self.catalog.recorded({'q':['3']})['items'][0]
        self.assertEqual((saved['favorite_count'],saved['reading_state']), (0,'planned'))

    def test_cache_period_and_zero_request_plan_keep_existing_data(self):
        self.configure(skip_existing=False)
        preview = self.collector.preview({})
        self.assertEqual((preview['pending'],preview['skipped'],preview['refresh_count']), (7,1,1))
        for gid in (1,2,4,5,6,11,12): self.catalog.favorites.save(gid, 0, 'e-hentai.org')
        preview = self.collector.preview({})
        self.assertEqual((preview['pending'],preview['skipped']), (0,8))
        self.assertFalse(self.collector.verified)
        self.collector.start({}, preview['preview_id'])
        self.assertEqual(self.collector.status()['job']['state'], 'completed')
        self.assertEqual(self.calls, [])

    def test_verify_without_search_samples_cache_without_overwriting_it(self):
        with patch('favorites.secrets.randbelow', return_value=0):
            result = self.collector.verify({})
        self.assertTrue(result['verified'])
        self.assertTrue(result['verification_only'])
        self.assertEqual(result['gid'], 6)
        with self.catalog.favorites.connection() as db:
            row = db.execute('SELECT favorite_count,checked_at FROM favorites WHERE gid=6').fetchone()
        self.assertEqual(tuple(row), (99,1))

    def test_restart_resumes_frozen_queue_despite_new_scores_filters_and_blacklist(self):
        self.collector.verified = True
        with patch.object(self.collector, '_launch'):
            preview = self.collector.preview({})
            self.collector.start({}, preview['preview_id'])
        job_id = self.collector.status()['job']['id']
        self.collector.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE gallery SET rating='5.0' WHERE gid=5")
            db.execute("INSERT INTO gallery VALUES(13,'abcdef0123','New sample','','Manga',13,24,'5.0',0,0,0)")
        self.catalog.favorites.set_tag_blacklist(['language:english'])
        self.collector = Collector(self.catalog, self.fetch)
        restored = self.collector.status()
        self.assertEqual((restored['collection_mode'],restored['job']['state']), ('rating_full','paused'))
        self.assertFalse(restored['configured'])
        self.configure(collection_mode='batch')
        with patch('favorites.secrets.randbelow', return_value=0): self.collector.verify(None)
        preview = self.collector.preview({}, 'resume')
        self.assertEqual((preview['collection_mode'],preview['pending']), ('rating_full',6))
        self.collector.resume(preview_id=preview['preview_id'])
        self.finish()
        self.assertEqual(self.calls[1:], [4,2,12,1,11,5])
        self.assertEqual(self.collector.status()['job']['id'], job_id)

    def test_confirmation_verification_and_scope_change_guards_remain_required(self):
        with self.assertRaises(ValueError): self.collector.start({})
        preview = self.collector.preview({})
        with self.assertRaises(ValueError): self.collector.start({}, preview['preview_id'])
        self.assertEqual(self.calls, [])
        self.collector.verified = True
        self.catalog.favorites.set_tag_blacklist(['other:blocked','language:english'])
        with self.assertRaisesRegex(ValueError, '范围、缓存或队列已变化'):
            self.collector.start({}, preview['preview_id'])
        self.assertIsNone(self.catalog.favorites.latest())

    def test_http_endpoints_support_empty_query_without_bypassing_confirmation(self):
        server = Server(('127.0.0.1',0), self.catalog)
        server.collector.fetcher = self.fetch
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(route, payload):
            request = Request(base+'/api/collector/'+route, data=json.dumps(payload).encode(),
                              headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token})
            with urlopen(request) as response: return json.load(response)
        try:
            result = post('settings', {'host':'e-hentai.org','collection_mode':'rating_full','rating_priority':False,'skip_existing':True})
            self.assertTrue(result['rating_priority'])
            server.collector.settings['interval'] = 0.00001
            preview = post('preview', {})
            self.assertEqual(preview['total'], 8)
            with self.assertRaises(HTTPError) as failure: post('start', {'preview_id':preview['preview_id']})
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()
            post('verify', {})
            preview = post('preview', {})
            post('start', {'preview_id':preview['preview_id']})
            server.collector.thread.join(timeout=5)
            self.assertEqual(server.collector.status()['job']['state'], 'completed')
        finally:
            server.collector.close()
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
