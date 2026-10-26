"""Small persistent fixture and simulated source for full-library collection."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from favorites import Collector
from test_desktop import make_library

root = Path(sys.argv[1])
database = root / 'data/catalog.sqlite3'
requests_file = root / 'requests.json'
requests = json.loads(requests_file.read_text()) if requests_file.exists() else []
if not database.exists():
    make_library(root)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO tag VALUES(2,'other:sample')")
        for gid in range(2, 13):
            db.execute("INSERT INTO gallery VALUES(?,'abcdef0123',?,'','Manga',?,24,?,0,0,0)",
                       (gid, f'评分全库示例 {gid}', gid, str(gid % 5 + 1)))
            db.execute('INSERT INTO gid_tid VALUES(?,?)', (gid, 1 if gid <= 7 else 2))


class FixtureCollector(Collector):
    @staticmethod
    def fetch(settings, gid, token):
        time.sleep(0.3)
        requests.append(gid)
        requests_file.write_text(json.dumps(requests), encoding='utf-8')
        return gid * 10

    def _launch(self, job_id):
        self.interrupt.clear()
        self.thread = threading.Thread(target=self._run, args=(job_id, {**self.settings, 'interval':0.001}), daemon=True)
        self.thread.start()


catalog = Catalog(database)
server = Server(('127.0.0.1', 0), catalog)
server.collector = FixtureCollector(catalog, FixtureCollector.fetch)
print(f'http://127.0.0.1:{server.server_port}', flush=True)
try:
    server.serve_forever()
finally:
    server.collector.close()
    server.server_close()
