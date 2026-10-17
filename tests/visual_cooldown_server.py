"""A disposable paused queue and simulated source for cooldown recovery checks."""
from pathlib import Path
import json
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from favorites import parse_gallery_counts
from test_desktop import make_library
from visual_verification_skip_server import FixtureCollector


class CooldownFixture(FixtureCollector):
    def fetch(self,settings,gid,token):
        self.requests.append(gid)
        if sys.argv[2]=='limited':
            return parse_gallery_counts('<h1>Too many requests</h1>',gid)
        return parse_gallery_counts(f'<script>var gid={gid};</script><td id="favcount">120 times</td><p>A user comment: Too many requests; temporarily banned.</p>',gid)


catalog=Catalog(make_library(Path(sys.argv[1])))
with sqlite3.connect(catalog.path) as db:
    for gid in (2,3,4):
        db.execute("INSERT INTO gallery VALUES(?,'abcdef0123','冷却恢复示例','','Non-H',?,10,'4.0',0,0,0)",(gid,gid))
        db.execute('INSERT INTO gid_tid VALUES(?,1)',(gid,))
with catalog.favorites.connection() as db:
    now=int(time.time())
    db.execute("INSERT INTO jobs(id,query_json,host,state,total,created_at,updated_at,message,cooldown_until) VALUES(1,?,'e-hentai.org','paused',3,?,?,'隔离测试：源站提示请求受限，已暂停。',?)",(json.dumps({'tag':['language:english']}),now,now,now+3600))
    db.executemany("INSERT INTO tasks(job_id,gid,token) VALUES(1,?,'abcdef0123')",((gid,) for gid in (2,3,4)))
server=Server(('127.0.0.1',0),catalog)
server.collector=CooldownFixture(catalog)
server.collector.configure({'host':'e-hentai.org'})
print(f'http://127.0.0.1:{server.server_port}',flush=True)
try:server.serve_forever()
finally:server.server_close()
