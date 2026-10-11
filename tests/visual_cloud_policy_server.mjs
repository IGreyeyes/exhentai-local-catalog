import http from 'node:http';
import worker from '../cloudflare/worker.js';
import {D1} from './cloud_sync_fixture.mjs';

const env={DB:new D1(),ADMIN_SECRET:'test-only-admin-secret-not-a-real-credential'};
if(process.argv.includes('--bulk-review')){
  const now=Math.floor(Date.now()/1000),owner='00000000-0000-4000-8000-000000000001';
  for(const [id,name,start,count] of [['00000000-0000-4000-8000-000000000003','测试来源甲',1,123],
    ['00000000-0000-4000-8000-000000000004','测试来源乙',1001,4]]){
    env.DB.sqlite.prepare("INSERT INTO upload_sources(id,token_hash,collector_id,name,created_at) VALUES(?,?,?,?,?)")
      .run(id,'test-only-placeholder-'+start,owner,name,now);
    for(let i=0;i<count;i++){
      const snapshot={gid:start+i,favorite_count:start===1&&i>=120?2000000:100,rating_count:20,
        checked_at:now-100,source:'e-hentai.org',collector_id:owner,collector_name:'测试原采集者'};
      env.DB.sqlite.prepare('INSERT INTO cloud_pending(gid,payload,checked_at,source_id,reason,received_at) VALUES(?,?,?,?,?,?)')
        .run(snapshot.gid,JSON.stringify(snapshot),now-100,id,'新来源待审',now);
    }
  }
}
const server=http.createServer(async(req,res)=>{
  if(req.url==='/__stop'){res.end('stopped');server.close(()=>{env.DB.close();});return;}
  try{
    const chunks=[];for await(const chunk of req)chunks.push(chunk);
    const request=new Request('https://sync.example.invalid'+req.url,{method:req.method,
      headers:{...req.headers,'CF-Connecting-IP':'127.0.0.1'},
      ...(['GET','HEAD'].includes(req.method)?{}:{body:Buffer.concat(chunks)})});
    const response=await worker.fetch(request,env);res.writeHead(response.status,Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  }catch{res.writeHead(500);res.end('Fixture error');}
});
let attempts=0;
function listen(){server.listen(30000+Math.floor(Math.random()*20000),'127.0.0.1',()=>console.log('http://127.0.0.1:'+server.address().port));}
server.on('error',error=>{if(error.code==='EADDRINUSE'&&++attempts<10)listen();else throw error;});
listen();
