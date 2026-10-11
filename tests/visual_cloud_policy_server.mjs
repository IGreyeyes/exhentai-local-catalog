import http from 'node:http';
import worker from '../cloudflare/worker.js';
import {D1} from './cloud_sync_fixture.mjs';

const env={DB:new D1(),ADMIN_SECRET:'test-only-admin-secret-not-a-real-credential'};
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
server.listen(0,'127.0.0.1',()=>console.log('http://127.0.0.1:'+server.address().port));
