"""Disposable saved works for inline reading-state interaction checks."""
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from test_desktop import make_library

root = Path(sys.argv[1])
database = make_library(root)
with sqlite3.connect(database) as db:
    for gid in range(2, 86):
        db.execute("INSERT INTO gallery VALUES(?,'abcdef0123',?,'','Non-H',?,24,'4.0',0,0,0)",
                   (gid, f'阅读标记示例 {gid}', gid))
        db.execute('INSERT INTO gid_tid VALUES(?,1)', (gid,))
catalog = Catalog(database)
for gid in range(1, 86):
    catalog.favorites.save(gid, gid * 10, 'e-hentai.org', rating_count=20)
catalog.favorites.set_reading_state([1], 'none')
# Failures are seeded locally, without any source-site requests.
with catalog.favorites.connection() as db:
    catalog.favorites._save_failure(db, 86, 'abcdef0123', 'e-hentai.org', '模拟连接失败', 1, 0)
server = Server(('127.0.0.1', 0), catalog)
print(f'http://127.0.0.1:{server.server_port}', flush=True)
try:
    server.serve_forever()
finally:
    server.collector.close()
    server.server_close()
