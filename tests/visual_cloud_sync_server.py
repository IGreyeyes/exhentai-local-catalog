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
    mock_checked_at=int(time.time())-1
    def transport(endpoint,path,payload=None,token=''):
        if path=='/health':return {'version':3,'max_upload_batch':50,'upload_preflight':True,'max_upload_check_batch':500,'public_correction_state':True,'upload_min_interval_days':14}
        if path=='/v3/register':return {'version':3,'source_id':'00000000-0000-4000-8000-000000000003','status':'review'}
        if path.startswith('/v3/upload-state?'):
            from urllib.parse import parse_qs,urlsplit
            dataset=parse_qs(urlsplit(path).query).get('dataset_id',[''])[0]
            return {'version':3,'dataset_id':'0'*32,'reset':dataset!='0'*32,'cursor':81,'until':81,'has_more':False,'corrections':[]}
        if path=='/v3/upload-check':
            return {'version':3,'dataset_id':'0'*32,'source_id':'00000000-0000-4000-8000-000000000003','status':'review',
                'records':[{'gid':r['gid'],'skip':r['gid']<=80,'revision':r['gid'] if r['gid']<=80 else 0} for r in payload['records']]}
        if payload is not None:
            return {'version':3,'accepted':len(payload['records']),'pending':0,'kept':0}
        time.sleep(.2)
        from urllib.parse import parse_qs,urlsplit
        after=int(parse_qs(urlsplit(path).query).get('after',['0'])[0])
        records=[] if after>=81 else [{'revision':81,'gid':99,'favorite_count':120,'rating_count':30,
            'checked_at':mock_checked_at,'source':'e-hentai.org',
            'collector_id':'00000000-0000-4000-8000-000000000002','collector_name':'测试贡献者'}]
        records=[{'gid':r['gid'],'revision':r.pop('revision'),'action':'snapshot','snapshot':r} for r in records]
        return {'format':'excatalog-cloud-sync','version':3,'dataset_id':'0'*32,
            'cursor':81,'until':81,'head':81,'has_more':False,'records':records}
    server.cloud_sync.transport=transport
    queued=int(sys.argv[2]) if len(sys.argv)>2 else 0
    if queued:
        mixed = len(sys.argv)>3 and sys.argv[3]=='mixed'
        with server.catalog.favorites.connection() as db:
            for gid in range(1,queued+1):
                if mixed and gid % 3:
                    collector = '00000000-0000-4000-8000-000000000002' if gid % 3==1 else ''
                    name = '测试原采集者' if collector else ''
                    db.execute("INSERT OR REPLACE INTO favorites VALUES(?,100,?,'e-hentai.org',?,?)", (gid,int(time.time()),collector,name))
                else:
                    server.catalog.favorites._save(db,gid,100,'e-hentai.org',20)
        if mixed:
            server.cloud_sync.stopped.wait=lambda timeout:False
        server.cloud_sync.configure({'endpoint':'https://sync.example.invalid','download_enabled':True,'upload_enabled':True,
            'consent':True})
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
