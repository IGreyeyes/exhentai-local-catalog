"""Disposable type-filter data and a simulated source for interval persistence checks."""
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
    def __init__(self, catalog):
        super().__init__(catalog, self.fetch)

    @staticmethod
    def fetch(settings, gid, token):
        time.sleep(0.05)
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
        db.execute("UPDATE gallery SET category='Manga',title='类型筛选示例 1'")
        for gid in range(2, 98):
            category = 'Manga' if gid <= 60 else 'Non-H' if gid <= 90 else 'Artist CG'
            db.execute("INSERT INTO gallery VALUES(?,'abcdef0123',?,'',?,?,10,'4.0',0,0,0)", (gid, f'类型筛选示例 {gid}', category, gid))
            db.execute('INSERT INTO gid_tid VALUES(?,1)', (gid,))
    catalog = Catalog(database)
    for gid in range(2, 97):
        catalog.favorites.save(gid, gid * 10, 'e-hentai.org')
else:
    catalog = Catalog(database)
server = Server(('127.0.0.1', 0), catalog)
server.collector = FixtureCollector(catalog)
server.collector.configure({'host': 'e-hentai.org', 'collection_mode': 'full'})
print(f'http://127.0.0.1:{server.server_port}', flush=True)
try:
    server.serve_forever()
finally:
    server.collector.close()
    server.server_close()
