from pathlib import Path
from contextlib import closing
from http.client import BadStatusLine, HTTPResponse, IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import io
import sqlite3
import tempfile
import threading
import time
import unittest

from app import Catalog, Server
from favorite_transfer import merge_snapshot
from favorites import CollectionPaused, Collector, GalleryAccessUncertain, GalleryUnavailable, RetryableFetch, SameGalleryRedirect, collection_error_details, fetch_favorites, parse_favorite_count


def http_response(payload):
    class FakeSocket:
        def makefile(self, *args):
            return io.BytesIO(payload)
    response = HTTPResponse(FakeSocket())
    response.begin()
    return response


class FavoriteNetworkTests(unittest.TestCase):
    def test_http_not_found_requires_an_explicit_gallery_notice(self):
        for code in (404,410):
            for body, expected in (
                (b'',CollectionPaused),
                (b'<html>Proxy route unavailable</html>',CollectionPaused),
                (b'<h1>Gallery Not Available</h1>',CollectionPaused),
                (b'<html>Please log in. Gallery not found.</html>',CollectionPaused),
                (b'<span id="favcount">12</span>',CollectionPaused),
                (b'<span id="favcount">12</span><p>This gallery has been removed.</p>',CollectionPaused),
                (b'<!-- Gallery not found. --><script>"This gallery has been removed"</script>',CollectionPaused),
                (b'<h1>Gallery not found.</h1>',GalleryUnavailable),
                (b'<p>This gallery has been removed.</p>',GalleryUnavailable),
            ):
                with self.subTest(code=code,body=body),patch('favorites.build_opener') as opener:
                    stream=io.BytesIO(body)
                    opener.return_value.open.side_effect=HTTPError('https://exhentai.org/g/1/abcdef0123/',code,'test error',{},stream)
                    with self.assertRaises(expected) as caught:
                        fetch_favorites({'host':'exhentai.org','cookies':'test-cookie=test-value'},1,'abcdef0123')
                    self.assertIn(f'HTTP {code}',str(caught.exception))
                    self.assertTrue(stream.closed)
                    self.assertNotIn('Proxy route unavailable',str(caught.exception))
                    self.assertNotIn('test-value',str(caught.exception))

    def test_http_not_found_preserves_limit_and_challenge_handling(self):
        for body, cooldown in ((b'Too many requests',3600),(b'Just a moment...',0)):
            with self.subTest(body=body),patch('favorites.build_opener') as opener:
                opener.return_value.open.side_effect=HTTPError('https://exhentai.org/g/1/abcdef0123/',404,'test',{},io.BytesIO(body))
                with self.assertRaises(CollectionPaused) as caught:
                    fetch_favorites({'host':'exhentai.org'},1,'abcdef0123')
                self.assertEqual(caught.exception.cooldown,cooldown)

    def test_http_429_still_pauses_even_with_a_gallery_count(self):
        with patch('favorites.build_opener') as opener:
            opener.return_value.open.side_effect=HTTPError('https://e-hentai.org/g/1/abcdef0123/',429,'test',{'Retry-After':'120'},io.BytesIO(b'<span id="favcount">12</span>'))
            with self.assertRaises(CollectionPaused) as caught:
                fetch_favorites({'host':'e-hentai.org'},1,'abcdef0123')
            self.assertEqual(caught.exception.cooldown,120)

    def test_truncated_http_error_body_is_retryable(self):
        body=b'<p>Gallery not found.</p>'
        with patch('favorites.build_opener') as opener:
            response=http_response(b'HTTP/1.1 404 Not Found\r\nContent-Type: text/html\r\nContent-Length: 1000\r\n\r\n'+body)
            opener.return_value.open.side_effect=HTTPError('https://exhentai.org/g/1/abcdef0123/',404,'test',response.headers,response)
            with self.assertRaises(RetryableFetch):
                fetch_favorites({'host':'exhentai.org'},1,'abcdef0123')
            self.assertTrue(response.closed)

    def test_broken_http_responses_become_retryable_and_discard_partial_counts(self):
        body = b'<span id="favcount">999</span>'
        responses = (
            b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1000\r\n\r\n' + body,
            b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n1000\r\n' + body,
            b'test invalid status line\r\n\r\n',
        )
        for raw in responses:
            with self.subTest(raw=raw[:30]), patch('favorites.build_opener') as opener:
                opener.return_value.open.side_effect = lambda *args, **kwargs: http_response(raw)
                with self.assertRaises(RetryableFetch):
                    fetch_favorites({'host':'e-hentai.org'},1,'abcdef0123')

    def test_http_exception_details_are_not_exposed(self):
        for error in (IncompleteRead(b'test-cookie=private-test-value',10), BadStatusLine('private-test-value')):
            with self.subTest(kind=type(error).__name__), patch('favorites.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(RetryableFetch) as caught:
                    fetch_favorites({'host':'e-hentai.org'},1,'abcdef0123')
                self.assertNotIn('private-test-value',str(caught.exception))

    def test_database_error_hints_use_fixed_labels_without_private_exception_text(self):
        for code, label, advice in ((sqlite3.SQLITE_BUSY,'SQLITE_BUSY','占用'),
                                   (sqlite3.SQLITE_READONLY | (1 << 8),'SQLITE_READONLY','写入权限'),
                                   (sqlite3.SQLITE_FULL,'SQLITE_FULL','空间不足'),
                                   (sqlite3.SQLITE_CORRUPT,'SQLITE_CORRUPT','备份')):
            with self.subTest(code=label):
                error=sqlite3.OperationalError('private-test-value')
                error.sqlite_errorcode=code
                kind, actual, hint=collection_error_details(error)
                self.assertEqual((kind,actual),('OperationalError',label))
                self.assertIn(advice,hint)
                self.assertNotIn('private-test-value',hint)


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

    def test_out_of_range_counts_pause_before_database_write(self):
        self.assertEqual(parse_favorite_count(f'<span id="favcount">{2**63-1}</span>'),2**63-1)
        self.assertEqual(parse_favorite_count('<span id="favcount">'+'0'*5000+'12</span>'),12)
        for value in (str(2**63),'9'*5000):
            with self.subTest(length=len(value)), self.assertRaisesRegex(CollectionPaused,'超出可保存范围'):
                parse_favorite_count(f'<span id="favcount">{value}</span>')

    def test_gallery_identity_and_block_pages(self):
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('<script>var gid = 99;</script><span id="favcount">2</span>',1)
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('Your IP address has been banned.')
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('Just a moment...')
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('Gallery Not Available')

    def test_not_found_and_other_unavailable_notices_are_gallery_failures(self):
        for notice in (
            'Gallery not found. If you just added this gallery, you may have to wait a short while before it becomes available. It might also have been reverted to its previous version.',
            'This gallery has been removed.',
            'This gallery is pining for the fjords.',
        ):
            with self.subTest(notice=notice),self.assertRaises(GalleryUnavailable):
                parse_favorite_count(notice)

    def test_valid_gallery_counts_are_not_overridden_by_notice_text(self):
        for content in (
            '<script>var message="This gallery is not available";</script>',
            '<!-- Gallery not found. -->',
            '<style>/* Gallery Not Available */</style>',
            '<script>"Too many requests"</script><!-- Just a moment... -->',
            '<p>A user comment: This gallery is pining for the fjords.</p>',
        ):
            with self.subTest(content=content):
                self.assertEqual(parse_favorite_count('<script>var gid=1;</script><td id="favcount">12 times</td>'+content,1),12)

    def test_gallery_comments_cannot_trigger_rate_limit_cooldown(self):
        for content in ('Too many requests','Your IP address has been banned.','temporarily banned','excessive pageloads','Just a moment...','verify you are human','checking your browser'):
            with self.subTest(content=content):
                self.assertEqual(parse_favorite_count('<td id="favcount">12</td><p>'+content+'</p>'),12)
                with self.assertRaises(CollectionPaused):
                    parse_favorite_count('<p>'+content+'</p>')
        with self.assertRaises(CollectionPaused):
            parse_favorite_count('<td id="favcount">12</td><div class="cf-chl-test">Check</div>')

    def test_unavailable_without_gallery_fields_pauses_for_access_check(self):
        for content in ('Gallery Not Available','This gallery is unavailable.','This gallery is not available.'):
            with self.subTest(content=content),self.assertRaisesRegex(CollectionPaused,'Cookie'):
                parse_favorite_count(content)

    def test_script_and_comment_text_do_not_confirm_a_deleted_gallery(self):
        for content in ('<script>"Gallery not found"</script>','<!-- This gallery has been removed. -->','<template>Gallery not found</template>'):
            with self.subTest(content=content),self.assertRaises(CollectionPaused):
                parse_favorite_count(content)

    def test_redirect_cannot_leak_cookie_or_change_gallery(self):
        request=Request('https://exhentai.org/g/1/abcdef0123/',headers={'Cookie':'test-cookie=secret'})
        redirect=SameGalleryRedirect()
        for target in ('https://other.example/g/1/abcdef0123/','https://exhentai.org/g/2/abcdef0123/','http://exhentai.org/g/1/abcdef0123/'):
            with self.assertRaises(CollectionPaused):
                redirect.redirect_request(request,None,302,'Moved',{},target)


class FavoriteCollectionTests(unittest.TestCase):
    def setUp(self):
        sampler=patch('favorites.secrets.randbelow',return_value=0)
        sampler.start();self.addCleanup(sampler.stop)
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
        # Only mocked requests use this tiny interval; the public API enforces >=1s.
        self.collector.settings['interval']=0.001

    def test_ambiguous_unavailable_page_preserves_saved_queue_and_successful_data(self):
        self.ready()
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('test pause'))
        self.start();self.finish()
        job=self.collector.store.latest()
        with self.collector.store.connection() as db:
            before=[tuple(row) for row in db.execute('SELECT gid,state,attempts,error FROM tasks ORDER BY gid')]
            counts=[tuple(row) for row in db.execute('SELECT * FROM favorites')]
        self.calls=[]
        self.collector.last_request=0
        def unavailable(settings,gid,token):
            self.calls.append(gid)
            return parse_favorite_count('<h1>Gallery Not Available</h1>')
        self.collector.fetcher=unavailable
        with self.assertRaisesRegex(ValueError,'Cookie') as caught:
            self.collector.verify(None)
        self.assertIn('作品链接：https://exhentai.org/g/',str(caught.exception))
        self.assertEqual(len(self.calls),1)
        self.assertFalse(self.collector.verified)
        self.assertFalse(self.collector.verification_unavailable)
        self.assertEqual(self.collector.store.latest()['id'],job['id'])
        with self.collector.store.connection() as db:
            self.assertEqual(before,[tuple(row) for row in db.execute('SELECT gid,state,attempts,error FROM tasks ORDER BY gid')])
            self.assertEqual(counts,[tuple(row) for row in db.execute('SELECT * FROM favorites')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],0)

    def test_ambiguous_unavailable_page_is_failed_without_stopping_background_task(self):
        self.ready()
        self.collector.store.save(3,12,'exhentai.org',7)
        with self.collector.store.connection() as db:
            db.execute('UPDATE favorites SET checked_at=1 WHERE gid=3')
        self.calls=[]
        def unavailable(settings,gid,token):
            self.calls.append(gid)
            if gid==3:return parse_favorite_count('This gallery is not available.')
            return gid*10
        self.collector.fetcher=unavailable
        self.start();self.finish()
        job=self.collector.store.latest()
        self.assertEqual(job['state'],'completed_with_errors')
        self.assertEqual(self.calls,[3,2,1])
        self.assertEqual(job['failed'],1)
        with self.collector.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT state,attempts FROM tasks WHERE gid=3").fetchone()[:],('failed',1))
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=3').fetchone()[0],12)
            self.assertEqual(db.execute('SELECT rating_count FROM rating_counts WHERE gid=3').fetchone()[0],7)
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=4').fetchone()[0],40)

    def test_batch_collection_skips_unavailable_http_responses_in_all_modes(self):
        for mode in ('full','rounds','batch'):
            for code,body in ((404,b'<h1>Gallery Not Available</h1>'),(410,b''),(200,b'This gallery is not available.')):
                with self.subTest(mode=mode,code=code):
                    self.collector.close()
                    self.collector=Collector(self.catalog,self.fetch)
                    with self.collector.store.connection() as db:
                        db.execute('DELETE FROM jobs');db.execute('DELETE FROM tasks')
                        db.execute('DELETE FROM collection_failures');db.execute('DELETE FROM favorites')
                    self.calls=[]
                    self.collector.configure({'host':'e-hentai.org','collection_mode':mode,'batch_size':100})
                    self.collector.verify(self.query)
                    self.collector.settings['interval']=0.00001
                    def fetch(settings,gid,token):
                        self.calls.append(gid)
                        if gid==3:
                            if code==200:return parse_favorite_count(body.decode())
                            with patch('favorites.build_opener') as opener:
                                opener.return_value.open.side_effect=HTTPError('https://e-hentai.org/g/3/abcdef0123/',code,'test unavailable',{},io.BytesIO(body))
                                return fetch_favorites(settings,gid,token)
                        return gid*10
                    self.collector.fetcher=fetch
                    self.start();self.finish()
                    job=self.collector.store.latest()
                    self.assertEqual(self.calls,[4,3,2,1])
                    self.assertEqual((job['state'],job['failed'],job['pending']),('completed_with_errors',1,0))
                    with self.collector.store.connection() as db:
                        self.assertEqual(db.execute('SELECT gid,attempts FROM collection_failures').fetchone()[:],(3,1))
                        self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=2').fetchone()[0],20)

    def test_background_login_and_challenge_responses_still_pause_without_failed_items(self):
        for code,body in ((404,b'Please log in'),(404,b'Just a moment...'),(200,b'Unrecognized test page'),(403,b'')):
            with self.subTest(code=code,body=body):
                self.collector.close()
                self.collector=Collector(self.catalog,self.fetch)
                with self.collector.store.connection() as db:
                    db.execute('DELETE FROM jobs');db.execute('DELETE FROM tasks');db.execute('DELETE FROM favorites')
                self.calls=[]
                self.collector.configure({'host':'e-hentai.org'})
                self.collector.verify(self.query)
                self.collector.settings['interval']=0.00001
                def fetch(settings,gid,token):
                    self.calls.append(gid)
                    if code==200:return parse_favorite_count(body.decode())
                    with patch('favorites.build_opener') as opener:
                        opener.return_value.open.side_effect=HTTPError('https://e-hentai.org/g/3/abcdef0123/',code,'test response',{},io.BytesIO(body))
                        return fetch_favorites(settings,gid,token)
                self.collector.fetcher=fetch
                self.start();self.finish()
                job=self.collector.store.latest()
                self.assertEqual(self.calls,[4,3])
                self.assertEqual((job['state'],job['failed'],job['pending']),('paused',0,3))

    def test_uncertain_access_rotates_verification_in_all_modes_without_recording_failure(self):
        for mode in ('full','rounds','batch'):
            with self.subTest(mode=mode):
                self.collector.close()
                self.collector=Collector(self.catalog,self.fetch)
                with self.collector.store.connection() as db:db.execute('DELETE FROM favorites')
                self.calls=[]
                self.collector.configure({'host':'e-hentai.org','collection_mode':mode})
                self.collector.settings['interval']=0.00001
                def fetch(settings,gid,token):
                    self.calls.append(gid)
                    if gid>2:raise GalleryAccessUncertain('test unavailable (HTTP 404)')
                    return gid*10
                self.collector.fetcher=fetch
                for _ in range(2):
                    self.collector.last_request=0
                    with self.assertRaisesRegex(ValueError,'再次点击验证会尝试其他候选'):
                        self.collector.verify(self.query)
                self.collector.last_request=0
                self.assertEqual(self.collector.verify(self.query)['gid'],2)
                self.assertEqual(self.calls,[4,3,2])
                self.assertEqual(self.collector.status()['verification_deferred'],2)
                self.assertEqual(self.collector.status()['verification_issue']['gid'],3)
                with self.collector.store.connection() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],0)

    def test_random_sample_can_select_the_last_gallery_instead_of_priority_head(self):
        self.collector.configure({'host':'e-hentai.org','rating_priority':True})
        with patch('favorites.secrets.randbelow',return_value=3) as sampler:
            result=self.collector.verify(self.query)
        sampler.assert_called_once_with(4)
        self.assertEqual(self.calls,[1])
        self.assertFalse(result['verification_only'])

    def test_imported_cached_sample_verifies_access_without_rewriting_snapshot(self):
        now=int(time.time())
        merge_snapshot(self.collector.store.path,[{'gid':2,'favorite_count':777,'checked_at':now,'source':'e-hentai.org'}])
        self.collector.configure({'host':'e-hentai.org','skip_existing':True})
        self.collector.fetcher=lambda settings,gid,token:self.calls.append(gid) or 0
        with self.collector.store.connection() as db:
            before=tuple(db.execute('SELECT * FROM favorites WHERE gid=2').fetchone())
        with patch('favorites.secrets.randbelow',return_value=0) as sampler:
            result=self.collector.verify(self.query)
        sampler.assert_called_once_with(1)
        self.assertEqual(self.calls,[2])
        self.assertTrue(result['verification_only'])
        self.assertEqual(result['favorite_count'],0)
        with self.collector.store.connection() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM favorites WHERE gid=2').fetchone()),before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)

    def test_multiple_cached_records_are_randomly_sampled_within_the_cached_group(self):
        self.collector.store.save(2,20,'e-hentai.org')
        self.collector.store.save(4,40,'e-hentai.org')
        self.collector.configure({'host':'e-hentai.org'})
        with patch('favorites.secrets.randbelow',return_value=1) as sampler:
            result=self.collector.verify(self.query)
        sampler.assert_called_once_with(2)
        self.assertEqual(result['gid'],2)
        self.assertTrue(result['verification_only'])

    def test_random_cached_sampling_stays_within_the_requested_tag_scope(self):
        with closing(sqlite3.connect(self.catalog.path)) as db,db:
            db.execute("INSERT INTO gallery VALUES (5,'abcdef0123','Other tag','','Non-H',5,10,'5.0',0,0,0)")
            db.execute("INSERT INTO tag VALUES(2,'language:chinese')")
            db.execute('INSERT INTO gid_tid VALUES(5,2)')
        self.collector.store.save(5,500,'e-hentai.org')
        self.collector.configure({'host':'e-hentai.org'})
        with patch('favorites.secrets.randbelow',return_value=3) as sampler:
            result=self.collector.verify(self.query)
        sampler.assert_called_once_with(4)
        self.assertEqual(self.calls,[1])
        self.assertNotEqual(result['gid'],5)

    def test_random_offset_uses_one_snapshot_during_a_concurrent_favorite_update(self):
        self.collector.store.save(1,10,'e-hentai.org')
        self.collector.store.save(3,30,'e-hentai.org')
        self.collector.configure({'host':'e-hentai.org'})
        def change_favorites(size):
            self.assertEqual(size,2)
            with self.collector.store.connection() as db:
                db.execute('DELETE FROM favorites WHERE gid=1')
            return 1
        with patch('favorites.secrets.randbelow',side_effect=change_favorites):
            result=self.collector.verify(self.query)
        self.assertEqual(result['gid'],1)
        self.assertTrue(result['verification_only'])
        with self.collector.store.connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM favorites WHERE gid=1').fetchone())

    def test_saved_task_uses_cached_sample_when_remaining_gallery_is_unavailable(self):
        self.ready()
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('test pause'))
        self.start();self.finish()
        self.calls=[]
        def fetch(settings,gid,token):
            self.calls.append(gid)
            if gid!=4:raise GalleryAccessUncertain('test unavailable')
            return 999
        self.collector.fetcher=fetch;self.collector.last_request=0
        with self.collector.store.connection() as db:
            queue=[tuple(row) for row in db.execute('SELECT * FROM tasks ORDER BY gid')]
            snapshot=tuple(db.execute('SELECT * FROM favorites WHERE gid=4').fetchone())
        job=self.collector.store.latest()
        result=self.collector.verify(None)
        self.assertTrue(result['verification_only'])
        self.assertEqual(self.calls,[4])
        current=self.collector.store.latest()
        self.assertEqual((current['id'],current['state'],current['pending'],current['current_round']),(job['id'],'paused',3,job['current_round']))
        with self.collector.store.connection() as db:
            self.assertEqual(queue,[tuple(row) for row in db.execute('SELECT * FROM tasks ORDER BY gid')])
            self.assertEqual(snapshot,tuple(db.execute('SELECT * FROM favorites WHERE gid=4').fetchone()))

    def test_cached_access_failure_confirmation_does_not_mutate_frozen_queue(self):
        self.ready()
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('test pause'))
        self.start();self.finish()
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(GalleryAccessUncertain('test unavailable'))
        self.collector.last_request=0
        with self.assertRaises(ValueError):self.collector.verify(None)
        issue=self.collector.status()['verification_issue']
        self.assertFalse(issue['in_queue'])
        result=self.collector.skip_unavailable(issue['id'])
        self.assertEqual((result['job']['failed'],result['job']['pending']),(0,3))
        with self.collector.store.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=4').fetchone()[0],40)
            self.assertEqual(db.execute('SELECT job_id FROM collection_failures WHERE gid=4').fetchone()[0],0)

    def test_future_round_cached_work_is_not_sampled_before_that_round(self):
        self.ready()
        self.collector.configure({'collection_mode':'rounds','batch_size':1})
        self.collector.settings['interval']=0.00001
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('test pause'))
        self.start();self.finish()
        self.collector.store.save(2,20,'exhentai.org')
        self.collector.fetcher=self.fetch;self.collector.last_request=0
        with patch('favorites.secrets.randbelow',return_value=0) as sampler:
            result=self.collector.verify(None)
        sampler.assert_called_once_with(1)
        self.assertEqual(result['gid'],4)
        self.assertTrue(result['verification_only'])
        self.assertEqual((self.collector.store.latest()['state'],self.collector.store.latest()['current_round']),('paused',1))

    def test_uncertain_verification_exhaustion_sends_no_more_requests_and_can_retry(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.settings['interval']=0.00001
        def unavailable(settings,gid,token):
            self.calls.append(gid)
            return parse_favorite_count('Gallery Not Available')
        self.collector.fetcher=unavailable
        for gid in (4,3,2,1):
            self.collector.last_request=0
            with self.assertRaises(ValueError):self.collector.verify(self.query)
            self.assertEqual(self.calls[-1],gid)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'没有其他可验证作品'):
                self.collector.verify(self.query)
        self.assertEqual(self.calls,[4,3,2,1])
        self.collector.last_request=0
        with self.assertRaises(ValueError):self.collector.verify(self.query,retry_unavailable=True)
        self.assertEqual(self.calls,[4,3,2,1,4])
        self.assertEqual(self.collector.status()['verification_deferred'],1)

    def test_confirmed_unavailable_item_leaves_original_queue_and_preserves_counts(self):
        self.ready()
        self.collector.store.save(3,12,'exhentai.org',7)
        with self.collector.store.connection() as db:
            db.execute('UPDATE favorites SET checked_at=1 WHERE gid=3')
            db.execute('UPDATE rating_counts SET checked_at=1 WHERE gid=3')
        def unavailable(settings,gid,token):
            self.calls.append(gid)
            raise GalleryAccessUncertain('test unavailable (HTTP 404)')
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('test initial pause'))
        self.start();self.finish()
        self.collector.fetcher=unavailable
        job=self.collector.store.latest()
        # Isolate the queued confirmation case from the cached access sample.
        self.collector.verification_unavailable.add(4)
        self.collector.last_request=0
        with self.assertRaises(ValueError):self.collector.verify(None)
        issue=self.collector.status()['verification_issue']
        before=list(self.calls)
        self.collector.preview({},'resume')
        result=self.collector.skip_unavailable(issue['id'])
        self.assertEqual(self.calls,before)
        self.assertIsNone(self.collector.preview_data)
        self.assertFalse(self.collector.verified)
        self.assertIsNone(result['verification_issue'])
        self.assertEqual(result['job']['id'],job['id'])
        self.assertEqual(result['job']['current_round'],job['current_round'])
        self.assertEqual(result['job']['failed'],1)
        with self.collector.store.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=3').fetchone()[0],12)
            self.assertEqual(db.execute('SELECT rating_count FROM rating_counts WHERE gid=3').fetchone()[0],7)
            self.assertIn('用户已在浏览器确认',db.execute('SELECT error FROM collection_failures WHERE gid=3').fetchone()[0])
        self.collector.fetcher=self.fetch
        self.collector.last_request=0
        self.assertEqual(self.collector.verify(None)['gid'],2)
        preview=self.collector.preview({},'resume')
        self.collector.resume(preview_id=preview['preview_id']);self.finish()
        self.assertEqual(self.calls[len(before):],[2,1])
        self.assertEqual(self.collector.store.latest()['state'],'completed_with_errors')
        preview=self.collector.preview({},'retry')
        self.collector.resume(retry_failed=True,preview_id=preview['preview_id']);self.finish()
        self.assertEqual(self.calls[-1],3)
        self.assertEqual(self.collector.store.latest()['failed'],0)

    def test_skip_confirmation_requires_current_issue_and_survives_only_same_access(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.fetcher=lambda *args:parse_favorite_count('Gallery Not Available')
        with self.assertRaises(ValueError):self.collector.verify(self.query)
        issue=self.collector.status()['verification_issue']
        for value in (None,True,123,'invalid-test-id'):
            with self.assertRaises(ValueError):self.collector.skip_unavailable(value)
        with patch.object(self.collector,'thread') as thread:
            thread.is_alive.return_value=True
            with self.assertRaisesRegex(ValueError,'先暂停采集'):self.collector.skip_unavailable(issue['id'])
        self.collector.configure({'host':'e-hentai.org','interval':2.5})
        self.assertEqual(self.collector.status()['verification_issue']['id'],issue['id'])
        self.collector.configure({'host':'e-hentai.org','proxy':'http://127.0.0.1:7890'})
        self.assertEqual(self.collector.status()['verification_deferred'],0)
        with self.assertRaises(ValueError):self.collector.skip_unavailable(issue['id'])
        with self.collector.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],0)

    def test_login_challenge_and_rate_limit_do_not_rotate_or_offer_skip(self):
        self.collector.configure({'host':'e-hentai.org'})
        for content in ('Please log in','Just a moment...','Too many requests'):
            with self.subTest(content=content):
                self.collector.cooldown_until=0
                self.collector.last_request=0
                self.calls=[]
                def fetch(settings,gid,token):
                    self.calls.append(gid)
                    return parse_favorite_count(content)
                self.collector.fetcher=fetch
                with self.assertRaises(ValueError):self.collector.verify(self.query)
                self.assertEqual(self.calls,[4])
                self.assertIsNone(self.collector.status()['verification_issue'])
                self.assertEqual(self.collector.status()['verification_deferred'],0)
                self.assertFalse(self.collector.verification_unavailable)

    def test_confirmed_search_failure_is_not_requested_again_when_creating_a_task(self):
        self.collector.configure({'host':'e-hentai.org','collection_mode':'rounds','batch_size':100})
        self.collector.settings['interval']=0.00001
        def fetch(settings,gid,token):
            self.calls.append(gid)
            if gid==4:return parse_favorite_count('Gallery Not Available')
            return gid*10
        self.collector.fetcher=fetch
        with self.assertRaises(ValueError):self.collector.verify(self.query)
        self.collector.skip_unavailable(self.collector.status()['verification_issue']['id'])
        self.collector.last_request=0
        self.assertEqual(self.collector.verify(self.query)['gid'],3)
        self.start();self.finish()
        self.assertEqual(self.calls,[4,3,2,1])
        self.assertEqual(self.collector.store.latest()['failed'],1)

    def test_http_skip_action_confirms_only_the_displayed_issue_without_fetching(self):
        server=Server(('127.0.0.1',0),self.catalog)
        calls=[]
        def unavailable(settings,gid,token):
            calls.append(gid)
            return parse_favorite_count('Gallery Not Available')
        server.collector.fetcher=unavailable
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        def post(route,payload,authorized=True):
            request=Request(root+'/api/collector/'+route,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token if authorized else '', 'Origin':root})
            with urlopen(request) as response:return json.load(response)
        try:
            post('settings',{'host':'e-hentai.org'})
            with self.assertRaises(HTTPError) as failure:post('verify',{'query':'tag=language%3Aenglish'})
            failure.exception.close()
            issue=server.collector.status()['verification_issue']
            for authorized,identifier,code in ((False,issue['id'],403),(True,'invalid-test-id',400)):
                with self.assertRaises(HTTPError) as failure:post('skip_unavailable',{'issue_id':identifier},authorized)
                self.assertEqual(failure.exception.code,code);failure.exception.close()
            result=post('skip_unavailable',{'issue_id':issue['id']})
            self.assertEqual(calls,[4])
            self.assertEqual(result['verification_unavailable'],1)
            self.assertIsNone(result['verification_issue'])
            with self.assertRaises(HTTPError) as failure:post('skip_unavailable',{'issue_id':issue['id']})
            self.assertEqual(failure.exception.code,400);failure.exception.close()
            with self.collector.store.connection() as db:
                self.assertEqual(db.execute('SELECT gid FROM collection_failures').fetchone()[0],4)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_confirming_last_item_of_a_round_does_not_validate_or_start_next_round(self):
        self.ready()
        self.collector.configure({'collection_mode':'rounds','batch_size':1})
        self.collector.settings['interval']=0.00001
        def unavailable(settings,gid,token):
            self.calls.append(gid)
            return parse_favorite_count('Gallery Not Available')
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('test initial pause'))
        self.start();self.finish()
        self.collector.fetcher=unavailable
        job=self.collector.store.latest()
        self.collector.verification_unavailable.add(4)
        self.collector.last_request=0
        with self.assertRaises(ValueError):self.collector.verify(None)
        before=list(self.calls)
        with self.assertRaisesRegex(ValueError,'没有其他可验证作品'):self.collector.verify(None)
        self.collector.skip_unavailable(self.collector.status()['verification_issue']['id'])
        self.assertEqual(self.calls,before)
        self.assertNotIn(2,self.calls)
        result=self.collector.store.latest()
        self.assertEqual((result['id'],result['state'],result['current_round']),(job['id'],'paused',1))
        self.assertFalse(self.collector.thread.is_alive())

    def test_request_intervals_persist_after_restart_and_rejected_settings(self):
        self.assertEqual(self.collector.status()['interval'],3)
        for seconds in (1,1.5,2,2.5,3,1.5):
            status = self.collector.configure({'host':'e-hentai.org','interval':seconds})
            self.assertEqual(status['interval'],seconds)
            restarted = Collector(Catalog(self.catalog.path),self.fetch)
            self.assertEqual(restarted.status()['interval'],seconds)
            self.assertFalse(restarted.status()['configured'])
            self.assertEqual(restarted.configure({'host':'e-hentai.org'})['interval'],seconds)
        for seconds in (0.5,3.5,4,5,60,61,True,float('nan'),float('inf')):
            with self.subTest(seconds=seconds),self.assertRaises(ValueError):
                self.collector.configure({'host':'e-hentai.org','interval':seconds})
        with self.assertRaises(ValueError):
            self.collector.configure({'host':'e-hentai.org','interval':2.5,'refresh_days':0})
        restarted = Collector(self.catalog,self.fetch)
        self.assertEqual(restarted.status()['interval'],1.5)

    def test_upgrade_uses_last_job_interval_when_no_preference_exists(self):
        now=int(time.time())
        with self.catalog.favorites.connection() as db:
            db.execute("INSERT INTO jobs(query_json,host,state,created_at,updated_at,selection_json) VALUES('{}','e-hentai.org','completed',?,?,?)",(now,now,json.dumps({'interval':2.5})))
        self.assertEqual(Collector(self.catalog,self.fetch).status()['interval'],2.5)
        with self.catalog.favorites.connection() as db:
            db.execute("UPDATE jobs SET selection_json=?",(json.dumps({'interval':4}),))
        self.assertEqual(Collector(self.catalog,self.fetch).status()['interval'],3)

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

    def test_multi_category_preview_and_queue_preserve_the_complete_scope(self):
        with closing(sqlite3.connect(self.catalog.path)) as db:
            db.execute("UPDATE gallery SET category='Manga' WHERE gid IN (1,2)")
            db.execute("UPDATE gallery SET category='Artist CG' WHERE gid=3")
            db.commit()
        self.ready()
        self.calls.clear()
        query={**self.query,'category':['Manga','Artist CG']}
        preview=self.collector.preview(query)
        self.assertEqual(preview['total'],3)
        self.collector.start(query,preview['preview_id'])
        self.finish()
        job=self.collector.store.latest()
        self.assertEqual(job['query']['category'],['Artist CG','Manga'])
        with self.collector.store.connection() as db:
            ids={row[0] for row in db.execute('SELECT gid FROM tasks WHERE job_id=?',(job['id'],))}
        self.assertEqual(ids,{1,2,3})
        self.assertNotIn(4,self.calls)

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

    def collect_broken_responses(self, permanent=False):
        self.ready()
        self.catalog.favorites.save(3,300,'exhentai.org',30)
        with self.catalog.favorites.connection() as db:
            db.execute('UPDATE favorites SET checked_at=1 WHERE gid=3')
        attempts=[]
        def open_response(request, **kwargs):
            gid=int(request.full_url.split('/g/')[1].split('/')[0])
            attempts.append(gid)
            if gid==3 and (permanent or attempts.count(3)==1):
                return http_response(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n1000\r\n<span id="favcount">999</span>')
            body=f'<span id="favcount">{gid*10}</span>'.encode()
            return http_response(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        self.collector.fetcher=fetch_favorites
        wait=self.collector.interrupt.wait
        with patch('favorites.build_opener') as opener, patch.object(self.collector.interrupt,'wait',side_effect=lambda seconds:wait(min(seconds,.001))):
            opener.return_value.open.side_effect=open_response
            self.start();self.finish()
        return attempts

    def test_interrupted_response_retries_and_continues_original_queue(self):
        attempts=self.collect_broken_responses()
        job=self.collector.store.latest()
        self.assertEqual((job['state'],job['failed'],job['succeeded']),('completed',0,3))
        self.assertEqual(attempts,[3,3,2,1])
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=3').fetchone()[0],30)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],0)

    def test_persistent_interruption_fails_only_one_item_and_keeps_old_count(self):
        attempts=self.collect_broken_responses(permanent=True)
        job=self.collector.store.latest()
        self.assertEqual((job['state'],job['failed'],job['succeeded']),('completed_with_errors',1,2))
        self.assertEqual(attempts,[3,3,3,2,1])
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=3').fetchone()[0],300)
            self.assertEqual(db.execute('SELECT rating_count FROM rating_counts WHERE gid=3').fetchone()[0],30)
            failure=db.execute('SELECT error,attempts FROM collection_failures WHERE gid=3').fetchone()
            self.assertEqual(failure['attempts'],3)
            self.assertNotIn('999',failure['error'])
        self.collector.fetcher=self.fetch
        self.resume(retry_failed=True);self.finish()
        self.assertEqual(self.collector.store.latest()['state'],'completed')

    def test_verification_interruption_is_readable_and_keeps_saved_count(self):
        self.ready()
        self.catalog.favorites.save(3,300,'exhentai.org')
        with self.catalog.favorites.connection() as db:
            db.execute('UPDATE favorites SET checked_at=1 WHERE gid=3')
        self.collector.fetcher=fetch_favorites
        self.collector.last_request=0
        with patch('favorites.build_opener') as opener:
            opener.return_value.open.side_effect=IncompleteRead(b'private-test-value',100)
            with self.assertRaisesRegex(ValueError,'网络响应中断'):
                self.collector.verify(self.query)
        self.assertFalse(self.collector.verified)
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=3').fetchone()[0],300)

    def test_unexpected_error_has_safe_diagnostics_and_resumable_queue(self):
        self.ready()
        def fail(*args):
            raise RuntimeError('private-test-value https://example.invalid/personal-record')
        self.collector.fetcher=fail
        with self.assertLogs('favorites',level='ERROR') as logged:
            self.start();self.finish()
        job=self.collector.store.latest()
        self.assertEqual((job['state'],job['pending']),('paused',3))
        self.assertIn('RuntimeError',job['message'])
        self.assertIn('读取网页',job['message'])
        self.assertNotIn('private-test-value',job['message']+' '.join(logged.output))
        self.assertNotIn('example.invalid',job['message']+' '.join(logged.output))
        self.assertRegex(' '.join(logged.output),r'line=[1-9][0-9]*')
        self.collector.fetcher=self.fetch
        self.resume();self.finish()
        self.assertEqual(self.collector.store.latest()['state'],'completed')

    def test_failed_database_write_rolls_back_counts_and_preserves_pending_task(self):
        self.ready()
        with self.catalog.favorites.connection() as db:
            db.execute("CREATE TRIGGER test_save_failure BEFORE INSERT ON cloud_self_snapshots WHEN NEW.gid=3 BEGIN SELECT RAISE(ABORT,'private-test-value'); END")
        with self.assertLogs('favorites',level='ERROR') as logged:
            self.start();self.finish()
        job=self.collector.store.latest()
        self.assertEqual((job['state'],job['pending']),('paused',3))
        self.assertIn('IntegrityError',job['message'])
        self.assertIn('保存收藏数',job['message'])
        self.assertNotIn('private-test-value',job['message']+' '.join(logged.output))
        with self.catalog.favorites.connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM favorites WHERE gid=3').fetchone())
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=4').fetchone()[0],40)
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE gid=3').fetchone()[0],'pending')
            db.execute('DROP TRIGGER test_save_failure')
        self.resume();self.finish()
        self.assertEqual(self.collector.store.latest()['state'],'completed')

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
        self.collector.verification_unavailable.add(4)
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

    def test_repeated_verification_moves_past_first_three_failures_in_all_modes(self):
        for mode in ('full','batch','rounds'):
            with self.subTest(mode=mode):
                self.collector.close()
                self.collector=Collector(self.catalog,self.fetch)
                with self.catalog.favorites.connection() as db:
                    db.execute('DELETE FROM favorites')
                self.calls=[]
                self.collector.configure({'host':'e-hentai.org','collection_mode':mode})
                self.collector.settings['interval']=0.00001
                def fetch(settings,gid,token):
                    self.calls.append(gid)
                    if gid>1:raise GalleryUnavailable('test unavailable')
                    return 10
                self.collector.fetcher=fetch
                with self.assertRaisesRegex(ValueError,'再次点击验证将尝试其他'):
                    self.collector.verify(self.query)
                self.collector.last_request=0
                result=self.collector.verify(self.query)
                self.assertTrue(result['verified'])
                self.assertEqual(self.calls,[4,3,2,1])
                self.assertIsNone(self.collector.store.latest())

    def test_exhausted_verification_stops_requests_until_explicit_retry(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.settings['interval']=0.00001
        def unavailable(settings,gid,token):
            self.calls.append(gid)
            raise GalleryUnavailable('test unavailable')
        self.collector.fetcher=unavailable
        with self.assertRaisesRegex(ValueError,'3 条作品均不可访问'):
            self.collector.verify(self.query)
        self.collector.last_request=0
        with self.assertRaisesRegex(ValueError,'没有其他可验证作品'):
            self.collector.verify(self.query)
        before=list(self.calls)
        with self.assertRaisesRegex(ValueError,'本次未向源站发送请求'):
            self.collector.verify(self.query)
        self.assertEqual(self.calls,before)
        self.assertFalse(self.collector.verified)
        self.collector.fetcher=self.fetch;self.collector.last_request=0
        result=self.collector.verify(self.query,retry_unavailable=True)
        self.assertEqual((result['verified'],result['gid']),(True,4))
        self.assertEqual(self.calls,[4,3,2,1,4])
        with self.catalog.favorites.connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM collection_failures WHERE gid=4').fetchone())

    def test_saved_task_verification_does_not_repeat_failures_or_replace_queue(self):
        with closing(sqlite3.connect(self.catalog.path)) as db, db:
            for gid in range(5,8):
                db.execute("INSERT INTO gallery VALUES (?,'abcdef0123','Test book','','Non-H',?,10,'4.0',0,0,0)",(gid,gid))
                db.execute('INSERT INTO gid_tid VALUES (?,1)',(gid,))
        self.ready()
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('pause'))
        self.start();self.finish()
        job=self.collector.store.latest();self.calls=[]
        def fetch(settings,gid,token):
            self.calls.append(gid)
            if gid>=4:raise GalleryUnavailable('test unavailable')
            return gid*10
        self.collector.fetcher=fetch;self.collector.last_request=0
        with self.assertRaisesRegex(ValueError,'3 条作品均不可访问'):
            self.collector.verify(None)
        self.collector.last_request=0
        result=self.collector.verify(None)
        self.assertEqual((result['verified'],self.calls),(True,[7,6,5,4,3]))
        updated=self.collector.store.latest()
        self.assertEqual((updated['id'],updated['state'],updated['total']),(job['id'],'paused',7))
        self.assertEqual(updated['failed'],3)
        self.resume();self.finish()
        self.assertEqual(self.collector.store.latest()['state'],'completed_with_errors')
        with self.catalog.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=7').fetchone()[0],70)

    def test_only_access_changes_reset_verification_failures(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.settings['interval']=0.00001
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(GalleryUnavailable('test unavailable'))
        with self.assertRaises(ValueError):self.collector.verify(self.query)
        self.collector.configure({'host':'e-hentai.org','interval':2.5,'collection_mode':'rounds','skip_existing':True})
        self.assertEqual(self.collector.status()['verification_unavailable'],3)
        self.collector.settings['interval']=0.00001
        self.collector.fetcher=self.fetch;self.collector.last_request=0
        self.assertEqual(self.collector.verify(self.query)['gid'],1)
        self.collector.configure({'host':'e-hentai.org','proxy':'http://127.0.0.1:7890'})
        self.assertEqual(self.collector.status()['verification_unavailable'],0)
        self.collector.last_request=0
        result=self.collector.verify(self.query)
        self.assertEqual(result['gid'],1)
        self.assertTrue(result['verification_only'])

    def test_retry_option_validation_does_not_clear_failures(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.verification_unavailable.add(4)
        with self.assertRaisesRegex(ValueError,'开关值'):
            self.collector.verify(self.query,retry_unavailable='false')
        self.assertEqual(self.collector.verification_unavailable,{4})
        self.assertEqual(self.calls,[])

    def test_many_previous_verification_failures_do_not_hit_sql_parameter_limit(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.verification_unavailable.update(range(4,40005))
        self.assertEqual(self.collector.verify(self.query)['gid'],3)

    def test_saved_round_verification_exhaustion_does_not_open_future_rounds(self):
        self.collector.configure({'host':'e-hentai.org','collection_mode':'rounds','batch_size':1})
        self.collector.verified=True;self.collector.settings['interval']=0.00001
        self.collector.fetcher=lambda *args:(_ for _ in ()).throw(CollectionPaused('pause'))
        self.start();self.finish()
        job=self.collector.store.latest();self.calls=[]
        def unavailable(settings,gid,token):
            self.calls.append(gid)
            raise GalleryUnavailable('test unavailable')
        self.collector.fetcher=unavailable;self.collector.last_request=0
        with self.assertRaisesRegex(ValueError,'没有其他可验证作品'):
            self.collector.verify(None)
        with self.assertRaisesRegex(ValueError,'本次未向源站发送请求'):
            self.collector.verify(None)
        self.assertEqual(self.calls,[4])
        updated=self.collector.store.latest()
        self.assertEqual((updated['id'],updated['current_round'],updated['pending']),(job['id'],1,3))

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

    def test_cooldown_recovery_clears_persistent_limits_without_resuming_queue(self):
        self.ready()
        def limited(*args):raise CollectionPaused('Test rate limit',3600)
        self.collector.fetcher=limited
        self.start();self.finish()
        self.assertFalse(self.collector.verified)
        self.collector.fetcher=self.fetch
        before=self.collector.store.latest()
        self.collector.last_request=0
        with self.assertRaisesRegex(ValueError,'冷却'):
            self.collector.verify(None)
        result=self.collector.verify(None,recover_cooldown=True)
        self.assertTrue(result['cooldown_cleared'])
        self.assertTrue(result['verification_only'])
        after=self.collector.store.latest()
        self.assertEqual((after['id'],after['pending'],after['state']),(before['id'],before['pending'],'paused'))
        self.assertEqual(after['cooldown_until'],0)
        self.assertIn('冷却已解除',after['message'])
        self.collector.close()
        self.collector=Collector(self.catalog,self.fetch)
        self.assertEqual(self.collector.status()['cooldown_until'],0)
        self.assertEqual(self.collector.status()['cooldown_probe_after'],0)

    def test_failed_recovery_is_one_request_preserves_cooldown_and_throttles_after_restart(self):
        self.collector.configure({'host':'e-hentai.org'})
        until=int(time.time()+3600)
        for error in (CollectionPaused('test login'),CollectionPaused('test 429',60),GalleryUnavailable('test removed'),GalleryAccessUncertain('test unavailable'),RetryableFetch('test network')):
            with self.subTest(error=type(error).__name__):
                self.collector.cooldown_until=until
                self.collector.cooldown_probe_after=0
                self.collector.last_request=0
                self.collector.verification_unavailable.clear()
                self.collector.verification_issues.clear()
                self.calls=[]
                def fail(*args):
                    self.calls.append(args[1])
                    raise error
                self.collector.fetcher=fail
                with self.assertRaises(ValueError):self.collector.verify(self.query,recover_cooldown=True)
                self.assertEqual(len(self.calls),1)
                self.assertGreaterEqual(self.collector.cooldown_until,until)
                self.assertFalse(self.collector.verified)
                with self.assertRaisesRegex(ValueError,'每分钟'):
                    self.collector.verify(self.query,recover_cooldown=True)
                self.assertEqual(len(self.calls),1)
                self.collector.store.remember_cooldown(until)
                self.collector.close()
                self.collector=Collector(self.catalog,self.fetch)
                self.collector.configure({'host':'e-hentai.org'})
                with self.assertRaisesRegex(ValueError,'每分钟'):
                    self.collector.verify(self.query,recover_cooldown=True)
                self.assertEqual(len(self.calls),1)

    def test_recovery_without_work_or_invalid_flag_never_clears_cooldown(self):
        self.collector.configure({'host':'e-hentai.org'})
        self.collector.cooldown_until=int(time.time()+3600)
        with self.assertRaisesRegex(ValueError,'开关'):
            self.collector.verify(self.query,recover_cooldown='true')
        for gid in range(1,5):self.collector.store.save(gid,10,'e-hentai.org')
        result=self.collector.verify(self.query,recover_cooldown=True)
        self.assertTrue(result['no_request'])
        self.assertFalse(result['verified'])
        self.assertEqual(self.calls,[])
        self.assertGreater(self.collector.cooldown_until,time.time())

    def test_cookie_stays_in_memory_and_settings_validation(self):
        self.ready()
        status=json.dumps(self.collector.status())
        self.assertNotIn('test-secret-hash',status)
        self.assertNotIn('test-igneous',status)
        self.collector.configure({'host':'exhentai.org','interval':2.5})
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
