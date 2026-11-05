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
let html;
const app = require('./cams')({esc:String, page:(title,body)=>'<head></head>'+body, send:(res,status,body,type,headers)=>res.done({status,body,type,headers}),notFound:()=>assert.fail('not found')});
Module._resolveFilename = original;
app.handle({method:'GET'}, {done:r=>{assert.equal(r.status,200);html=r.body;}}, {user:'test',csrf:'csrf'}, '/cams/', new URL('http://localhost/cams/'));
assert(html.includes('id="timeline"'));
assert(html.indexOf('hls.min.js?v=1.7.4') < html.indexOf('cams.js?v=10'));
assert(html.includes('cams.css?v=6'));
app.handle({method:'GET'}, {done:r=>{assert.equal(r.status,200);assert(r.type.startsWith('application/javascript'));assert.equal(r.headers['Cache-Control'],'private, max-age=86400');fs.rmSync(dir,{recursive:true});console.log('camera page tests passed');}}, {}, '/cams/static/hls.min.js', new URL('http://localhost/cams/static/hls.min.js'));
