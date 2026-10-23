"""Disposable library and simulated requests for search and task recovery checks."""
from pathlib import Path
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from favorites import Collector
from test_desktop import make_library


class FixtureCollector(Collector):
    @staticmethod
    def fetch(settings, gid, token):
        time.sleep(0.3)
        return gid * 10

    def _launch(self, job_id):
        self.interrupt.clear()
        self.thread = threading.Thread(target=self._run, args=(job_id, {**self.settings, 'interval': 0.001}), daemon=True)
        self.thread.start()


root = Path(sys.argv[1])
database = root / 'data/catalog.sqlite3'
if not database.exists():
    make_library(root)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO tag VALUES(2,'other:sample')")
        db.execute('INSERT INTO gid_tid VALUES(1,2)')
        for gid in range(2, 98):
            db.execute("INSERT INTO gallery VALUES(?,'abcdef0123',?,'','Manga',?,10,'4.0',0,0,0)", (gid, f'搜索恢复示例 {gid}', gid))
            db.execute('INSERT INTO gid_tid VALUES(?,?)', (gid, 1 if gid <= 80 else 2))
catalog = Catalog(database)
server = Server(('127.0.0.1', 0), catalog)
server.collector = FixtureCollector(catalog, FixtureCollector.fetch)
server.collector.configure({'host': 'e-hentai.org'})
print(f'http://127.0.0.1:{server.server_port}', flush=True)
try:
    server.serve_forever()
finally:
    server.collector.close()
    server.server_close()
