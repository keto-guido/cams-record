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
assert(html.indexOf('hls.min.js?v=1.7.4') < html.indexOf('camera_audio.js?v=2'));
assert(html.indexOf('camera_audio.js?v=2') < html.indexOf('cams.js?v=22'));
assert(html.includes('cams.css?v=17'));
// Live production (apphub PR #4 / 7c7d020) already shipped cams.js?v=21; never go backwards.
assert(!html.includes('cams.js?v=20') && !html.includes('cams.js?v=21') && !html.includes('cams.js?v=19'));
assert(html.includes('rel="manifest" href="/cams/manifest.webmanifest"'));
assert(html.includes('id="push-bar"') && html.includes('id="push-toggle"') && html.includes('id="push-test"'));
assert(html.includes('apple-mobile-web-app-capable'));
// Shared navigation: the hub's page() renders the bar from these; sections use the one sub-nav pattern.
assert.deepEqual(pageOptions, {app:'cams',name:'Cameras'});
assert(html.includes('<nav class="mn-subnav cams-tabs" id="cams-tabs"'));
for (const view of ['grid','det']) assert(html.includes('class="cams-tab" data-view="'+view+'"'));
// Camera management is part of the same app: an Admin tab, hidden until cams-record confirms an admin.
assert(html.includes('<a class="cams-tab" data-view="admin" href="#admin" id="tab-admin" hidden>Admin</a>'));
for (const id of ['view-admin','view-admin-edit','adm-list','adm-banner','adm-form','adm-history','adm-intro','f-id','f-name','f-enabled','f-main','f-sub','f-snap','f-use','f-defaults','f-advanced','f-vis-everyone','f-vis-restricted','f-users','f-roles','f-detect','f-detect-status','f-masks','f-masks-none','f-audio-panel','f-main-days','f-sub-days','adm-save','adm-delete','sec-name','sec-streams','sec-detection','sec-masks','sec-access','sec-recording'])
  assert(html.includes('id="'+id+'"'), id);
assert(html.includes('>Name<') && html.includes('>Streams<') && html.includes('>Detection<') && html.includes('>Masks<') && html.includes('>Access<') && html.includes('>Recording<'));
assert(html.includes('Low-res stream (sub)') && html.includes('HD stream (main)'));
assert(/id="view-admin" hidden/.test(html) && /id="view-admin-edit" hidden/.test(html) && /id="adm-add" hidden/.test(html));

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
// Push deep-link: #event=<uid> -> /api/push/event-context -> lbOpen, with real snapshot flags (not hard-coded true).
assert(client.includes("CR_PUSH + '/event-context/'") && client.includes('lbOpen(ev)'));
assert(client.includes('has_snapshot: !!ctx.has_snapshot') && client.includes('has_snapshot_hires: !!ctx.has_snapshot_hires'));
assert(!/has_snapshot:\s*true/.test(client.split('async function openEventId')[1].split('// ---------- camera management')[0]));
// Lightbox snapshot retry/fallback (apphub PR #4 behaviour, kept above production v=21).
assert(client.includes('function lbArmSnapshot') && client.includes('SNAP_RETRY') && client.includes('allowFallback'));
assert(client.includes('lbImg.onerror'));
// Admin writes carry the loaded revision, are JSON, and never build HTML from camera data.
assert(client.includes("ADM = CR + '/api/admin'") && client.includes('expect_revision: adm.revision'));
assert(client.includes("'Content-Type': 'application/json'"));
assert(client.includes('CamsAudio.mount') && client.includes("basePath: '/cams-audio'"));
assert(client.includes('presentCamera') && client.includes('streams_view'));
assert(client.includes('Low-res ') && client.includes('HD '));
const adminPart = client.split('// ---------- camera management')[1].split('// ---------- routing')[0];
assert(!/innerHTML|insertAdjacentHTML|outerHTML/.test(adminPart), 'admin view must use textContent only');
const audio = fs.readFileSync(path.join(__dirname, 'static', 'camera_audio.js'), 'utf8');
assert(audio.includes("DEFAULT_BASE = '/cams-audio'") && audio.includes('has_audio') && audio.includes('Audio service not installed'));
assert(audio.includes('RETENTION_MAX = 3650') && audio.includes("method: 'PATCH'"));
// Detection rows: zoomed thumbnail with a fallback to the full snapshot, and a type icon.
const rowPart = client.split('function evRow(e)')[1].split('function EvList')[0];
assert(rowPart.includes("'/thumb.jpg'") && rowPart.includes("'/snapshot.jpg'") && rowPart.includes("addEventListener('error'"));
assert(rowPart.includes('kindIcon('));
for (const k of ['person','car','dog','cat','other']) assert(new RegExp('\\n    '+k+': \\[').test(client), k);
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
      app.handle({method:'GET'}, {done:r4=>{
        assert.equal(r4.status,200);
        assert.equal(r4.type,'application/javascript; charset=utf-8');
        assert(String(r4.body).includes('CamsAudio') || Buffer.isBuffer(r4.body));
        fs.rmSync(dir,{recursive:true});
        console.log('camera page tests passed');
      }}, {user:'test'}, '/cams/static/camera_audio.js', new URL('http://localhost/cams/static/camera_audio.js'));
    }}, {user:'test'}, '/cams/static/hls.min.js', new URL('http://localhost/cams/static/hls.min.js'));
  }}, {user:'test'}, '/cams/manifest.webmanifest', new URL('http://localhost/cams/manifest.webmanifest'));
}}, {user:'test'}, '/cams/sw.js', new URL('http://localhost/cams/sw.js'));
