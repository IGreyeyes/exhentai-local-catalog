from contextlib import closing
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
from favorites import FavoriteStore


class RecordedWorksTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'catalog.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE gallery (gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag (id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid (gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english'),(2,'other:anthology'),(3,'language:chinese');
                INSERT INTO gallery VALUES
                (1,'abcdef0123','First book','第一冊','Non-H',100,10,'4.1',0,0,0),
                (2,'abcdef0123','100%_ literal book','第二冊','Non-H',200,20,'4.9',0,0,0),
                (3,'abcdef0123','Old version','旧版','Non-H',300,30,'5.0',0,1,0),
                (4,'abcdef0123','Removed title','已移除','Manga',400,40,'3.5',1,0,1),
                (5,'abcdef0123','Never collected','未采集','Manga',500,50,'4.0',0,0,0);
                INSERT INTO gid_tid VALUES(1,1),(1,2),(2,1),(3,1),(3,2),(4,3),(5,3);
            """)
            db.commit()
        translations={'version':7,'data':[
            {'namespace':'rows','data':{'language':{'name':'语言'},'other':{'name':'其他'}}},
            {'namespace':'language','data':{'english':{'name':'英语'},'chinese':{'name':'汉语'}}},
            {'namespace':'other','data':{'anthology':{'name':'选集'}}},
        ]}
        self.path.with_name('tag-translations.json').write_text(json.dumps(translations),encoding='utf-8')
        self.catalog=Catalog(self.path)
        self.now=int(time.time())
        records=[(1,0,self.now-5,'exhentai.org'),(2,20,self.now-10,'e-hentai.org'),(3,100,self.now-31*86400,'exhentai.org'),(4,7,self.now-20,'exhentai.org'),(99,55,self.now-90*86400,'e-hentai.org')]
        with self.catalog.favorites.connection() as db:db.executemany('INSERT INTO favorites(gid,favorite_count,checked_at,source) VALUES(?,?,?,?)',records)

    def tearDown(self):self.directory.cleanup()

    def test_view_choice_survives_reopening_and_preserves_other_personal_data(self):
        self.assertIsNone(self.catalog.preferences()['records_view'])
        self.assertIsNone(self.catalog.preferences()['search_view'])
        self.catalog.favorites.set_tag_blacklist(['other:anthology'])
        self.catalog.favorites.set_reading_state([1],'watched')
        self.catalog.favorites.set_record_view('thumbnails')
        self.catalog.favorites.set_search_view('compact')
        reopened=Catalog(self.path)
        self.assertEqual(reopened.preferences()['records_view'],'thumbnails')
        self.assertEqual(reopened.preferences()['search_view'],'compact')
        self.assertEqual(reopened.preferences()['tag_blacklist'],['other:anthology'])
        saved=next(item for item in reopened.recorded({})['items'] if item['gid']==1)
        self.assertEqual(saved['reading_state'],'watched')
        self.assertEqual(reopened.recorded({})['summary']['total'],5)
        for invalid in ('minimal-tags','invalid','',None,True,[],{}):
            with self.subTest(view=invalid),self.assertRaises(ValueError):
                reopened.favorites.set_record_view(invalid)
            with self.subTest(search_view=invalid),self.assertRaises(ValueError):
                reopened.favorites.set_search_view(invalid)
        self.assertEqual(reopened.favorites.get_record_view(),'thumbnails')
        self.assertEqual(reopened.favorites.get_search_view(),'compact')
        reopened.set_preferences({'tag_blacklist':[]})
        self.assertEqual(Catalog(self.path).preferences()['records_view'],'thumbnails')

    def query(self,**options):
        return self.catalog.recorded({key:value if isinstance(value,list) else [str(value)] for key,value in options.items()})

    def test_default_includes_only_saved_and_keeps_old_removed_missing_records(self):
        result=self.query()
        self.assertEqual(result['total'],5)
        self.assertEqual([item['gid'] for item in result['items']],[3,99,2,4,1])
        self.assertEqual(result['summary'],{'total':5,'recent_7_days':3,'last_checked_at':self.now-5,'missing_metadata':1,'opened':0,'states':{'none':5,'planned':0,'reading':0,'watched':0,'ignored':0},'successful':5,'failed':0,'last_recorded_at':self.now-5})
        missing=next(item for item in result['items'] if item['gid']==99)
        self.assertFalse(missing['metadata_available'])
        self.assertIsNone(missing['source_url'])
        self.assertEqual(missing['favorite_count'],55)
        self.assertEqual(missing['tags'],[])
        self.assertEqual((missing['reading_state'],missing['opened_count']),('none',0))

    def test_favorite_sort_keeps_zero_and_pagination_has_no_duplicates(self):
        first=self.query(sort='favorites_desc',limit=2)
        second=self.query(sort='favorites_desc',limit=2,page=2)
        last=self.query(sort='favorites_desc',limit=2,page=999)
        ids=[item['gid'] for page in (first,second,last) for item in page['items']]
        self.assertEqual(ids,[3,99,2,4,1])
        self.assertEqual(last['items'][0]['favorite_count'],0)
        self.assertEqual(last['page'],3)
        self.assertEqual(self.query(sort='favorites_asc')['items'][0]['gid'],1)
        self.assertEqual(self.query(sort='recorded_asc')['items'][0]['gid'],99)

    def test_chinese_tags_intersect_without_hiding_old_versions(self):
        result=self.query(tag=['英语','合集'])
        self.assertEqual(result['total'],2)
        self.assertEqual({item['gid'] for item in result['items']},{1,3})
        self.assertEqual(result['tag_labels']['language:english']['name'],'英语')
        self.assertEqual(result['summary']['total'],5)
        with self.assertRaises(ValueError):self.query(tag=['language:english','missing:tag'])

    def test_id_titles_source_and_time_filters(self):
        self.assertEqual(self.query(q='99')['items'][0]['gid'],99)
        self.assertEqual(self.query(q='第一冊')['total'],1)
        self.assertEqual(self.query(q='%_')['items'][0]['gid'],2)
        self.assertEqual(self.query(source='e-hentai.org')['total'],2)
        self.assertEqual(self.query(age='7d')['total'],3)
        self.assertEqual(self.query(age='older30')['total'],2)
        self.assertEqual(self.query(q='book',source='e-hentai.org',age='7d')['total'],1)

    def test_no_records_and_no_filter_matches_are_distinct(self):
        filtered=self.query(q='not present')
        self.assertEqual(filtered['total'],0)
        self.assertEqual(filtered['summary']['total'],5)
        with self.catalog.favorites.connection() as db:db.execute('DELETE FROM favorites')
        empty=self.query()
        self.assertEqual(empty['items'],[])
        self.assertEqual(empty['pages'],1)
        self.assertEqual(empty['summary']['total'],0)
        self.assertIsNone(empty['summary']['last_checked_at'])
        self.assertEqual(empty['summary']['states'],{'none':0,'planned':0,'reading':0,'watched':0,'ignored':0})

    def test_refresh_reflects_latest_saved_value_without_duplicate_history(self):
        self.query()
        self.catalog.favorites.save(2,1000,'e-hentai.org')
        result=self.query(sort='favorites_desc')
        self.assertEqual(result['total'],5)
        self.assertEqual(result['items'][0]['gid'],2)
        self.assertEqual(result['items'][0]['favorite_count'],1000)
        self.assertEqual(result['items'][0]['source_url'],'https://e-hentai.org/g/2/abcdef0123/')

    def test_input_validation_and_literal_search(self):
        for options in ({'source':'other.example'},{'age':'-1'},{'sort':'favorite_count; DROP TABLE favorites'},{'page':'bad'},{'q':'x'*201}):
            with self.assertRaises(ValueError):self.query(**options)
        self.assertEqual(self.query(q="%' OR 1=1 --")['total'],0)
        self.assertEqual(self.query(q='99999999999999999999999999999999')['total'],0)

    def test_opened_and_manual_state_are_independent(self):
        first=self.catalog.favorites.mark_opened(1)
        second=self.catalog.favorites.mark_opened(1)
        self.assertEqual((first['state'],first['opened_count']),('none',1))
        self.assertEqual((second['state'],second['opened_count']),('none',2))
        changed=self.catalog.favorites.set_reading_state([1],'planned')
        self.assertEqual(changed['items'][0]['opened_count'],2)
        self.assertEqual(self.query(opened='yes')['items'][0]['gid'],1)
        self.assertEqual(self.query(state='planned')['items'][0]['gid'],1)
        self.assertEqual(self.query(opened='no')['total'],4)
        self.assertEqual(self.query()['summary']['opened'],1)
        self.assertEqual(self.query()['summary']['states']['planned'],1)
        self.catalog.favorites.save(1,999,'e-hentai.org')
        item=self.query(q='1')['items'][0]
        self.assertEqual((item['favorite_count'],item['reading_state'],item['opened_count']),(999,'planned',2))

    def test_all_states_bulk_update_and_reset(self):
        for gid,state in ((1,'planned'),(2,'reading'),(3,'watched'),(4,'ignored'),(99,'none')):
            self.catalog.favorites.set_reading_state([gid],state)
        all_records=self.query(sort='state_updated_desc')
        self.assertEqual(all_records['summary']['states'],{'none':1,'planned':1,'reading':1,'watched':1,'ignored':1})
        for state,gid in (('planned',1),('reading',2),('watched',3),('ignored',4),('none',99)):
            result=self.query(state=state)
            self.assertEqual((result['total'],result['items'][0]['gid']),(1,gid))
        changed=self.catalog.favorites.set_reading_state([1,2,3,4,99],'watched')
        self.assertEqual(changed['updated'],5)
        self.assertEqual(self.query(state='watched')['total'],5)
        self.catalog.favorites.set_reading_state([1,2],'none')
        self.assertEqual(self.query(state='none')['total'],2)

    def test_state_and_open_validation(self):
        for options in ({'state':'later'},{'opened':'maybe'}):
            with self.assertRaises(ValueError):self.query(**options)
        for state in ('later',''):
            with self.assertRaises(ValueError):self.catalog.favorites.set_reading_state([1],state)
        for gids in ([],list(range(1,1002)),[True],[0],['1'],[123456]):
            with self.assertRaises(ValueError):self.catalog.favorites.set_reading_state(gids,'watched')
        for gid in (True,0,'1',123456):
            with self.assertRaises(ValueError):self.catalog.favorites.mark_opened(gid)

    def test_existing_favorites_database_migrates_without_losing_counts(self):
        path=Path(self.directory.name)/'legacy-favorites.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE favorites(gid INTEGER PRIMARY KEY,favorite_count INTEGER NOT NULL,checked_at INTEGER NOT NULL,source TEXT NOT NULL)')
            db.execute("INSERT INTO favorites(gid,favorite_count,checked_at,source) VALUES(7,88,1,'e-hentai.org')");db.commit()
        store=FavoriteStore(path)
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=7').fetchone()[0],88)
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='record_status'").fetchone())
        store.set_reading_state([7],'planned')
        self.assertEqual(store.mark_opened(7)['state'],'planned')

    def failure(self,gid,job_id=42,host='exhentai.org',token='abcdef0123',message='作品不可访问（HTTP 404）。'):
        with self.catalog.favorites.connection() as db:
            self.catalog.favorites._save_failure(db,gid,token,host,message,3,job_id)

    def test_failures_and_successes_are_deduplicated_and_unknown_counts_stay_last(self):
        self.failure(5)
        self.failure(2)
        result=self.query()
        self.assertEqual((result['total'],result['summary']['successful'],result['summary']['failed']),(6,4,2))
        self.assertEqual(len({item['gid'] for item in result['items']}),6)
        unknown=self.query(q='5')['items'][0]
        self.assertIsNone(unknown['favorite_count'])
        self.assertFalse(unknown['has_saved_count'])
        self.assertEqual((unknown['collection_status'],unknown['failure_attempts']),('failed',3))
        self.assertEqual(unknown['source_url'],'https://exhentai.org/g/5/abcdef0123/')
        self.assertEqual(self.query(sort='favorites_asc')['items'][-1]['gid'],5)
        old=self.query(q='2')['items'][0]
        self.assertEqual((old['favorite_count'],old['collection_status'],old['last_success_source']),(20,'failed','e-hentai.org'))
        self.assertEqual(self.query(collection='success')['total'],4)

    def test_failure_filters_keep_tags_source_time_and_original_task(self):
        self.failure(5,job_id=42)
        self.failure(2,job_id=43,host='e-hentai.org')
        with self.catalog.favorites.connection() as db:db.execute('UPDATE collection_failures SET failed_at=1 WHERE gid=2')
        self.assertEqual(self.query(collection='failed',tag=['汉语'])['items'][0]['gid'],5)
        self.assertEqual(self.query(collection='failed',q='Never')['total'],1)
        self.assertEqual(self.query(collection='failed',source='e-hentai.org')['items'][0]['gid'],2)
        self.assertEqual(self.query(collection='failed',age='7d')['items'][0]['gid'],5)
        self.assertEqual(self.query(collection='failed',age='older30')['items'][0]['gid'],2)
        self.assertEqual(self.query(collection='failed',job_id=42)['total'],1)
        self.assertEqual(self.query(collection='failed',job_id=999)['total'],0)
        for options in ({'collection':'bad'},{'job_id':'abc'},{'job_id':'0'},{'job_id':'9223372036854775808'}):
            with self.assertRaises(ValueError):self.query(**options)

    def test_missing_metadata_failure_keeps_attempt_url_and_links_are_validated(self):
        self.failure(123456)
        item=self.query(q='123456')['items'][0]
        self.assertFalse(item['metadata_available'])
        self.assertEqual(item['source_url'],'https://exhentai.org/g/123456/abcdef0123/')
        self.assertEqual(item['tags'],[])
        self.failure(123457,host='invalid.example')
        self.failure(123458,token='../../bad')
        self.assertIsNone(self.query(q='123457')['items'][0]['source_url'])
        self.assertIsNone(self.query(q='123458')['items'][0]['source_url'])

    def test_failed_only_work_supports_open_and_reading_state_and_success_clears_failure(self):
        self.failure(5)
        self.catalog.favorites.mark_opened(5)
        self.catalog.favorites.set_reading_state([5],'planned')
        item=self.query(collection='failed',opened='yes',state='planned')['items'][0]
        self.assertEqual((item['gid'],item['opened_count']),(5,1))
        self.catalog.favorites.save(5,0,'exhentai.org')
        self.assertEqual(self.query(collection='failed')['total'],0)
        item=self.query(q='5')['items'][0]
        self.assertEqual((item['collection_status'],item['favorite_count'],item['reading_state'],item['opened_count']),('success',0,'planned',1))
        self.assertEqual(self.query()['total'],6)

    def test_failed_refresh_does_not_erase_successful_count(self):
        self.failure(2)
        self.failure(2,job_id=43,message='再次网络失败')
        result=self.query(collection='failed')
        self.assertEqual((result['total'],result['items'][0]['favorite_count'],result['items'][0]['job_id']),(1,20,43))
        self.catalog.favorites.save(2,99,'e-hentai.org')
        self.assertEqual(self.query(collection='failed')['total'],0)
        self.assertEqual(self.query(q='2')['items'][0]['favorite_count'],99)

    def test_old_failed_queue_migration_keeps_latest_unresolved_failures(self):
        path=Path(self.directory.name)/'legacy-queue.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE favorites(gid INTEGER PRIMARY KEY,favorite_count INTEGER NOT NULL,checked_at INTEGER NOT NULL,source TEXT NOT NULL);
                CREATE TABLE jobs(id INTEGER PRIMARY KEY,query_json TEXT NOT NULL,host TEXT NOT NULL,state TEXT NOT NULL,total INTEGER NOT NULL DEFAULT 0,cached INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,message TEXT NOT NULL DEFAULT '',cooldown_until INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE tasks(job_id INTEGER NOT NULL,gid INTEGER NOT NULL,token TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(job_id,gid));
                INSERT INTO jobs(id,query_json,host,state,created_at,updated_at) VALUES(1,'{}','exhentai.org','completed_with_errors',1,10),(2,'{}','exhentai.org','completed_with_errors',20,30);
                INSERT INTO tasks VALUES(1,7,'abcdef0123','failed',3,'old error'),(1,8,'abcdef0123','failed',3,'resolved error'),(1,9,'abcdef0123','failed',3,'verified later'),(2,7,'abcdef0123','failed',3,'latest error'),(2,8,'abcdef0123','done',1,'');
                INSERT INTO favorites VALUES(9,0,40,'exhentai.org');
            """)
        store=FavoriteStore(path)
        with store.connection() as db:
            rows=db.execute('SELECT gid,error FROM collection_failures').fetchall()
            self.assertEqual([tuple(row) for row in rows],[(7,'latest error')])
        store.save(7,0,'exhentai.org')
        store=FavoriteStore(path)
        with store.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],0)

    def test_routes_serve_independent_page_and_recorded_data(self):
        server=Server(('127.0.0.1',0),self.catalog)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        def post(path,payload,token=True):
            headers={'Content-Type':'application/json','Origin':root}
            if token:headers['X-Catalog-Token']=server.action_token
            request=Request(root+path,data=json.dumps(payload).encode(),headers=headers)
            with urlopen(request) as response:return json.load(response)
        try:
            with urlopen(root+'/records') as response:
                html=response.read().decode('utf-8')
                self.assertIn('已记录 · EX资料库',html)
                self.assertIn('/records.js',html)
            with urlopen(root+'/api/records?sort=favorites_desc&limit=1') as response:
                data=json.load(response)
                self.assertEqual(data['total'],5)
                self.assertEqual(data['items'][0]['gid'],3)
            with self.assertRaises(HTTPError) as failure:urlopen(root+'/api/records?age=invalid')
            self.assertEqual(failure.exception.code,400);failure.exception.close()
            with self.assertRaises(HTTPError) as failure:post('/api/records/opened',{'gid':1},token=False)
            self.assertEqual(failure.exception.code,403);failure.exception.close()
            with self.assertRaises(HTTPError) as failure:post('/api/records/view',{'view':'thumbnails'},token=False)
            self.assertEqual(failure.exception.code,403);failure.exception.close()
            self.assertEqual(post('/api/records/view',{'view':'thumbnails'})['records_view'],'thumbnails')
            with self.assertRaises(HTTPError) as failure:post('/api/search/view',{'view':'compact'},token=False)
            self.assertEqual(failure.exception.code,403);failure.exception.close()
            self.assertEqual(post('/api/search/view',{'view':'compact'})['search_view'],'compact')
            with urlopen(root+'/api/preferences') as response:
                self.assertEqual(json.load(response)['records_view'],'thumbnails')
            with urlopen(root+'/api/preferences') as response:
                self.assertEqual(json.load(response)['search_view'],'compact')
            for payload in ({'view':'bad'},{'view':'thumbnails','cookies':'test'}):
                with self.assertRaises(HTTPError) as failure:post('/api/search/view',payload)
                self.assertEqual(failure.exception.code,400);failure.exception.close()
                with self.assertRaises(HTTPError) as failure:post('/api/records/view',payload)
                self.assertEqual(failure.exception.code,400);failure.exception.close()
            opened=post('/api/records/opened',{'gid':1})
            self.assertEqual(opened['opened_count'],1)
            updated=post('/api/records/state',{'gids':[1,2],'state':'watched'})
            self.assertEqual(updated['updated'],2)
            with urlopen(root+'/api/records?state=watched&opened=yes') as response:
                marked=json.load(response)
                self.assertEqual((marked['total'],marked['items'][0]['gid']),(1,1))
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
