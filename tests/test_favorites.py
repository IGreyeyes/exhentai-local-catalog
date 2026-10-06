from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import sqlite3
import tempfile
import threading
import time
import unittest

from app import Catalog, Server
from favorites import CollectionPaused, Collector, GalleryUnavailable, SameGalleryRedirect, parse_favorite_count


class FavoriteParsingTests(unittest.TestCase):
    def test_counts_and_real_zero(self):
        for content, expected in (("0",0),("Never",0),("Once",1),("12,345",12345),("123 times",123),("<b>2,000</b>",2000)):
            self.assertEqual(parse_favorite_count(f'<td id="favcount">{content}</td>'),expected)

    def test_missing_and_unrecognized_counts_never_become_zero(self):
        for content in ("", "unknown", "-1", "1,23", "1.2k"):
            with self.assertRaises(CollectionPaused):
                parse_favorite_count(f'<span id="favcount">{content}</span>')
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('<html>Sign in</html>')

    def test_gallery_identity_and_block_pages(self):
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('<script>var gid = 99;</script><span id="favcount">2</span>',1)
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('Your IP address has been banned.')
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('Just a moment...')
        with self.assertRaises(GalleryUnavailable):
            parse_favorite_count('Gallery Not Available')

    def test_not_found_and_other_unavailable_notices_are_gallery_failures(self):
        for notice in (
            'Gallery not found. If you just added this gallery, you may have to wait a short while before it becomes available. It might also have been reverted to its previous version.',
            'This gallery is not available.',
            'This gallery is pining for the fjords.',
        ):
            with self.subTest(notice=notice),self.assertRaises(GalleryUnavailable):
                parse_favorite_count(notice)

    def test_redirect_cannot_leak_cookie_or_change_gallery(self):
        request=Request('https://exhentai.org/g/1/abcdef0123/',headers={'Cookie':'test-cookie=secret'})
        redirect=SameGalleryRedirect()
        for target in ('https://other.example/g/1/abcdef0123/','https://exhentai.org/g/2/abcdef0123/','http://exhentai.org/g/1/abcdef0123/'):
            with self.assertRaises(CollectionPaused):
                redirect.redirect_request(request,None,302,'Moved',{},target)


class FavoriteCollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        path=Path(self.directory.name)/'catalog.sqlite3'
        db=sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE gallery (gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
            CREATE TABLE tag (id INTEGER PRIMARY KEY,name TEXT UNIQUE);
            CREATE TABLE gid_tid (gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
            INSERT INTO tag VALUES (1,'language:english');
        """)
        for gid in range(1,5):
            db.execute("INSERT INTO gallery(gid,token,title,title_jpn,category,posted,filecount,rating) VALUES (?,?,'Test book','','Non-H',?,10,'4.0')",(gid,'abcdef0123',gid))
            db.execute("INSERT INTO gid_tid VALUES (?,1)",(gid,))
        db.commit();db.close()
        self.catalog=Catalog(path)
        self.calls=[]
        self.collector=Collector(self.catalog,self.fetch)
        self.query={'tag':['language:english']}

    def tearDown(self):
        self.collector.close()
        self.directory.cleanup()

    def fetch(self,settings,gid,token):
        self.calls.append(gid)
        return gid*10

    def ready(self):
        self.collector.configure({'host':'exhentai.org','ipb_member_id':'123','ipb_pass_hash':'test-secret-hash','igneous':'test-igneous','interval':3})
        self.collector.verify(self.query)
        # Only mocked requests use this tiny interval; the public API enforces >=3s.
        self.collector.settings['interval']=0.001

    def finish(self):
        if self.collector.thread:
            self.collector.thread.join(timeout=4)
            self.assertFalse(self.collector.thread.is_alive())

    def start(self,query=None):
        query=self.query if query is None else query
        preview=self.collector.preview(query)
        return self.collector.start(query,preview['preview_id'])

    def resume(self,retry_failed=False):
        preview=self.collector.preview({},'retry' if retry_failed else 'resume')
        return self.collector.resume(retry_failed=retry_failed,preview_id=preview['preview_id'])

    def test_favorite_sort_puts_unknown_after_zero_and_covers_all_pages(self):
        for gid,count in ((1,0),(2,20),(4,10)):
            self.catalog.favorites.save(gid,count,'exhentai.org')
        first=self.catalog.search({**self.query,'sort':['favorites'],'limit':['2']})
        second=self.catalog.search({**self.query,'sort':['favorites'],'limit':['2'],'page':['2']})
        self.assertEqual([item['gid'] for item in first['items']],[2,4])
        self.assertEqual([item['favorite_count'] for item in second['items']],[0,None])
        self.assertEqual(first['favorite_coverage']['known'],3)
        self.assertFalse(first['favorite_coverage']['complete'])
        self.catalog.favorites.save(3,99,'exhentai.org')
        updated=self.catalog.search({**self.query,'sort':['favorites']})
        self.assertEqual(updated['items'][0]['gid'],3)
        self.assertTrue(updated['favorite_coverage']['complete'])

    def test_start_includes_entire_query_and_reuses_fresh_cache(self):
        with self.assertRaises(ValueError):
            self.collector.start(self.query)
        self.ready()
        self.start({**self.query,'limit':['1'],'page':['2']})
        self.finish()
        job=self.collector.status()['job']
        self.assertEqual((job['total'],job['cached'],job['succeeded'],job['state']),(4,1,3,'completed'))
        self.assertEqual(sorted(self.calls),[1,2,3,4])
        self.start()
        self.assertEqual(self.collector.status()['job']['cached'],4)
        self.assertEqual(len(self.calls),4)

    def test_stale_cache_is_refreshed(self):
        self.ready()
        self.catalog.favorites.save(1,999,'exhentai.org')
        with self.catalog.favorites.connection() as db:
            db.execute("UPDATE favorites SET checked_at=1 WHERE gid=1")
        self.start()
        self.finish()
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute("SELECT favorite_count FROM favorites WHERE gid=1").fetchone()[0],10)

    def test_pause_restart_resume_preserves_completed_items(self):
        self.ready()
        entered,release=threading.Event(),threading.Event()
        def blocked(settings,gid,token):
            entered.set();release.wait(timeout=3)
            return self.fetch(settings,gid,token)
        self.collector.fetcher=blocked
        self.start()
        self.assertTrue(entered.wait(timeout=2))
        self.collector.pause();release.set();self.finish()
        self.assertEqual(self.collector.status()['job']['state'],'paused')
        self.assertEqual(self.collector.status()['job']['succeeded'],1)
        self.collector.close()
        self.collector=Collector(self.catalog,self.fetch)
        self.assertFalse(self.collector.status()['configured'])
        with self.assertRaises(ValueError):
            self.collector.resume()
        self.ready()
        self.resume();self.finish()
        self.assertEqual(self.collector.status()['job']['state'],'completed')
        self.assertEqual(self.calls.count(3),1)

    def test_failure_does_not_erase_old_count_and_can_be_retried(self):
        self.ready()
        self.catalog.favorites.save(3,300,'exhentai.org')
        with self.catalog.favorites.connection() as db:
            db.execute("UPDATE favorites SET checked_at=1 WHERE gid=3")
        def unavailable(settings,gid,token):
            if gid==3:raise GalleryUnavailable('Test unavailable')
            return self.fetch(settings,gid,token)
        self.collector.fetcher=unavailable
        self.start();self.finish()
        self.assertEqual(self.collector.status()['job']['state'],'completed_with_errors')
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute("SELECT favorite_count FROM favorites WHERE gid=3").fetchone()[0],300)
        self.collector.fetcher=self.fetch
        self.resume(retry_failed=True);self.finish()
        self.assertEqual(self.collector.status()['job']['state'],'completed')

    def test_verification_skips_unavailable_gallery_and_preserves_old_count(self):
        self.catalog.favorites.save(4,400,'exhentai.org')
        with self.catalog.favorites.connection() as db:db.execute('UPDATE favorites SET checked_at=1 WHERE gid=4')
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.settings['interval']=0.001
        def fetch(settings,gid,token):
            self.calls.append(gid)
            if gid==4:return parse_favorite_count('Gallery not found.')
            return 30
        self.collector.fetcher=fetch
        result=self.collector.verify(self.query)
        self.assertTrue(result['verified'])
        self.assertEqual((result['gid'],result['favorite_count']),(3,30))
        self.assertEqual(self.calls,[4,3])
        self.assertEqual([item['gid'] for item in result['unavailable']],[4])
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=4').fetchone()[0],400)
            self.assertEqual(db.execute('SELECT gid FROM collection_failures').fetchall()[0][0],4)

    def test_saved_task_verification_marks_unavailable_task_and_tries_next(self):
        self.ready()
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('pause'))
        self.start();self.finish()
        job=self.collector.store.latest()
        self.calls=[]
        def fetch(settings,gid,token):
            self.calls.append(gid)
            if gid==3:raise GalleryUnavailable('Gallery not found')
            return gid*10
        self.collector.fetcher=fetch;self.collector.last_request=0
        result=self.collector.verify(None)
        self.assertEqual(self.calls,[3,2])
        self.assertTrue(result['verified'])
        with self.catalog.favorites.connection() as db:
            self.assertEqual(tuple(db.execute('SELECT state,attempts FROM tasks WHERE job_id=? AND gid=3',(job['id'],)).fetchone()),('failed',1))
            self.assertEqual(db.execute('SELECT job_id FROM collection_failures WHERE gid=3').fetchone()[0],job['id'])
        self.assertEqual(self.collector.store.latest()['state'],'paused')

    def test_verification_still_stops_on_login_or_unknown_page(self):
        self.collector.configure({'host':'e-hentai.org'})
        def fetch(settings,gid,token):
            self.calls.append(gid)
            return parse_favorite_count('<html>Sign in</html>')
        self.collector.fetcher=fetch
        with self.assertRaisesRegex(ValueError,'未找到可靠'):
            self.collector.verify(self.query)
        self.assertEqual(self.calls,[4])
        self.assertFalse(self.collector.verified)
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM favorites').fetchone()[0],0)

    def test_verification_limits_unavailable_samples_and_respects_request_interval(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.settings['interval']=0.01
        times=[]
        def fetch(settings,gid,token):
            self.calls.append(gid);times.append(time.monotonic())
            raise GalleryUnavailable('Gallery not found')
        self.collector.fetcher=fetch
        with self.assertRaisesRegex(ValueError,'3 条作品均不可访问'):
            self.collector.verify(self.query)
        self.assertEqual(self.calls,[4,3,2])
        self.assertTrue(all(later-earlier>=0.009 for earlier,later in zip(times,times[1:])))
        self.assertFalse(self.collector.verified)

    def test_not_found_page_does_not_pause_whole_collection(self):
        self.ready()
        def fetch(settings,gid,token):
            if gid==3:return parse_favorite_count('Gallery not found.')
            return gid*10
        self.collector.fetcher=fetch
        self.start();self.finish()
        job=self.collector.store.latest()
        self.assertEqual(job['state'],'completed_with_errors')
        self.assertEqual((job['failed'],job['succeeded']),(1,2))

    def test_throttling_pauses_and_survives_restart(self):
        self.ready()
        def limited(*args):raise CollectionPaused('Test rate limit',60)
        self.collector.fetcher=limited
        self.start();self.finish()
        self.assertEqual(self.collector.status()['job']['pending'],3)
        self.assertEqual(self.collector.status()['job']['state'],'paused')
        self.collector.close()
        self.collector=Collector(self.catalog,self.fetch)
        self.assertGreater(self.collector.status()['cooldown_until'],time.time())

    def test_cookie_stays_in_memory_and_settings_validation(self):
        self.ready()
        status=json.dumps(self.collector.status())
        self.assertNotIn('test-secret-hash',status)
        self.assertNotIn('test-igneous',status)
        self.collector.configure({'host':'exhentai.org','interval':5})
        self.assertIn('test-secret-hash',self.collector.settings['cookies'])
        for values in ({'host':'invalid.example'},{'host':'e-hentai.org','interval':0},{'host':'e-hentai.org','proxy':'socks5://127.0.0.1:7890'},{'host':'e-hentai.org','ipb_pass_hash':'x\r\nInjected:1'}):
            with self.assertRaises(ValueError):self.collector.configure(values)
        for file in Path(self.directory.name).iterdir():
            if file.is_file():self.assertNotIn(b'test-secret-hash',file.read_bytes())

    def test_custom_days_and_policy_changes_keep_verified_access(self):
        self.ready()
        self.catalog.favorites.save(1,0,'exhentai.org')
        with self.catalog.favorites.connection() as db:
            db.execute("UPDATE favorites SET checked_at=? WHERE gid=1",(int(time.time())-45*86400,))
        self.collector.configure({'refresh_days':90})
        self.assertTrue(self.collector.verified)
        self.assertEqual(self.collector.status()['refresh_days'],90)
        self.assertEqual(self.collector.preview(self.query)['skipped'],2)
        self.collector.configure({'refresh_days':30})
        self.assertEqual(self.collector.preview(self.query)['refresh_count'],1)
        self.collector.configure({'refresh_days':600})
        self.assertEqual(self.collector.status()['refresh_days'],600)
        for value in (0,-1,90.5,True,'x',3651):
            with self.assertRaises(ValueError):self.collector.configure({'refresh_days':value})

    def test_skip_existing_includes_old_counts_and_zero(self):
        self.ready()
        for gid,count in ((1,0),(2,20)):
            self.catalog.favorites.save(gid,count,'exhentai.org')
        with self.catalog.favorites.connection() as db:
            db.execute("UPDATE favorites SET checked_at=1 WHERE gid IN (1,2)")
        self.collector.configure({'skip_existing':True,'refresh_days':1})
        self.collector.settings['interval']=0.001
        preview=self.collector.preview(self.query)
        self.assertEqual((preview['pending'],preview['skipped'],preview['refresh_count']),(1,3,0))
        self.collector.start(self.query,preview['preview_id']);self.finish()
        self.assertEqual(self.calls,[4,3])
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute("SELECT checked_at FROM favorites WHERE gid=1").fetchone()[0],1)

    def test_preview_has_no_requests_or_jobs_and_uses_pending_count(self):
        self.ready()
        self.collector.settings['interval']=4
        self.collector.response_times.clear();self.collector.response_times.extend([1.0,3.0])
        before=list(self.calls)
        preview=self.collector.preview(self.query)
        self.assertEqual(self.calls,before)
        self.assertIsNone(self.collector.store.latest())
        self.assertEqual((preview['total'],preview['pending'],preview['skipped']),(4,3,1))
        self.assertEqual(preview['estimated_seconds'],18)
        self.assertEqual(preview['interval_seconds'],12)
        self.assertEqual(preview['estimate_basis'],'measured')
        self.collector.response_times.clear()
        self.assertEqual(self.collector.preview(self.query)['estimate_basis'],'assumed')

    def test_confirmation_is_required_and_preview_is_single_use(self):
        self.ready()
        for token in (None,'invented'):
            with self.assertRaisesRegex(ValueError,'先预估'):
                self.collector.start(self.query,token)
        preview=self.collector.preview(self.query)
        self.assertEqual(self.calls,[4])
        self.collector.start(self.query,preview['preview_id']);self.finish()
        with self.assertRaisesRegex(ValueError,'先预估'):
            self.collector.start(self.query,preview['preview_id'])

    def test_changed_or_expired_preview_cannot_start(self):
        self.ready()
        preview=self.collector.preview(self.query)
        self.collector.configure({'refresh_days':90})
        with self.assertRaises(ValueError):self.collector.start(self.query,preview['preview_id'])
        preview=self.collector.preview(self.query)
        with self.assertRaisesRegex(ValueError,'已变化'):
            self.collector.start({**self.query,'title':['absent']},preview['preview_id'])
        preview=self.collector.preview(self.query)
        self.collector.preview_data['expires']=0
        with self.assertRaisesRegex(ValueError,'过期'):
            self.collector.start(self.query,preview['preview_id'])
        preview=self.collector.preview(self.query)
        self.catalog.favorites.save(1,10,'exhentai.org')
        with self.assertRaisesRegex(ValueError,'已变化'):
            self.collector.start(self.query,preview['preview_id'])
        self.assertEqual(self.calls,[4])
        self.assertIsNone(self.collector.store.latest())

    def test_skip_mode_verification_never_refetches_known_and_zero_work_needs_no_network(self):
        for gid in range(1,5):self.catalog.favorites.save(gid,0,'exhentai.org')
        with self.catalog.favorites.connection() as db:db.execute("UPDATE favorites SET checked_at=1")
        self.collector.configure({'host':'e-hentai.org','skip_existing':True})
        self.assertTrue(self.collector.verify(self.query)['no_request'])
        self.assertFalse(self.collector.verified)
        preview=self.collector.preview(self.query)
        self.assertEqual((preview['pending'],preview['estimated_seconds']),(0,0))
        result=self.collector.start(self.query,preview['preview_id'])
        self.assertEqual(result['job']['state'],'completed')
        self.assertEqual(result['job']['cached'],4)
        self.assertEqual(self.calls,[])

    def test_resume_skip_option_prunes_already_known_queue_items(self):
        self.ready()
        def stop(*args):raise CollectionPaused('pause')
        self.collector.fetcher=stop
        self.start();self.finish()
        self.catalog.favorites.save(3,0,'exhentai.org')
        with self.catalog.favorites.connection() as db:db.execute("UPDATE favorites SET checked_at=1 WHERE gid=3")
        self.collector.configure({'skip_existing':True})
        self.collector.settings['interval']=0.001
        self.collector.fetcher=self.fetch
        preview=self.collector.preview({},'resume')
        self.assertEqual((preview['total'],preview['pending'],preview['skipped']),(3,2,1))
        with self.assertRaises(ValueError):self.collector.resume()
        self.collector.resume(preview_id=preview['preview_id']);self.finish()
        self.assertEqual(self.calls,[4,2,1])
        self.assertEqual(self.collector.store.latest()['cached'],2)

    def test_http_preview_then_confirm_is_separate_from_fetching(self):
        server=Server(('127.0.0.1',0),self.catalog)
        server.collector.fetcher=self.fetch
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        def post(route,payload):
            request=Request(root+'/api/collector/'+route,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token,'Origin':root})
            with urlopen(request) as response:return json.load(response)
        query='tag=language%3Aenglish'
        try:
            post('settings',{'host':'e-hentai.org','refresh_days':90,'skip_existing':True})
            preview=post('preview',{'query':query})
            self.assertEqual(preview['pending'],4)
            self.assertEqual(self.calls,[])
            with self.assertRaises(HTTPError) as failure:post('start',{'query':query})
            self.assertEqual(failure.exception.code,400);failure.exception.close()
            post('verify',{'query':query})
            server.collector.settings['interval']=0.001
            preview=post('preview',{'query':query})
            self.assertEqual(preview['pending'],3)
            self.assertEqual(self.calls,[4])
            post('start',{'query':query,'preview_id':preview['preview_id']})
            server.collector.thread.join(timeout=3)
            self.assertEqual(sorted(self.calls),[1,2,3,4])
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_completion_callback_runs_after_results_are_committed(self):
        self.ready()
        completed=[]
        def on_completed(job_id):
            with self.catalog.favorites.connection() as db:
                count=db.execute('SELECT COUNT(*) FROM favorites').fetchone()[0]
            completed.append((job_id,self.collector.store.latest()['state'],count))
        self.collector.on_completed=on_completed
        self.start();self.finish()
        self.assertEqual(completed,[(self.collector.store.latest()['id'],'completed',4)])

    def test_post_requires_local_token_and_correct_origin(self):
        server=Server(('127.0.0.1',0),self.catalog)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        try:
            payload=json.dumps({'host':'e-hentai.org'}).encode()
            for headers in ({'Content-Type':'application/json'},{'Content-Type':'application/json','X-Catalog-Token':server.action_token,'Origin':'https://other.example'}):
                with self.assertRaises(HTTPError) as failure:
                    urlopen(Request(root+'/api/collector/settings',data=payload,headers=headers))
                self.assertEqual(failure.exception.code,403);failure.exception.close()
            headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token,'Origin':root}
            with urlopen(Request(root+'/api/collector/settings',data=payload,headers=headers)) as response:
                self.assertTrue(json.load(response)['configured'])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
