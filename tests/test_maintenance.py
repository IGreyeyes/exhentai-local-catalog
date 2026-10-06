from compression import zstd
from contextlib import closing, nullcontext, redirect_stdout
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import io
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from app import Catalog, Server
from initialize_catalog import initialize
from maintenance import LibraryMaintenance, MaintenanceManager, checkpoint, contained, digest_file, read_json, recover_import, save_json


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.data=self.root/'data';self.data.mkdir()
        self.catalog=self.data/'catalog.sqlite3'
        self.make_catalog(self.catalog,2,100)
        with redirect_stdout(io.StringIO()):initialize(self.catalog)
        (self.root/'app.py').write_text('# test application\n',encoding='utf-8')
        (self.root/'static').mkdir()
        (self.root/'static'/'app.js').write_text('// test page\n',encoding='utf-8')
        (self.root/'logs').mkdir()
        (self.root/'logs'/'server.log').write_text('not-in-backup',encoding='utf-8')
        (self.data/'tag-translations.json').write_text(json.dumps({'version':7,'data':[{'namespace':'language','data':{'english':{'name':'英语'}}}]}),encoding='utf-8')
        self.library=Catalog(self.catalog)
        self.library.favorites.save(1,37,'exhentai.org')
        self.engine=LibraryMaintenance(self.catalog,self.root)

    def tearDown(self):self.temporary.cleanup()

    def make_catalog(self,path,count,posted):
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE gallery (gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag (id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid (gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english');
            """)
            for gid in range(1,count+1):
                db.execute("INSERT INTO gallery(gid,token,title,title_jpn,category,posted,filecount,rating) VALUES (?,'abcdef0123','Test record','','Non-H',?,10,'4.0')",(gid,posted))
                db.execute("INSERT INTO gid_tid VALUES(?,1)",(gid,))
            db.commit()

    def archive(self,count=3,posted=200):
        path=self.root/f'incoming-{time.time_ns()}.sqlite3'
        self.make_catalog(path,count,posted)
        (self.root/'e-hentai.db.zstd').write_bytes(zstd.compress(path.read_bytes()))

    def prepare(self):
        with redirect_stdout(io.StringIO()):return self.engine.prepare_archive()

    def count(self,path=None):
        with closing(sqlite3.connect(path or self.catalog)) as db:return db.execute('SELECT COUNT(*) FROM gallery').fetchone()[0]

    def test_online_backup_includes_wal_and_is_restorable(self):
        self.library.favorites.set_reading_state([1],'watched')
        self.library.favorites.mark_opened(1)
        (self.data/'covers').mkdir()
        (self.data/'covers'/'regenerable.jpg').write_bytes(b'cover-cache')
        (self.data/'covers.sqlite3').write_bytes(b'cover-index')
        with closing(sqlite3.connect(self.data/'favorites.sqlite3')) as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("INSERT INTO favorites VALUES(2,99,1,'exhentai.org')");db.commit()
            self.assertTrue((self.data/'favorites.sqlite3-wal').exists())
            result=self.engine.backup()
        path=Path(result['path'])
        self.assertFalse((path/'data'/'catalog.sqlite3').exists())
        with closing(sqlite3.connect(path/'data'/'favorites.sqlite3')) as restored:
            self.assertEqual(restored.execute('SELECT gid,favorite_count FROM favorites ORDER BY gid').fetchall(),[(1,37),(2,99)])
            self.assertEqual(restored.execute('SELECT state,opened_count FROM record_status WHERE gid=1').fetchone(),('watched',1))
        manifest=read_json(path/'manifest.json')
        for item in manifest['files']:self.assertEqual(digest_file(path/item['path']),item['sha256'])
        self.assertFalse((path/'logs').exists())
        self.assertFalse((path/'data'/'covers').exists())
        self.assertFalse((path/'data'/'covers.sqlite3').exists())
        self.assertTrue((path/'app.py').exists())
        self.assertTrue((path/'static'/'app.js').exists())

    def test_full_backup_has_catalog_and_independent_database_files(self):
        result=self.engine.backup(True)
        path=Path(result['path'])
        self.assertEqual(self.count(path/'data'/'catalog.sqlite3'),2)
        self.assertTrue(read_json(path/'manifest.json')['include_catalog'])
        self.assertEqual(len(self.engine.list_backups()),1)

    def test_prepare_and_apply_preserve_favorites_and_translation(self):
        self.archive()
        translation=digest_file(self.data/'tag-translations.json')
        ready=self.prepare()
        self.assertEqual(ready['new']['gallery_count'],3)
        self.assertEqual(self.count(),2)
        reloads=[]
        result=self.engine.apply(ready['id'],reload_catalog=lambda:reloads.append(self.count()))
        self.assertEqual(reloads,[3])
        self.assertEqual(self.count(),3)
        self.assertEqual(digest_file(self.data/'tag-translations.json'),translation)
        with self.library.favorites.connection() as db:self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=1').fetchone()[0],37)
        self.assertEqual(self.count(Path(result['backup']['path'])/'data'/'catalog.sqlite3'),2)
        self.assertIsNone(self.engine.prepared())
        self.assertFalse((self.data/'catalog-swap.json').exists())
        self.assertTrue(self.prepare()['same_archive'])

    def test_bad_archive_or_checksum_never_changes_active_catalog(self):
        (self.root/'e-hentai.db.zstd').write_bytes(b'invalid-zstd')
        with self.assertRaises(Exception):self.prepare()
        self.assertEqual(self.count(),2)
        self.archive()
        with self.assertRaisesRegex(ValueError,'校验值不匹配'):self.engine.prepare_archive('0'*64)
        self.assertEqual(self.count(),2)
        self.assertEqual(self.engine.list_backups(),[])

    def test_reload_failure_rolls_back_catalog_and_metadata(self):
        self.archive();ready=self.prepare()
        calls=[]
        def reload():
            calls.append(self.count())
            if len(calls)==1:raise RuntimeError('simulated reload failure')
        with self.assertRaises(RuntimeError):self.engine.apply(ready['id'],reload_catalog=reload)
        self.assertEqual(calls,[3,2])
        self.assertEqual(self.count(),2)
        self.assertEqual(read_json(self.data/'catalog_info.json')['gallery_count'],2)
        self.assertFalse((self.data/'catalog-swap.json').exists())
        self.assertEqual(len(self.engine.list_backups()),1)

    def test_restart_recovers_an_interrupted_switch(self):
        stage=self.data/'.maintenance'/'interrupted-test';previous=stage/'previous';previous.mkdir(parents=True)
        self.catalog.rename(previous/'catalog.sqlite3')
        (self.data/'catalog_info.json').rename(previous/'catalog_info.json')
        self.make_catalog(self.catalog,3,200)
        save_json(self.data/'catalog_info.json',{'gallery_count':3})
        save_json(self.data/'catalog-swap.json',{'catalog':str(self.catalog),'stage':str(stage),'state':'installed','had_info':True})
        self.assertIsNotNone(recover_import(self.catalog))
        self.assertEqual(self.count(),2)
        self.assertEqual(read_json(self.data/'catalog_info.json')['gallery_count'],2)
        self.assertEqual(len(list(stage.glob('interrupted-*.sqlite3'))),1)

    def test_older_snapshot_requires_explicit_choice(self):
        self.archive(count=1,posted=50);ready=self.prepare()
        self.assertTrue(ready['older']);self.assertTrue(ready['fewer_records'])
        with self.assertRaisesRegex(ValueError,'较旧'):self.engine.apply(ready['id'])
        self.assertEqual(self.count(),2)
        self.assertEqual(self.engine.list_backups(),[])
        self.engine.apply(ready['id'],allow_older=True)
        self.assertEqual(self.count(),1)

    def test_prepared_catalog_tampering_is_rejected(self):
        self.archive();ready=self.prepare()
        path=Path(ready['stage'])/'catalog.sqlite3'
        with closing(sqlite3.connect(path)) as db:db.execute("UPDATE gallery SET title='changed'");db.commit()
        with self.assertRaisesRegex(ValueError,'发生变化'):self.engine.apply(ready['id'])
        self.assertEqual(self.count(),2)

    def test_auto_backup_is_opt_in_and_persists_choice(self):
        manager=MaintenanceManager(self.engine,nullcontext,lambda:None)
        self.assertFalse(manager.status()['settings']['backup_on_completion'])
        manager.on_collection_completed(1);self.assertEqual(self.engine.list_backups(),[])
        manager.configure(True);manager.on_collection_completed(1)
        manager.thread.join(timeout=5)
        self.assertFalse(manager.thread.is_alive())
        self.assertEqual(manager.status()['phase'],'completed')
        self.assertFalse(self.engine.list_backups()[0]['include_catalog'])
        manager.close()
        other=MaintenanceManager(self.engine,nullcontext,lambda:None)
        self.assertTrue(other.status()['settings']['backup_on_completion']);other.close()

    def test_paths_cannot_escape_workspace(self):
        with self.assertRaises(ValueError):contained(self.root.parent/'outside',self.root)
        save_json(self.data/'catalog-swap.json',{'catalog':str(self.catalog),'stage':str(self.root.parent),'state':'installed','had_info':True})
        with self.assertRaises(ValueError):recover_import(self.catalog)
        self.assertEqual(self.count(),2)

    def test_custom_backup_directory_persists_and_keeps_auto_backup_setting(self):
        with tempfile.TemporaryDirectory() as external:
            directory=Path(external)/'EX backup destination'
            manager=MaintenanceManager(self.engine,nullcontext,lambda:None)
            manager.configure(True)
            manager.configure(backup_directory=str(directory))
            self.assertTrue(manager.settings['backup_on_completion'])
            result=self.engine.backup()
            self.assertEqual(Path(result['path']).parent,directory)
            self.assertEqual(len(self.engine.list_backups()),1)
            other=LibraryMaintenance(self.catalog,self.root)
            self.assertEqual(other.backups,directory)
            manager.configure(False)
            self.assertEqual(self.engine.backups,directory)
            manager.configure(backup_directory='')
            self.assertEqual(self.engine.backups,self.root/'backups')
            self.assertEqual(self.engine.list_backups(),[])
            self.assertTrue(Path(result['path']).is_dir())
            for invalid in ('relative/path',str(self.data),str(self.data/'.maintenance')):
                with self.assertRaises(ValueError):manager.configure(backup_directory=invalid)
            manager.close()

    def test_restore_light_backup_restores_records_progress_and_blacklist_without_catalog(self):
        self.library.favorites.set_reading_state([1],'watched')
        self.library.favorites.mark_opened(1)
        self.library.favorites.set_tag_blacklist(['language:english'])
        with self.library.favorites.connection() as db:
            db.execute("INSERT INTO jobs(id,query_json,host,state,total,created_at,updated_at) VALUES(7,'{}','e-hentai.org','running',1,1,1)")
            db.execute("INSERT INTO tasks(job_id,gid,token) VALUES(7,2,'abcdef0123')")
        result=self.engine.backup()
        original_catalog=digest_file(self.catalog)
        original_translation=read_json(self.data/'tag-translations.json')
        self.library.favorites.save(1,999,'e-hentai.org')
        self.library.favorites.set_reading_state([1],'planned')
        self.library.favorites.set_tag_blacklist([])
        save_json(self.data/'tag-translations.json',{'data':[]})
        ready=self.engine.prepare_restore(result['path'])
        self.assertEqual(ready['summary'],{'favorites':1,'reading_states':1,'jobs':1})
        with self.library.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=1').fetchone()[0],999)
        restored=self.engine.restore(ready['id'],reload_catalog=lambda:Catalog(self.catalog))
        self.assertEqual(digest_file(self.catalog),original_catalog)
        self.assertEqual(read_json(self.data/'tag-translations.json'),original_translation)
        with self.library.favorites.connection() as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=1').fetchone()[0],37)
            self.assertEqual(tuple(db.execute('SELECT state,opened_count FROM record_status WHERE gid=1').fetchone()),('watched',1))
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=7').fetchone()[0],'paused')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks WHERE job_id=7').fetchone()[0],1)
        self.assertEqual(self.library.favorites.get_tag_blacklist(),['language:english'])
        with closing(sqlite3.connect(Path(restored['backup']['path'])/'data/favorites.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=1').fetchone()[0],999)
        self.assertIsNone(self.engine.prepared_restore())

    def test_restore_full_backup_can_replace_catalog_or_keep_current_catalog(self):
        result=self.engine.backup(True)
        self.archive();new=self.prepare();self.engine.apply(new['id'])
        self.assertEqual(self.count(),3)
        ready=self.engine.prepare_restore(result['path'])
        self.engine.restore(ready['id'],reload_catalog=lambda:Catalog(self.catalog))
        self.assertEqual(self.count(),3)
        ready=self.engine.prepare_restore(result['path'],include_catalog=True)
        restored=self.engine.restore(ready['id'],reload_catalog=lambda:Catalog(self.catalog))
        self.assertEqual(self.count(),2)
        self.assertEqual(read_json(self.data/'catalog_info.json')['gallery_count'],2)
        self.assertEqual(self.count(Path(restored['backup']['path'])/'data/catalog.sqlite3'),3)

    def test_restore_older_full_backup_without_catalog_metadata(self):
        (self.data/'catalog_info.json').unlink()
        backup=self.engine.backup(True)
        self.archive();new=self.prepare();self.engine.apply(new['id'])
        ready=self.engine.prepare_restore(backup['path'],True)
        self.engine.restore(ready['id'],reload_catalog=lambda:Catalog(self.catalog))
        self.assertEqual(self.count(),2)
        self.assertEqual(read_json(self.data/'catalog_info.json')['gallery_count'],2)

    def test_restore_rejects_corruption_unsafe_paths_and_incomplete_backups(self):
        result=self.engine.backup()
        folder=Path(result['path'])
        with self.assertRaisesRegex(ValueError,'没有作品目录'):self.engine.prepare_restore(str(folder),True)
        manifest=read_json(folder/'manifest.json')
        manifest['files'].append({'path':'../outside','size':0,'sha256':'0'*64})
        save_json(folder/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'不安全'):self.engine.prepare_restore(str(folder))
        manifest['files'].pop();save_json(folder/'manifest.json',manifest)
        (folder/'static/app.js').write_text('// changed',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'校验失败'):self.engine.prepare_restore(str(folder))
        self.assertEqual(self.count(),2)
        partial=folder.with_name(folder.name+'.partial');folder.rename(partial)
        with self.assertRaisesRegex(ValueError,'尚未完成'):self.engine.prepare_restore(str(partial))

    def test_restore_uses_verified_stage_and_rejects_stage_tampering(self):
        backup=self.engine.backup()
        ready=self.engine.prepare_restore(backup['path'])
        # A removable drive can disconnect after preparation: use the staged copy.
        Path(backup['path']).rename(Path(backup['path']).with_name('moved-backup'))
        self.library.favorites.save(1,555,'e-hentai.org')
        self.engine.restore(ready['id'])
        with self.library.favorites.connection() as db:self.assertEqual(db.execute('SELECT favorite_count FROM favorites WHERE gid=1').fetchone()[0],37)
        another=self.engine.backup()
        ready=self.engine.prepare_restore(another['path'])
        with closing(sqlite3.connect(Path(ready['stage'])/'incoming/favorites.sqlite3')) as db:
            db.execute('UPDATE favorites SET favorite_count=100');db.commit()
        with self.assertRaisesRegex(ValueError,'发生变化'):self.engine.restore(ready['id'])

    def test_restore_reload_failure_rolls_back_all_data(self):
        backup=self.engine.backup(True)
        self.library.favorites.save(1,555,'e-hentai.org')
        save_json(self.data/'tag-translations.json',{'data':[]})
        original_translation=digest_file(self.data/'tag-translations.json')
        ready=self.engine.prepare_restore(backup['path'],True)
        calls=[]
        def reload():
            library=Catalog(self.catalog)
            with library.favorites.connection() as db:calls.append(db.execute('SELECT favorite_count FROM favorites WHERE gid=1').fetchone()[0])
            if len(calls)==1:raise RuntimeError('restore reload failed')
        with self.assertRaisesRegex(RuntimeError,'restore reload failed'):self.engine.restore(ready['id'],reload)
        self.assertEqual(calls,[37,555])
        self.assertEqual(digest_file(self.data/'tag-translations.json'),original_translation)
        self.assertFalse((self.data/'restore-swap.json').exists())

    def test_restart_rolls_back_interrupted_restore_including_new_files(self):
        stage=self.data/'.maintenance/restore-crash'
        (stage/'incoming').mkdir(parents=True);(stage/'previous').mkdir()
        path=self.data/'favorites.sqlite3'
        checkpoint(path)
        original=digest_file(path)
        path.rename(stage/'previous/favorites.sqlite3')
        replacement=type(self.library.favorites)(path);replacement.save(1,222,'e-hentai.org')
        (self.data/'tag-translations-info.json').write_text('{}',encoding='utf-8')
        save_json(self.data/'restore-swap.json',{'stage':str(stage),'state':'planned','files':[{'name':'favorites.sqlite3','had_original':True},{'name':'tag-translations-info.json','had_original':False}]})
        self.assertIsNotNone(recover_import(self.catalog))
        self.assertEqual(digest_file(path),original)
        self.assertFalse((self.data/'tag-translations-info.json').exists())

    def test_directory_picker_handles_selection_cancellation_and_failure(self):
        from subprocess import CompletedProcess
        manager=MaintenanceManager(self.engine,nullcontext,lambda:None)
        for selected in ('',str(self.root/'backups')):
            with patch('maintenance.subprocess.run',return_value=CompletedProcess([],0,json.dumps({'path':selected}),'')) as run:
                self.assertEqual(manager.choose_directory('backup')['path'],selected)
                self.assertEqual(run.call_args.args[0][-1],'backup')
        with patch('maintenance.subprocess.run',return_value=CompletedProcess([],1,'','missing Tk')):
            with self.assertRaisesRegex(ValueError,'直接粘贴'):manager.choose_directory('restore')
        self.assertFalse(manager.picker_lock.locked())
        with self.assertRaises(ValueError):manager.choose_directory('unknown')
        manager.close()

    def test_web_maintenance_page_settings_and_restore(self):
        backup=self.engine.backup()
        server=Server(('127.0.0.1',0),self.library)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        def post(name,payload):
            request=Request(root+'/api/maintenance/'+name,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token,'Origin':root})
            with urlopen(request) as response:return json.load(response)
        try:
            for page in ('/maintenance','/maintenance/'):
                with urlopen(root+page) as response:self.assertIn('导入已有备份',response.read().decode())
            post('settings',{'backup_directory':str(self.root/'custom backups')})
            self.assertEqual(server.maintenance.engine.backups,self.root/'custom backups')
            self.library.favorites.save(1,555,'e-hentai.org')
            post('prepare-restore',{'path':backup['path']});server.maintenance.thread.join(timeout=10)
            ready=server.maintenance.status()['prepared_restore']
            self.assertIsNotNone(ready)
            post('restore',{'prepared_id':ready['id']});server.maintenance.thread.join(timeout=10)
            self.assertEqual(server.maintenance.status()['phase'],'completed')
            with urlopen(root+'/api/records') as response:self.assertEqual(json.load(response)['items'][0]['favorite_count'],37)
            self.assertIs(server.collector.store,server.catalog.favorites)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_busy_maintenance_blocks_stop_and_queued_auto_backup_keeps_task_history(self):
        manager=MaintenanceManager(self.engine,nullcontext,lambda:None)
        manager.configure(True)
        entered,release=threading.Event(),threading.Event()
        original=self.engine.backup
        def slow_backup(*args,**kwargs):
            entered.set();release.wait(timeout=3)
            return original(*args,**kwargs)
        self.engine.backup=slow_backup
        try:
            started=manager.start('backup')
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaisesRegex(ValueError,'维护任务'):manager.reserve_stop()
            manager.on_collection_completed(9)
            release.set()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if manager.thread:manager.thread.join(timeout=0.1)
                state=manager.status()
                if not state['busy'] and len(state['recent_tasks'])==2:break
            state=manager.status()
            self.assertEqual(len(state['recent_tasks']),2)
            self.assertEqual(state['recent_tasks'][0]['id'],started['requested_id'])
            self.assertEqual(state['recent_tasks'][0]['phase'],'completed')
            self.assertEqual(len(self.engine.list_backups()),2)
        finally:
            release.set();manager.close()

    def test_web_import_keeps_server_and_collector_session(self):
        self.archive()
        server=Server(('127.0.0.1',0),self.library)
        server.collector.configure({'host':'e-hentai.org'})
        server.collector.verified=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        def post(name,payload):
            request=Request(root+'/api/maintenance/'+name,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Catalog-Token':server.action_token,'Origin':root})
            with urlopen(request) as response:return json.load(response)
        try:
            with redirect_stdout(io.StringIO()):
                post('prepare',{});server.maintenance.thread.join(timeout=10)
            ready=server.maintenance.status()['prepared']
            self.assertIsNotNone(ready)
            self.assertEqual(server.catalog.status()['gallery_count'],2)
            post('apply',{'prepared_id':ready['id']});server.maintenance.thread.join(timeout=10)
            self.assertEqual(server.maintenance.status()['phase'],'completed')
            with urlopen(root+'/api/status') as response:self.assertEqual(json.load(response)['gallery_count'],3)
            self.assertTrue(server.collector.status()['configured'])
            self.assertTrue(server.collector.status()['verified'])
            self.assertEqual(server.catalog.search({'tag':['language:english']})['favorite_coverage']['known'],1)
            with self.assertRaises(HTTPError) as failure:
                urlopen(Request(root+'/api/maintenance/stop',data=b'{}',headers={'Content-Type':'application/json'}))
            self.assertEqual(failure.exception.code,403);failure.exception.close()
            self.assertTrue(post('stop',{})['stopping'])
            thread.join(timeout=3);self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():server.shutdown()
            server.server_close();thread.join()


if __name__=='__main__':unittest.main()
