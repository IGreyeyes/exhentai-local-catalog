"""Independent source/target computers for browser failure migration checks."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from test_desktop import make_library

root=Path(sys.argv[1])
role=sys.argv[2]
database=make_library(root)
catalog=Catalog(database)
store=catalog.favorites
if role=='source':
    store.save(1,50,'e-hentai.org')
    store.save(2,60,'e-hentai.org')
    with store.connection() as db:
        db.execute('UPDATE favorites SET checked_at=1760000000')
        for gid,failed_at,error in ((2,1760000100,'模拟连接失败，代理 test-private-proxy-marker'),(3,1760000100,'作品不可访问（HTTP 404）'),(999,1760000200,'模拟 Cookie 访问异常 test-private-access-marker')):
            store._save_failure(db,gid,'abcdef0123','e-hentai.org',error,3,7)
            db.execute('UPDATE collection_failures SET failed_at=? WHERE gid=?',(failed_at,gid))
else:
    store.save(3,10,'e-hentai.org')
    with store.connection() as db:
        db.execute('UPDATE favorites SET checked_at=1760000300 WHERE gid=3')
        store._save_failure(db,2,'abcdef0123','e-hentai.org','目标电脑原有失败记录',1,0)
        db.execute('UPDATE collection_failures SET failed_at=1760000050 WHERE gid=2')
server=Server(('127.0.0.1',0),catalog)
print(f'http://127.0.0.1:{server.server_port}',flush=True)
try:server.serve_forever()
finally:server.server_close()
