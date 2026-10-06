from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Catalog, Server
from favorites import Collector, FavoriteStore, GalleryUnavailable


class BatchCollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'catalog.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english');
            """)
            for year in (2006,2016,2026):
                posted=int(datetime(year,6,1,tzinfo=timezone.utc).timestamp())
                for index in range(120):
                    gid=year*1000+index+1
                    db.execute("INSERT INTO gallery(gid,token,title,title_jpn,category,posted,filecount,rating) VALUES (?,'abcdef0123','Test book','','Non-H',?,10,?)",(gid,posted,f'{5-index/100:.2f}'))
                    db.execute('INSERT INTO gid_tid VALUES(?,1)',(gid,))
            db.commit()
        self.catalog=Catalog(self.path)
        self.calls=[]
        self.collector=Collector(self.catalog,self.fetch)
        self.query={'tag':['language:english']}
        self.collector.configure({'host':'e-hentai.org','collection_mode':'batch','batch_size':100,'skip_existing':True})

    def tearDown(self):
        self.collector.close();self.directory.cleanup()

    def fetch(self,settings,gid,token):
        self.calls.append(gid);return gid%100

    def ready(self):
        self.collector.verify(self.query)
        self.collector.settings['interval']=0.00001

    def finish(self):
        self.collector.thread.join(timeout=8)
        self.assertFalse(self.collector.thread.is_alive())

    def first_batch(self):
        preview=self.collector.preview(self.query)
        selected=[row['gid'] for row in self.collector.preview_data['candidates']]
        self.collector.start(self.query,preview['preview_id']);self.finish()
        return selected

    def test_years_share_batch_and_each_year_prefers_higher_scores(self):
        preview=self.collector.preview({**self.query,'page':['99'],'limit':['1']})
        self.assertEqual((preview['total'],preview['pending'],preview['skipped'],preview['deferred']),(360,100,0,260))
        self.assertEqual(preview['scope_known'],0)
        self.assertEqual(preview['year_breakdown'],[{'year':2026,'count':34},{'year':2016,'count':33},{'year':2006,'count':33}])
        selected=self.collector.preview_data['candidates']
        self.assertEqual([item['gid'] for item in selected[:6]],[2026001,2016001,2006001,2026002,2016002,2006002])
        self.assertEqual(self.calls,[])
        self.assertIsNone(self.catalog.favorites.latest())

    def test_two_batches_are_distinct_and_worker_keeps_preview_order(self):
        self.ready()
        first=self.first_batch()
        self.assertEqual(self.calls[1:],first)
        job=self.collector.status()['job']
        self.assertEqual(job['total'],100)
        self.assertEqual(job['collection_mode'],'batch')
        self.assertEqual(job['selection']['scope_total'],360)
        second_preview=self.collector.preview({},'next_batch')
        second=[item['gid'] for item in self.collector.preview_data['candidates']]
        self.assertEqual(second_preview['scope_known'],101)
        self.assertEqual(second_preview['pending'],100)
        self.assertFalse(set(first)&set(second))
        with self.assertRaises(ValueError):self.collector.next_batch()
        self.collector.next_batch(second_preview['preview_id']);self.finish()
        self.assertEqual(self.calls[101:],second)
        self.assertEqual(len(set(self.calls)),201)
        self.assertEqual(self.catalog.recorded({})['total'],201)

    def test_batch_sizes_and_full_mode_do_not_change_scope(self):
        for size,expected in ((100,100),(300,300),(1000,360)):
            self.collector.configure({'host':'e-hentai.org','collection_mode':'batch','batch_size':size,'skip_existing':True})
            preview=self.collector.preview(self.query)
            self.assertEqual(preview['pending'],expected)
            self.assertEqual(preview['total'],360)
        self.collector.configure({'host':'e-hentai.org','collection_mode':'full','batch_size':100})
        full=self.collector.preview(self.query)
        self.assertEqual((full['pending'],full['deferred']),(360,0))
        self.assertIsNone(self.collector.preview_data['candidates'])
        for values in ({'collection_mode':'invalid'},{'batch_size':True},{'batch_size':0},{'batch_size':100.5},{'rating_priority':'yes'}):
            with self.assertRaises(ValueError):self.collector.configure({'host':'e-hentai.org',**values})

    def test_rating_priority_selects_global_highest_scores(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE gallery SET rating=printf('%.2f',CAST(rating AS REAL)+1) WHERE gid BETWEEN 2026001 AND 2026120")
            db.execute("UPDATE gallery SET rating='9.99' WHERE gid=2006120")
            db.execute("UPDATE gallery SET rating='9.98' WHERE gid=2016120")
            db.execute("UPDATE gallery SET rating='9.97' WHERE gid=2026120")
            db.commit()
        self.collector.configure({'host':'e-hentai.org','collection_mode':'batch','batch_size':100,'skip_existing':True,'rating_priority':True})
        preview=self.collector.preview(self.query)
        selected=[item['gid'] for item in self.collector.preview_data['candidates']]
        self.assertEqual(selected[:3],[2006120,2016120,2026120])
        self.assertTrue(preview['rating_priority'])
        self.assertEqual(preview['pending'],100)
        self.assertNotEqual(preview['year_breakdown'],[{'year':2026,'count':34},{'year':2016,'count':33},{'year':2006,'count':33}])

    def test_full_mode_worker_uses_rating_priority_order(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE gallery SET rating='9.99' WHERE gid=2006120")
            db.execute("UPDATE gallery SET rating='9.98' WHERE gid=2016120")
            db.execute("UPDATE gallery SET rating='9.97' WHERE gid=2026120")
            db.commit()
        self.collector.configure({'host':'e-hentai.org','collection_mode':'full','batch_size':100,'skip_existing':True,'rating_priority':True})
        self.collector.verified=True;self.collector.settings['interval']=0.00001
        preview=self.collector.preview(self.query)
        self.assertTrue(preview['rating_priority'])
        self.collector.start(self.query,preview['preview_id']);self.finish()
        self.assertEqual(self.calls[:3],[2006120,2016120,2026120])
        job=self.collector.status()['job']
        self.assertTrue(job['selection']['rating_priority'])
        self.assertEqual(job['collection_mode'],'full')

    def test_cached_zero_is_skipped_and_unknowns_precede_stale_refreshes(self):
        self.catalog.favorites.save(2026001,0,'e-hentai.org')
        with self.catalog.favorites.connection() as db:db.execute('UPDATE favorites SET checked_at=1')
        strict=self.collector.preview(self.query)
        self.assertEqual(strict['skipped'],1)
        self.assertNotIn(2026001,[item['gid'] for item in self.collector.preview_data['candidates']])
        self.collector.configure({'host':'e-hentai.org','collection_mode':'batch','skip_existing':False})
        preview=self.collector.preview(self.query)
        self.assertEqual(preview['eligible_total'],360)
        self.assertEqual(preview['refresh_count'],0)
        self.assertNotIn(2026001,[item['gid'] for item in self.collector.preview_data['candidates']])

    def test_failed_high_score_does_not_fill_next_batch_repeatedly(self):
        self.ready()
        preview=self.collector.preview(self.query)
        failed=self.collector.preview_data['candidates'][0]['gid']
        def unavailable(settings,gid,token):
            if gid==failed:raise GalleryUnavailable('test unavailable')
            return self.fetch(settings,gid,token)
        self.collector.fetcher=unavailable
        self.collector.start(self.query,preview['preview_id']);self.finish()
        self.assertEqual(self.collector.status()['job']['failed'],1)
        self.collector.preview({},'next_batch')
        self.assertNotIn(failed,[item['gid'] for item in self.collector.preview_data['candidates']])

    def test_confirm_detects_changed_candidate_order_even_when_counts_match(self):
        preview=self.collector.preview(self.query)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE gallery SET rating='9.0' WHERE gid=2026120");db.commit()
        with self.assertRaisesRegex(ValueError,'已变化'):
            self.collector.start(self.query,preview['preview_id'])
        self.assertEqual(self.calls,[])

    def test_unknown_publication_year_is_labeled_separately(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO gallery(gid,token,title,title_jpn,category,posted,filecount,rating) VALUES(9,'abcdef0123','Unknown year','','Non-H',0,10,'5.0')")
            db.execute('INSERT INTO gid_tid VALUES(9,1)');db.commit()
        preview=self.collector.preview(self.query)
        self.assertIn({'year':0,'count':1},preview['year_breakdown'])
        self.assertEqual(preview['pending'],100)

    def test_restart_and_larger_setting_do_not_expand_paused_batch(self):
        self.ready()
        preview=self.collector.preview(self.query)
        original={item['gid'] for item in self.collector.preview_data['candidates']}
        entered,release=threading.Event(),threading.Event()
        def blocked(settings,gid,token):
            entered.set();release.wait(timeout=3);return self.fetch(settings,gid,token)
        self.collector.fetcher=blocked
        try:
            self.collector.start(self.query,preview['preview_id'])
            self.assertTrue(entered.wait(timeout=2));self.collector.pause();release.set();self.finish()
        finally:release.set()
        self.collector.close();self.collector=Collector(self.catalog,self.fetch)
        self.collector.configure({'host':'e-hentai.org','collection_mode':'batch','batch_size':1000,'skip_existing':True})
        self.ready()
        resumed=self.collector.preview({},'resume')
        self.assertEqual(resumed['batch_size'],100)
        self.assertEqual(resumed['scope_total'],360)
        self.assertLessEqual(resumed['pending'],99)
        self.collector.resume(preview_id=resumed['preview_id']);self.finish()
        self.assertEqual(self.collector.status()['job']['total'],100)
        self.assertTrue(set(self.calls[1:]).issubset(original))

    def test_migration_preserves_old_job_and_queue(self):
        path=Path(self.directory.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE jobs(id INTEGER PRIMARY KEY,query_json TEXT NOT NULL,host TEXT NOT NULL,state TEXT NOT NULL,total INTEGER NOT NULL DEFAULT 0,cached INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,message TEXT NOT NULL DEFAULT '',cooldown_until INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE tasks(job_id INTEGER NOT NULL,gid INTEGER NOT NULL,token TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(job_id,gid));
                INSERT INTO jobs(id,query_json,host,state,total,created_at,updated_at) VALUES(1,'{}','e-hentai.org','paused',1,1,1);
                INSERT INTO tasks(job_id,gid,token) VALUES(1,55,'abcdef0123');
            """);db.commit()
        store=FavoriteStore(path)
        self.assertEqual(store.latest()['collection_mode'],'full')
        with store.connection() as db:self.assertEqual(tuple(db.execute('SELECT gid,priority FROM tasks').fetchone()),(55,0))

    def test_next_batch_route_previews_then_confirms_previous_query(self):
        self.ready();first=self.first_batch()
        server=Server(('127.0.0.1',0),self.catalog)
        server.collector.fetcher=self.fetch
        server.collector.configure({'host':'e-hentai.org','collection_mode':'batch','batch_size':100,'skip_existing':True})
        server.collector.settings['interval']=0.00001
        server.collector.verified=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        def post(path,payload):
            req=Request(root+'/api/collector/'+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token,'Origin':root})
            with urlopen(req) as response:return json.load(response)
        try:
            with self.assertRaises(HTTPError) as failure:post('next_batch',{})
            self.assertEqual(failure.exception.code,400);failure.exception.close()
            before=len(self.calls)
            preview=post('preview',{'operation':'next_batch'})
            self.assertEqual(len(self.calls),before)
            self.assertEqual(preview['query']['tag'],self.query['tag'])
            self.assertEqual(preview['scope_total'],360)
            post('next_batch',{'preview_id':preview['preview_id']})
            server.collector.thread.join(timeout=8)
            self.assertFalse(server.collector.thread.is_alive())
            self.assertEqual(len(self.calls)-before,100)
            self.assertFalse(set(self.calls[before:])&set(first))
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
