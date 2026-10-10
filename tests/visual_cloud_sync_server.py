"""Isolated UI fixture with a mock cloud transport; never uses real records or D1."""
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from cloud_sync import DEFAULT_ENDPOINT
from test_desktop import make_library


def main():
    root=Path(sys.argv[1]).resolve()
    root.mkdir(parents=True,exist_ok=True)
    server=Server(('127.0.0.1',0),Catalog(make_library(root)))
    def transport(endpoint,path,payload=None,token=''):
        if path=='/v2/register':return {'version':2,'source_id':'00000000-0000-4000-8000-000000000003','status':'review'}
        if payload is not None:
            return {'version':2,'accepted':len(payload['records']),'pending':0,'kept':0}
        time.sleep(.2)
        from urllib.parse import parse_qs,urlsplit
        after=int(parse_qs(urlsplit(path).query).get('after',['0'])[0])
        records=[] if after else [{'revision':1,'gid':99,'favorite_count':120,'rating_count':30,
            'checked_at':int(time.time())-1,'source':'e-hentai.org',
            'collector_id':'00000000-0000-4000-8000-000000000002','collector_name':'测试贡献者'}]
        records=[{'gid':r['gid'],'revision':r.pop('revision'),'action':'snapshot','snapshot':r} for r in records]
        return {'format':'excatalog-cloud-sync','version':2,'dataset_id':'0'*32,
            'cursor':1,'until':1,'head':1,'has_more':False,'records':records}
    server.cloud_sync.transport=transport
    queued=int(sys.argv[2]) if len(sys.argv)>2 else 0
    if queued:
        with server.catalog.favorites.connection() as db:
            for gid in range(1,queued+1):
                server.catalog.favorites._save(db,gid,100,'e-hentai.org',20)
        server.cloud_sync.configure({'endpoint':'https://sync.example.invalid','download_enabled':True,'upload_enabled':True,
            'consent':True})
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
