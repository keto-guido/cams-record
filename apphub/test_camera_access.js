'use strict';
// Camera authorization: the shared vectors, then every /cams/ route against a fake go2rtc. Run: node apphub/test_camera_access.js
const assert = require('assert'), Module = require('module'), path = require('path'), os = require('os'), fs = require('fs'), http = require('http');
const { EventEmitter } = require('events');
const acl = require('./camera_acl');
const V = JSON.parse(fs.readFileSync(path.join(__dirname, 'camera_acl_vectors.json'), 'utf8'));

const reg = acl.parseRegistry(JSON.stringify(V.registry));
assert.deepEqual(reg.map(c => c.id), V.ids);
for (const text of V.broken_files) assert.deepEqual(acl.parseRegistry(text), []);
for (const c of V.access) assert.deepEqual(reg.filter(x => acl.canAccess(x, c.user, c.roles)).map(x => x.id), c.allowed, c.user);
for (const c of V.streams) assert.equal(acl.canStream(reg, c.name, c.user, c.roles), c.allowed, c.name + '/' + c.user);

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cams-acl-')), file = path.join(dir, 'cams.json');
const write = v => fs.writeFileSync(file, JSON.stringify(v));

// fileAccess has no roles: role grants never match, listed users do, and it follows the file.
write([{ id: 'open', name: 'Open' }, { id: 'locked', name: 'Locked', users: ['ann'] }, { id: 'by-role', name: 'By role', roles: ['admin', 'family'] }]);
const local = acl.fileAccess(file), err = console.error; console.error = () => {};
assert.deepEqual(local.list('bob'), [{ id: 'open', name: 'Open' }]);
assert.equal(local.get('bob', 'locked'), null);
assert.deepEqual(local.get('ann', 'locked'), { id: 'locked', name: 'Locked' });
assert.equal(local.can('ann', 'by-role'), false);
write([{ id: 'open', name: 'Renamed', users: ['ann'] }]);
assert.deepEqual(local.list('bob'), []);
fs.writeFileSync(file, '{broken');
assert.deepEqual(local.list('ann'), []);
fs.unlinkSync(file);
assert.deepEqual(local.list('ann'), []);
console.error = err;

const lib = path.join(dir, 'lib.js'), resolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) { return request === './lib' && parent.filename === path.join(__dirname, 'cams.js') ? lib : resolve.call(this, request, parent, ...rest); };
require.cache[lib] = { id: lib, filename: lib, loaded: true, exports: { DIR: dir } };
const factory = require('./cams');
Module._resolveFilename = resolve;

// Fake go2rtc, recording every path requested.
let calls = [];
http.get = (o, cb) => {
  calls.push(o.path);
  const ur = new EventEmitter(), up = new EventEmitter();
  ur.statusCode = 200; ur.headers = {}; ur.resume = () => {}; ur.pipe = r => r.pipe(o.path);
  up.setTimeout = () => {}; up.destroy = () => {};
  setImmediate(() => {
    cb(ur);
    const src = /src=([^&]+)/.exec(o.path);
    ur.emit('data', Buffer.from(o.path.startsWith('/api/stream.m3u8') ? 'hls/playlist.m3u8?id=sid' + src[1].replace(/\W/g, '') : 'x'));
    ur.emit('end');
  });
  return up;
};

const CAMS = [{ id: 'open', name: 'Open' }, { id: 'locked', name: 'Locked Room', snap: 'locked-raw', users: ['ann'], roles: [] }, { id: 'open2', name: 'Open2' }];
const hub = {
  list: user => CAMS.filter(c => !c.users || user === 'ann'),
  get: (user, id) => hub.list(user).find(c => c.id === id) || null,
  can: (user, id) => !!hub.get(user, id),
  canStream: (user, name) => { const o = CAMS.filter(c => c.id === name || c.snap === name); return o.length > 0 && o.every(c => hub.can(user, c.id)); },
};
const NF = { status: 404, body: 'nf' };
const app = factory({
  cameras: hub, recheckMs: 5, esc: String, page: (t, b) => '<head></head>' + b,
  send: (res, status, body) => res.finish({ status, body: String(body) }), notFound: res => res.finish(NF),
});
function get(user, url, headers) {
  return new Promise(ok => {
    const res = new EventEmitter();
    res.headersSent = false; res.status = null;
    res.finish = ok;
    res.writeHead = s => { res.status = s; res.headersSent = true; };
    res.end = b => ok({ status: res.status, body: String(b) });
    res.pipe = () => ok({ status: res.status, body: '(piped)', res });
    res.destroy = () => { res.destroyed = true; };
    const u = new URL('http://x' + url);
    app.handle({ method: 'GET', headers: headers || {} }, res, { user }, u.pathname, u);
  });
}

(async () => {
  for (const url of ['/cams/', '/cams/api/list', '/cams/sw.js', '/cams/manifest.webmanifest', '/cams/static/icon-192.png']) {
    assert.deepEqual(await get('', url, { 'x-apphub-user': 'ann' }), NF);
  }
  assert(!(await get('bob', '/cams/')).body.includes('Locked Room'));
  assert((await get('ann', '/cams/')).body.includes('Locked Room'));
  assert.deepEqual(JSON.parse((await get('bob', '/cams/api/list')).body), [{ id: 'open', name: 'Open' }, { id: 'open2', name: 'Open2' }]);
  const listed = (await get('ann', '/cams/api/list')).body;
  assert(listed.includes('locked') && !/users|roles|snap/.test(listed));

  // Spoofed headers change nothing, and forbidden looks exactly like unknown, without touching go2rtc.
  const spoof = { 'x-apphub-user': 'ann', 'x-apphub-roles': 'admin' };
  calls = [];
  for (const p of ['/cams/api/snap/', '/cams/api/stream/']) for (const rest of p.endsWith('stream/') ? ['/index.m3u8', '/live.mp4', '/hls/playlist.m3u8?id=sidlocked', '/hls/segment.ts?id=sidlocked&n=1'] : ['']) {
    assert.deepEqual(await get('bob', p + 'locked' + rest, spoof), NF);
    assert.deepEqual(await get('bob', p + 'unregistered' + rest), NF);
  }
  assert.deepEqual(calls, []);

  // The snap alias is what gets fetched, and it answers for the camera.
  assert.equal((await get('ann', '/cams/api/snap/locked')).status, 200);
  assert.deepEqual(calls, ['/api/frame.jpeg?src=locked-raw']);

  // An HLS session id only works for the camera it was issued for.
  assert.equal((await get('ann', '/cams/api/stream/locked/index.m3u8')).body, 'hls/playlist.m3u8?id=sidlocked');
  assert.equal((await get('bob', '/cams/api/stream/open/index.m3u8')).status, 200);
  calls = [];
  assert.deepEqual(await get('bob', '/cams/api/stream/open/hls/segment.ts?id=sidlocked&n=1'), NF);
  assert.deepEqual(await get('bob', '/cams/api/stream/open/hls/playlist.m3u8?id=never-issued'), NF);
  assert.deepEqual(calls, []);
  assert.equal((await get('bob', '/cams/api/stream/open/hls/segment.ts?id=sidopen&n=1')).status, 200);
  assert.equal((await get('ann', '/cams/api/stream/locked/hls/segment.ts?id=sidlocked&n=1')).status, 200);

  // Revocation: the next request is refused, even for a session id issued earlier, and an open stream is cut.
  const live = (await get('ann', '/cams/api/stream/locked/live.mp4')).res;
  const canStream = hub.canStream, can = hub.get;
  assert(!live.destroyed);
  hub.canStream = () => false; hub.get = () => null;
  assert.deepEqual(await get('ann', '/cams/api/stream/locked/hls/segment.ts?id=sidlocked&n=1'), NF);
  await new Promise(r => setTimeout(r, 30));
  assert(live.destroyed);
  live.emit('close');
  hub.canStream = canStream; hub.get = can;

  // A stream shared with a camera the user cannot see stays closed, even if the camera itself is visible.
  CAMS.push({ id: 'shared', name: 'Shared' });
  const only = hub.canStream;
  hub.canStream = (u, n) => n !== 'shared' && only(u, n);
  assert.deepEqual(await get('bob', '/cams/api/snap/shared'), NF);
  assert.deepEqual(await get('bob', '/cams/api/stream/shared/live.mp4'), NF);

  fs.rmSync(dir, { recursive: true, force: true });
  console.log('camera access tests passed');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
