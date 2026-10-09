"""Disposable catalog-update fixture; never opens the user's library or source site."""
from compression import zstd
from contextlib import closing, redirect_stdout
from pathlib import Path
import io
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from initialize_catalog import initialize


def make_catalog(path,count):
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
            CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
            CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
            INSERT INTO tag VALUES(1,'language:english');
        """)
        for gid in range(1,count+1):
            db.execute("INSERT INTO gallery VALUES (?,'abcdef0123','测试作品','','Non-H',?,10,'4.0',0,0,0)",(gid,1760000000+count*86400))
            db.execute('INSERT INTO gid_tid VALUES(?,1)',(gid,))
        db.commit()


def main():
    root=Path(sys.argv[1]).resolve()
    current=root/'data'/'catalog.sqlite3'
    make_catalog(current,2)
    with redirect_stdout(io.StringIO()):initialize(current)
    for count in (3,4):
        incoming=root/f'incoming-{count}.sqlite3'
        make_catalog(incoming,count)
        (root/f'incoming-{count}.zstd').write_bytes(zstd.compress(incoming.read_bytes()))
    (root/'e-hentai.db.zstd').write_bytes((root/'incoming-3.zstd').read_bytes())
    server=Server(('127.0.0.1',0),Catalog(current))
    server.catalog.favorites.save(1,123,'e-hentai.org')
    original_backup=server.maintenance.engine.backup
    def slow_backup(*args,**kwargs):
        progress=args[2] if len(args)>2 else kwargs.get('progress',lambda message:None)
        progress('正在一致性备份 catalog.sqlite3：1/2 页（50%）')
        time.sleep(5)
        return original_backup(*args,**kwargs)
    server.maintenance.engine.backup=slow_backup
    original_reload=server.maintenance.reload_catalog
    def reload_catalog():
        marker=root/'fail-next-switch'
        if marker.is_file():
            marker.unlink()
            raise RuntimeError('模拟目录载入失败')
        original_reload()
    server.maintenance.reload_catalog=reload_catalog
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
