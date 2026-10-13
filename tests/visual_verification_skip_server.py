"""A saved collector queue with a simulated unavailable gallery, never a live source."""
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import io
import json
import sqlite3
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Catalog,Server
from favorites import Collector,fetch_favorites,parse_gallery_counts
from test_desktop import make_library


class FixtureCollector(Collector):
    def __init__(self,catalog):
        self.requests=[]
        super().__init__(catalog,self.fetch)

    def fetch(self,settings,gid,token):
        self.requests.append(gid)
        if gid==4:
            with patch('favorites.build_opener') as opener:
                opener.return_value.open.side_effect=HTTPError('https://e-hentai.org/g/4/abcdef0123/',404,'test unavailable',{},io.BytesIO(b'<h1>Gallery Not Available</h1>'))
                return fetch_favorites(settings,gid,token)
        return parse_gallery_counts(f'<script>var gid={gid};var notice="This gallery is not available";</script><td id="favcount">120 times</td><span id="rating_count">30</span>',gid)

    def status(self):
        with self.store.connection() as db:
            cached=dict(db.execute('SELECT * FROM favorites WHERE gid=1').fetchone())
        return {**super().status(),'fixture_requests':list(self.requests),'fixture_cached_snapshot':cached}

    def _verification_candidate(self,query,cutoff,excluded):
        with patch('favorites.secrets.randbelow',return_value=0):
            return super()._verification_candidate(query,cutoff,excluded)

    def _launch(self,job_id):
        self.interrupt.clear()
        self.thread=threading.Thread(target=self._run,args=(job_id,{**self.settings,'interval':0.00001}),daemon=True)
        self.thread.start()


def main():
    catalog=Catalog(make_library(Path(sys.argv[1])))
    with sqlite3.connect(catalog.path) as db:
        if '--cached-sample' not in sys.argv:
            db.execute('DELETE FROM gid_tid WHERE gid=1')
        for gid in (2,3,4):
            db.execute("INSERT INTO gallery VALUES(?,'abcdef0123','验证跳过示例','','Non-H',?,10,'4.0',0,0,0)",(gid,gid))
            db.execute('INSERT INTO gid_tid VALUES(?,1)',(gid,))
    with catalog.favorites.connection() as db:
        now=int(time.time())
        db.execute("INSERT INTO jobs(id,query_json,host,state,total,created_at,updated_at,message) VALUES(1,?,'e-hentai.org','paused',3,?,?,'隔离测试：原队列已暂停')",(json.dumps({'tag':['language:english']}),now,now))
        db.executemany("INSERT INTO tasks(job_id,gid,token) VALUES(1,?,'abcdef0123')",((gid,) for gid in (2,3,4)))
    server=Server(('127.0.0.1',0),catalog)
    server.collector=FixtureCollector(catalog)
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
