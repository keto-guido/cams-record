'use strict';
// LOCAL DEVELOPMENT ONLY. Run: node apphub/dev_preview.js [port] (default 8123).
// Screenshot fixture server; never deploy or require this file from cams.js.
const http=require('http'),fs=require('fs'),path=require('path'),os=require('os'),Module=require('module');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'cams-preview-'));
const cameras=[{id:'front-door',name:'Front Door'},{id:'back-door',name:'Back Door'}];
fs.writeFileSync(path.join(dir,'cams.json'),JSON.stringify(cameras));
const lib=path.join(dir,'lib.js'),resolve=Module._resolveFilename;
Module._resolveFilename=function(request,parent,...rest){if(request==='./lib'&&parent.filename===path.join(__dirname,'cams.js'))return lib;return resolve.call(this,request,parent,...rest);};
require.cache[lib]={id:lib,filename:lib,loaded:true,exports:{DIR:dir}};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const send=(res,status,body,type='text/plain',headers={})=>{res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store',...headers});res.end(body);};
const app=require('./cams')({esc,send,notFound:res=>send(res,404,'Not found'),page:(title,body)=>'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>'+esc(title)+'</title><link rel="stylesheet" href="/static/style.css"></head><body>'+body+'</body></html>'});
Module._resolveFilename=resolve;
const base=':root{color-scheme:dark;--card:#202328;--line:#34383e;--acc:#a18af8;--mut:#969ba5;--fg:#edf0f5}*{box-sizing:border-box}body{margin:0;background:#121417;color:var(--fg);font:14px system-ui}button,input,select{font:inherit;color:inherit}button{cursor:pointer}.wrap{max-width:1000px;margin:auto;padding:12px 24px max(24px,env(safe-area-inset-bottom))}.bar{display:flex;justify-content:space-between;align-items:center;padding:12px 24px;border-bottom:1px solid var(--line)}.bar form{display:flex;gap:12px;align-items:center}.bar a{color:inherit}.ghost{background:transparent;border:0;color:var(--mut);min-height:44px}.muted{color:var(--mut)}.sm{font-size:12px}[hidden]{display:none!important}@media(max-width:600px){.wrap{padding:12px 12px 24px}.bar{padding:8px 12px}.bar form nav,.bar form span{display:none}}';
const placeholder='<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720"><rect width="1280" height="720" fill="#30343a"/><path d="M0 520 380 280 760 520 1060 340 1280 520V720H0" fill="#383e45"/><text x="640" y="350" text-anchor="middle" fill="#8c939d" font-family="sans-serif" font-size="28">Camera preview</text></svg>';
const now=()=>Date.now()/1000;
function spans(start,end,quality='sub'){const n=now(),min=n-(quality==='main'?3:2)*86400,max=n-65,out=[];for(let a=Math.floor(min/3600)*3600;a<max;a+=3600){const s=Math.max(a+((Math.floor(a/3600)%7===0)?420:0),min,start),e=Math.min(a+3540,max,end);if(e>s)out.push([s,e]);}return out;}
function events(cam){const n=now();return Array.from({length:12},(_,i)=>({id:String(i+1),camera:cam||cameras[i%2].id,camera_name:cam==='back-door'?'Back Door':'Front Door',label:i%3?'person':'car',score:.91,start_ts:n-240-i*880,end_ts:n-230-i*880,has_snapshot:true}));}
const server=http.createServer((req,res)=>{const u=new URL(req.url,'http://localhost'),p=u.pathname,q=u.searchParams;const json=j=>send(res,200,JSON.stringify(j),'application/json');
  if(p==='/')return res.writeHead(302,{Location:'/cams/'}),res.end();
  if(p==='/static/style.css')return send(res,200,base,'text/css');
  if(p.startsWith('/cams/api/snap/')||/\/snapshot.*\.jpg$/.test(p)||p.startsWith('/cams-detect/api/frame/'))return send(res,200,placeholder,'image/svg+xml');
  if(p.startsWith('/cams/api/stream/'))return send(res,404,'Preview has no live stream');
  if(p==='/cams-detect/api/cameras')return json(cameras.map(c=>({...c,stream:c.id,configured:true,enabled:true})));
  if(p==='/cams-detect/api/events'){let evs=events(q.get('camera'));if(q.has('since'))evs=evs.filter(e=>e.start_ts>=+q.get('since'));if(q.has('until'))evs=evs.filter(e=>e.start_ts<+q.get('until'));if(q.has('before'))evs=evs.filter(e=>e.start_ts<+q.get('before'));return json(evs);}
  const start=+(q.get('start')||now()-86400),end=+(q.get('end')||now());
  if(p==='/cams-record/api/timeline')return json({spans:{sub:spans(start,end),main:spans(start,end,'main')}});
  if(p==='/cams-record/api/playback'){let offset=0,id=0;const segments=[];for(const [a,b] of spans(start,end))for(let s=a;s<b;s+=60){const e=Math.min(s+60,b),duration=e-s;segments.push({id:++id,start:s,end:e,duration,offset,codec:'h264'});offset+=duration;}return json({segments,playlist:'hls/'+(q.get('cam')||'front-door')+'/index.m3u8?'+q});}
  if(p==='/cams-record/api/export/info'||p==='/cams-record/api/clip/info'){const sub=spans(start,end),main=spans(start,end,'main'),status=x=>x.length?'available':end>now()-65?'not_ready':'unavailable';return json({busy:false,recommended:main.length?'main':sub.length?'sub':null,main:{status:status(main),bytes:Math.round((end-start)*750000)},sub:{status:status(sub),bytes:Math.round((end-start)*100000)}});}
  if(p.startsWith('/cams-record/'))return send(res,404,'Preview has no media files');
  return app.handle(req,res,{user:'Preview',csrf:'preview'},p,u);
});
server.listen(Number(process.argv[2]||8123),'127.0.0.1',()=>console.log('Camera preview: http://127.0.0.1:'+server.address().port+'/cams/#cam=front-door'));
process.on('exit',()=>fs.rmSync(dir,{recursive:true,force:true}));
