"""Failure snapshot migration across disposable libraries, with no source requests."""
from contextlib import closing
from pathlib import Path
from uuid import uuid4
import json
import sqlite3
import tempfile
import unittest

from app import Catalog
from favorite_transfer import FORMAT, FAILURE_REASONS, export_snapshot, merge_snapshot, parse_snapshot, preview_snapshot, snapshot_share_info
from maintenance import LibraryMaintenance
from test_desktop import make_library


class FailureTransferTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.database=make_library(self.root)
        self.catalog=Catalog(self.database)
        self.store=self.catalog.favorites
        self.engine=LibraryMaintenance(self.database,self.root)

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def success(gid,count=20,when=100,collector_id=''):
        return {'gid':gid,'favorite_count':count,'checked_at':when,'source':'e-hentai.org','collector_id':collector_id,'collector_name':''}

    @staticmethod
    def failure(gid,when=200,reason='network',**values):
        return {'gid':gid,'token':'abcdef0123','source':'e-hentai.org','failed_at':when,'attempts':3,'reason':reason,**values}

    @staticmethod
    def raw(records=(),failures=()):
        return json.dumps({'format':FORMAT,'version':3,'exported_at':'2026-10-01T00:00:00Z','records':list(records),'failures':list(failures)}).encode()

    @staticmethod
    def save(store,item):
        with store.connection() as db:
            db.execute('INSERT INTO favorites(gid,favorite_count,checked_at,source,collector_id,collector_name) VALUES(?,?,?,?,?,?) ON CONFLICT(gid) DO UPDATE SET favorite_count=excluded.favorite_count,checked_at=excluded.checked_at,source=excluded.source,collector_id=excluded.collector_id,collector_name=excluded.collector_name',tuple(item[key] for key in ('gid','favorite_count','checked_at','source','collector_id','collector_name')))

    def fail(self,gid,when=200,error='模拟连接失败',job_id=0):
        with self.store.connection() as db:
            self.store._save_failure(db,gid,'abcdef0123','e-hentai.org',error,3,job_id)
            db.execute('UPDATE collection_failures SET failed_at=? WHERE gid=?',(when,gid))

    def rows(self,table):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY gid')]

    def prepare(self,raw):
        return self.engine.prepare_favorites(self.engine.stage_favorites(raw),'test-failure-transfer')

    def test_export_between_computers_keeps_refresh_failures_and_missing_work_links(self):
        source_root=self.root/'source'
        source_root.mkdir()
        source=Catalog(make_library(source_root)).favorites
        identity=source.collector_identity()
        self.save(source,self.success(2,0,100,identity['collector_id']))
        with source.connection() as db:
            for gid,when in ((2,200),(999,210)):
                source._save_failure(db,gid,'abcdef0123','e-hentai.org','Connection failed through http://test-user:test-password@proxy.example.invalid',3,7)
                db.execute('UPDATE collection_failures SET failed_at=? WHERE gid=?',(when,gid))
        raw,count=export_snapshot(source.path)
        payload=parse_snapshot(raw)
        self.assertEqual((count,len(payload['failures'])),(2,2))
        self.assertNotIn(b'test-password',raw)
        self.assertNotIn(b'proxy.example.invalid',raw)
        self.assertNotIn(b'"error"',raw)
        self.assertNotIn(b'"job_id"',raw)
        self.assertTrue(all(item['reason']=='network' for item in payload['failures']))
        self.assertEqual(snapshot_share_info(raw)['oldest_failed_at'],200)
        ready=self.prepare(raw)
        self.assertEqual((ready['summary']['failures']['added'],ready['summary']['missing_catalog']),(2,2))
        result=self.engine.import_favorites(ready['id'])
        self.assertEqual(result['summary']['failures']['added'],2)
        with closing(sqlite3.connect(Path(result['backup']['path'])/'data/favorites.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_failures').fetchone()[0],0)
        refreshed=self.catalog.recorded({'collection':['failed']})
        self.assertEqual(refreshed['total'],2)
        item=next(item for item in refreshed['items'] if item['gid']==2)
        self.assertEqual((item['favorite_count'],item['collector_id'],item['failed_at']),(0,identity['collector_id'],200))
        missing=next(item for item in refreshed['items'] if item['gid']==999)
        self.assertFalse(missing['metadata_available'])
        self.assertEqual(missing['source_url'],'https://e-hentai.org/g/999/abcdef0123/')
        self.assertEqual(self.catalog.recorded({'q':['1']})['items'][0]['reading_state'],'watched')
        self.assertEqual(parse_snapshot(export_snapshot(self.store.path)[0])['failures'],payload['failures'])

    def test_failure_conflict_matrix_matches_read_only_preview_and_atomic_merge(self):
        for gid,when in ((2,300),(6,100),(7,100),(8,200)):
            self.save(self.store,self.success(gid,10,when))
        for gid,when in ((3,300),(4,100),(6,150),(9,200)):
            self.fail(gid,when,job_id=7)
        successes=[self.success(6,20,400),self.success(7,20,150)]
        failures=[self.failure(gid,when) for gid,when in ((2,200),(3,200),(4,200),(5,200),(6,350),(7,200),(8,200),(9,200))]
        before=self.rows('collection_failures')
        preview=preview_snapshot(self.store.path,self.database,successes,failures)
        self.assertEqual(self.rows('collection_failures'),before)
        self.assertEqual(preview['failures'],{'total':8,'added':2,'updated':1,'kept':5,'kept_newer_success':3,'kept_newer_or_equal_failure':2})
        merged=merge_snapshot(self.store.path,successes,failures)
        self.assertEqual({key:value for key,value in preview.items() if key!='missing_catalog'},merged)
        saved={item['gid']:item for item in self.rows('collection_failures')}
        self.assertEqual(set(saved),{3,4,5,7,9})
        self.assertEqual((saved[3]['failed_at'],saved[9]['failed_at']),(300,200))
        self.assertEqual((saved[4]['job_id'],saved[5]['job_id']),(7,0))
        counts={item['gid']:item['favorite_count'] for item in self.rows('favorites')}
        self.assertEqual((counts[6],counts[7],counts[2]),(20,20,10))

    def test_failures_only_file_and_repeated_import_keep_original_timestamp(self):
        raw=self.raw(failures=[self.failure(999,200)])
        self.assertEqual(parse_snapshot(raw)['records'],[])
        result=self.engine.import_favorites(self.prepare(raw)['id'])
        self.assertEqual((result['summary']['added'],result['summary']['failures']['added']),(0,1))
        before=self.rows('collection_failures')
        result=self.engine.import_favorites(self.prepare(raw)['id'])
        self.assertEqual(result['summary']['failures']['kept'],1)
        self.assertEqual(self.rows('collection_failures'),before)
        self.store.set_reading_state([999],'planned')
        self.store.mark_opened(999)
        reopened=Catalog(self.database).recorded({'q':['999']})['items'][0]
        self.assertEqual((reopened['failed_at'],reopened['reading_state'],reopened['opened_count']),(200,'planned',1))

    def test_recheck_newer_local_success_after_file_preview(self):
        ready=self.prepare(self.raw(failures=[self.failure(2,200)]))
        self.assertEqual(ready['summary']['failures']['added'],1)
        self.save(self.store,self.success(2,10,300))
        result=self.engine.import_favorites(ready['id'])
        self.assertEqual((result['summary']['failures']['added'],result['summary']['failures']['kept_newer_success']),(0,1))
        self.assertEqual(self.rows('collection_failures'),[])

    def test_bad_failure_rows_or_duplicate_ids_reject_whole_file(self):
        self.fail(5)
        before=self.rows('collection_failures')
        variants=[dict(self.failure(2),gid=True),self.failure(2,failed_at=0),self.failure(2,failed_at=10**20),self.failure(2,attempts=-1),self.failure(2,attempts=True),self.failure(2,source='example.invalid'),self.failure(2,token='https://example.invalid'),self.failure(2,reason='arbitrary error'),self.failure(2,error='test marker')]
        for item in variants:
            with self.subTest(item=item),self.assertRaises(ValueError):
                self.prepare(self.raw([self.success(3)], [item]))
            self.assertEqual(self.rows('collection_failures'),before)
            self.assertEqual(len(self.rows('favorites')),1)
        with self.assertRaisesRegex(ValueError,'作品 ID 重复'):
            self.prepare(self.raw(failures=[self.failure(2),self.failure(2)]))
        payload=json.loads(self.raw())
        payload['failures']={}
        with self.assertRaises(ValueError):parse_snapshot(json.dumps(payload).encode())

    def test_database_failure_rolls_back_successes_failure_cleanup_and_new_failures(self):
        self.save(self.store,self.success(2,10,100))
        self.fail(2,150)
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_failure_import BEFORE INSERT ON collection_failures WHEN NEW.gid=9 BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        before_success=self.rows('favorites')
        before_failed=self.rows('collection_failures')
        ready=self.prepare(self.raw([self.success(2,20,200)], [self.failure(9,250)]))
        with self.assertRaises(sqlite3.Error):self.engine.import_favorites(ready['id'])
        self.assertEqual(self.rows('favorites'),before_success)
        self.assertEqual(self.rows('collection_failures'),before_failed)
        self.assertEqual(self.engine.prepared_favorites()['id'],ready['id'])

    def test_legacy_files_keep_local_failures_and_new_files_need_the_failure_list(self):
        self.fail(3,200)
        for version in (1,2):
            item=self.success(2)
            if version==1:
                item.pop('collector_id');item.pop('collector_name')
            payload={'format':FORMAT,'version':version,'exported_at':'2026-10-01T00:00:00Z','records':[item]}
            parsed=parse_snapshot(json.dumps(payload).encode())
            self.assertEqual(parsed['failures'],[])
            self.engine.import_favorites(self.prepare(json.dumps(payload).encode())['id'])
        self.assertEqual(self.rows('collection_failures')[0]['gid'],3)
        payload=json.loads(self.raw())
        payload.pop('failures')
        with self.assertRaises(ValueError):parse_snapshot(json.dumps(payload).encode())

    def test_effective_success_rules_keep_combined_file_idempotent(self):
        self.save(self.store,self.success(2,100,300))
        successes=[self.success(2,200,100,str(uuid4()))]
        failures=[self.failure(2,200)]
        first=merge_snapshot(self.store.path,successes,failures)
        self.assertEqual((first['updated'],first['failures']['added']),(1,1))
        before_success=self.rows('favorites')
        before_failed=self.rows('collection_failures')
        second=merge_snapshot(self.store.path,successes,failures)
        self.assertEqual((second['updated'],second['failures']['kept']),(0,1))
        self.assertEqual(self.rows('favorites'),before_success)
        self.assertEqual(self.rows('collection_failures'),before_failed)

    def test_unknown_access_identifier_keeps_failure_and_known_catalog_links(self):
        self.fail(2,error='Unexpected response with test-private-marker')
        with self.store.connection() as db:
            db.execute("UPDATE collection_failures SET token='invalid token' WHERE gid=2")
        raw,_=export_snapshot(self.store.path)
        failure=parse_snapshot(raw)['failures'][0]
        self.assertEqual((failure['token'],failure['reason']),('','unknown'))
        self.assertNotIn(b'test-private-marker',raw)
        self.assertIn(failure['reason'],FAILURE_REASONS)

    def test_failure_only_export_metadata_does_not_invent_success_times(self):
        with self.store.connection() as db:db.execute('DELETE FROM favorites')
        self.fail(999,200)
        raw,count=export_snapshot(self.store.path)
        info=snapshot_share_info(raw)
        self.assertEqual((count,info['record_count'],info['failure_count']),(0,0,1))
        self.assertIsNone(info['oldest_checked_at'])
        self.assertIsNone(info['newest_checked_at'])
        self.assertEqual((info['oldest_failed_at'],info['newest_failed_at']),(200,200))


if __name__=='__main__':unittest.main()
