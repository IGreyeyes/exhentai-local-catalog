"""Isolated catalog and mocked source for the collector browser regression test."""
from pathlib import Path
import sqlite3
import json
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from favorites import Collector


class MockCollector(Collector):
    def _launch(self,job_id):
        self.interrupt.clear()
        settings = {**self.settings,'interval':0.00001}
        self.thread = threading.Thread(target=self._run,args=(job_id,settings),daemon=True)
        self.thread.start()


def main():
    path = Path(sys.argv[1]) / 'catalog.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,thumb TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english');
            """)
            count=46 if len(sys.argv)>2 and sys.argv[2]=='search-views' else 10001
            db.executemany("INSERT INTO gallery(gid,token,title,title_jpn,category,posted,filecount,rating) VALUES (?,'abcdef0123','示例作品','','Non-H',1760000000,10,'4.0')",((gid,) for gid in range(1,count+1)))
            db.executemany('INSERT INTO gid_tid VALUES(?,1)',((gid,) for gid in range(1,count+1)))
    if len(sys.argv)>2 and sys.argv[2]=='search-views':
        with sqlite3.connect(path) as db:
            for gid,category in enumerate(('Doujinshi','Image Set','Artist CG','Manga','Game CG','Misc'),1):
                db.execute("UPDATE gallery SET category=?,title=?,title_jpn=? WHERE gid=?",(category,'示例作品 · '+category,'测试原标题',gid))
            for tid,tag in enumerate(['artist:example','parody:example','character:example',*[f'other:tag{index}' for index in range(12)]],2):
                db.execute("INSERT INTO tag VALUES(?,?)",(tid,tag))
                db.executemany("INSERT INTO gid_tid VALUES(?,?)",((gid,tid) for gid in range(1,7)))
        path.with_name('tag-translations.json').write_text(json.dumps({'version':7,'data':[
            {'namespace':'rows','data':{key:{'name':name} for key,name in [('language','语言'),('artist','画师'),('parody','原作'),('character','角色'),('other','其他')]}},
            {'namespace':'language','data':{'english':{'name':'英语'}}},
            {'namespace':'artist','data':{'example':{'name':'示例画师'}}},
        ]},ensure_ascii=False),encoding='utf-8')
    catalog = Catalog(path)
    if len(sys.argv)>2 and sys.argv[2]=='search-views':
        catalog.favorites.set_collector_name('测试采集者')
        for gid in range(1,7):catalog.favorites.save(gid,(gid-1)*1234,'e-hentai.org')
    if len(sys.argv)>2 and sys.argv[2]=='features':
        with sqlite3.connect(path) as db:
            db.execute("INSERT OR IGNORE INTO tag VALUES(2,'artist:example')")
            for gid,category in enumerate(('Doujinshi','Image Set','Artist CG','Manga','Game CG','Misc'),1):
                db.execute("UPDATE gallery SET category=?,title=? WHERE gid=?",(category,'示例作品 · '+category,gid))
                db.execute("INSERT OR IGNORE INTO gid_tid VALUES(?,2)",(gid,))
        for gid in range(1,7):catalog.favorites.save(gid,gid*1234,'e-hentai.org')
    if len(sys.argv)>2 and sys.argv[2]=='failures':
        for gid,count in ((1,842),(2,0),(3,288)):catalog.favorites.save(gid,count,'e-hentai.org')
        with catalog.favorites.connection() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,query_json,host,state,total,created_at,updated_at) VALUES (1,?,'e-hentai.org','completed_with_errors',7,1760000000,1760000000)",('{"tag":["language:english"]}',))
            for gid in (3,4,5,6,7,8,20000):
                message='网络连接失败，已尝试 3 次。' if gid==4 else '<img src=x onerror=alert(1)> 模拟错误' if gid==5 else '作品不可访问（HTTP 404）。'
                db.execute("INSERT OR REPLACE INTO tasks(job_id,gid,token,state,attempts,error) VALUES (1,?,'abcdef0123','failed',3,?)",(gid,message))
                catalog.favorites._save_failure(db,gid,'abcdef0123','e-hentai.org',message,3,1)
    server = Server(('127.0.0.1',0),catalog)
    server.collector = MockCollector(catalog,lambda settings,gid,token:gid%100)
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__ == '__main__':
    main()
