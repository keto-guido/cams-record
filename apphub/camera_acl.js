'use strict';
// Per-camera authorization. Contract: docs/camera-access.md.
// Node twin of cams_record/camera_access.py; camera_acl_vectors.json holds the cases both must pass.
const fs = require('fs');
const ADMIN = 'admin', ID = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/, SNAP = /^[A-Za-z0-9._-]{1,64}$/;

// The list under key; null if malformed. Missing is empty, but an explicit null is malformed.
function list(entry, key) {
  if (!Object.prototype.hasOwnProperty.call(entry, key)) return [];
  const v = entry[key];
  return Array.isArray(v) && v.every(x => typeof x === 'string' && x) ? v : null;
}
function camera(e) {
  if (!e || typeof e !== 'object' || Array.isArray(e)) return null;
  if (typeof e.id !== 'string' || !ID.test(e.id) || typeof e.name !== 'string' || !e.name) return null;
  const hasSnap = Object.prototype.hasOwnProperty.call(e, 'snap');
  if (hasSnap && (typeof e.snap !== 'string' || !SNAP.test(e.snap) || e.snap.includes('..'))) return null;
  const users = list(e, 'users'), roles = list(e, 'roles'), bad = !users || !roles; // malformed: admin-only, never open
  return { id: e.id, name: e.name, snap: hasSnap ? e.snap : null, users: bad ? [] : users, roles: bad ? [] : roles, restricted: bad || users.length + roles.length > 0 };
}
// cams.json text to cameras. Bad JSON or a non-array means no cameras; bad entries and every entry
// sharing a duplicated id are dropped, so they are unknown to everyone including admin.
function parseRegistry(text) {
  let data;
  try { data = JSON.parse(text); } catch (e) { console.error('cams.json: invalid JSON, no cameras'); return []; }
  if (!Array.isArray(data)) { console.error('cams.json: not an array, no cameras'); return []; }
  const cams = data.map(camera).filter(Boolean), count = {};
  for (const c of cams) count['$' + c.id] = (count['$' + c.id] || 0) + 1;
  return cams.filter(c => count['$' + c.id] === 1);
}
function canAccess(cam, user, roles) {
  if (!cam || typeof user !== 'string' || !user) return false;
  if (roles.includes(ADMIN)) return true;
  return !cam.restricted || cam.users.includes(user) || roles.some(r => cam.roles.includes(r));
}
// A go2rtc stream is the same footage as every camera using it as id or snap: all must allow it.
function canStream(cams, name, user, roles) {
  const owners = cams.filter(c => c.id === name || c.snap === name);
  return owners.length > 0 && owners.every(c => canAccess(c, user, roles));
}
const pub = ({ id, name, snap }) => (snap == null ? { id, name } : { id, name, snap }); // never users/roles
// Same interface as the hub's C.cameras, for when it is absent. The session carries no roles, so
// open cameras and listed users work, while role grants and admin do not: this can only under-grant.
function fileAccess(file) {
  let stamp = null, cams = [];
  const current = () => {
    try {
      const st = fs.statSync(file), next = `${st.mtimeMs}:${st.size}:${st.ino}`;
      if (next !== stamp) { cams = parseRegistry(fs.readFileSync(file, 'utf8')); stamp = next; }
    } catch (e) {
      if (stamp !== 'unreadable') console.error('cams.json:', e.message);
      stamp = 'unreadable'; cams = [];
    }
    return cams;
  };
  const can = (user, id) => canAccess(current().find(c => c.id === id), user, []);
  return {
    list: user => current().filter(c => canAccess(c, user, [])).map(pub),
    get: (user, id) => (can(user, id) ? pub(current().find(c => c.id === id)) : null),
    can,
    canStream: (user, name) => canStream(current(), name, user, []),
  };
}
module.exports = { parseRegistry, canAccess, canStream, fileAccess };
