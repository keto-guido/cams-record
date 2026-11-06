'use strict';
const assert = require('assert'), Module = require('module'), path = require('path'), os = require('os'), fs = require('fs');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cams-page-'));
fs.writeFileSync(path.join(dir, 'cams.json'), '[]');
const original = Module._resolveFilename;
Module._resolveFilename = function(request, parent, ...rest) {
  if (request === './lib' && parent.filename === path.join(__dirname, 'cams.js')) return path.join(dir, 'lib.js');
  return original.call(this, request, parent, ...rest);
};
require.cache[path.join(dir, 'lib.js')] = {id:path.join(dir,'lib.js'),filename:path.join(dir,'lib.js'),loaded:true,exports:{DIR:dir}};
let html, pageOptions;
const app = require('./cams')({esc:String, page:(title,body,options)=>{pageOptions=options;assert(title.includes('MERSINO NETWORK'));return '<head></head>'+body;}, send:(res,status,body,type,headers)=>res.done({status,body,type,headers}),notFound:()=>assert.fail('not found')});
Module._resolveFilename = original;
app.handle({method:'GET'}, {done:r=>{assert.equal(r.status,200);html=r.body;}}, {user:'test',csrf:'csrf'}, '/cams/', new URL('http://localhost/cams/'));
assert(html.includes('id="timeline"'));
assert(!html.includes('id="tl-video"'));
assert(!html.includes('id="tl-fs"'));
assert(html.includes('<canvas id="tl-bar"'));
for (const id of ['back','play','forward','det-prev','det-next','out','in','calendar','select','cancel']) assert(html.includes('id="tl-'+id+'"'));
for (const id of ['cam-video','cam-mode','cam-live','cam-retry','cam-fs','tl-date','tl-range']) assert(html.includes('id="'+id+'"'));
assert(!/style=|<script>/.test(html));
assert(/id="cam-retry" hidden>Couldn't load this recording\. Tap to retry\.</.test(html));
assert(html.indexOf('hls.min.js?v=1.7.4') < html.indexOf('cams.js?v=17'));
assert(html.includes('cams.css?v=13'));
assert(html.includes('rel="manifest" href="/cams/manifest.webmanifest"'));
assert(html.includes('id="push-bar"') && html.includes('id="push-toggle"') && html.includes('id="push-test"'));
assert(html.includes('apple-mobile-web-app-capable'));
// Shared navigation: the hub's page() renders the bar from these; sections use the one sub-nav pattern.
assert.deepEqual(pageOptions, {app:'cams',name:'Cameras'});
assert(html.includes('<nav class="mn-subnav cams-tabs" id="cams-tabs"'));
for (const view of ['grid','det']) assert(html.includes('class="cams-tab" data-view="'+view+'"'));
assert(!/Back to Dashboard|backdash|action="\/logout"|App Hub/.test(html));
assert(html.includes('id="tl-det-prev" aria-label="Previous detection"'));
assert(html.includes('id="tl-det-next" aria-label="Next detection"'));
assert(html.includes('>Prev event<') && html.includes('>Next event<'));
assert(html.includes('id="cam-toast" hidden'));
assert(html.includes('class="cd-lb-hd" aria-pressed="false"'));
// Plain links stay the desktop path; the share flow only intercepts their tap.
assert(/<a class="cd-dl-snap" href="#" download hidden>/.test(html) && /<a class="cd-dl-clip" href="#" download hidden>/.test(html) && /<a id="tl-download" download hidden>/.test(html));
const client = fs.readFileSync(path.join(__dirname, 'static', 'cams.js'), 'utf8');
assert(client.split('async function saveShare')[1].split('await ')[1].startsWith('navigator.share('), 'share must be the first await in the tap');
assert(client.includes('MAX_SHARE = 250e6'));
assert(client.includes("register('/cams/sw.js'") && client.includes('#event='));
assert(client.includes('Add to Home Screen') && client.includes('normal tab'));
assert(client.includes('event=([^&]+)'), 'deep link must accept string event ids');
assert(client.includes('encodeURIComponent(eid)'));
const sw = fs.readFileSync(path.join(__dirname, 'static', 'sw.js'), 'utf8');
assert(sw.includes("addEventListener('push'") && sw.includes("addEventListener('notificationclick'"));
assert(sw.includes("clients.openWindow") || sw.includes('openWindow'));
app.handle({method:'GET'}, {done:r=>{
  assert.equal(r.status,200);
  assert.equal(r.type,'application/javascript; charset=utf-8');
  assert.equal(r.headers['Service-Worker-Allowed'],'/cams/');
  app.handle({method:'GET'}, {done:r2=>{
    assert.equal(r2.status,200);
    assert(r2.type.startsWith('application/manifest'));
    app.handle({method:'GET'}, {done:r3=>{
      assert.equal(r3.status,200);
      assert(r3.type.startsWith('application/javascript'));
      assert.equal(r3.headers['Cache-Control'],'private, max-age=86400');
      fs.rmSync(dir,{recursive:true});
      console.log('camera page tests passed');
    }}, {}, '/cams/static/hls.min.js', new URL('http://localhost/cams/static/hls.min.js'));
  }}, {}, '/cams/manifest.webmanifest', new URL('http://localhost/cams/manifest.webmanifest'));
}}, {}, '/cams/sw.js', new URL('http://localhost/cams/sw.js'));
