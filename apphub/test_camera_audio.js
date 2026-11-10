'use strict';
// CamsAudio panel: installed / not-installed / enable-links-source / 409, retention, DOM mount.
// Run: node apphub/test_camera_audio.js
const assert = require('assert');
const CamsAudio = require('./static/camera_audio');

assert.deepEqual(CamsAudio.defaults(), {
  enabled: true, clip_retention_days: 90, transcript_retention_days: 365,
});
assert.equal(CamsAudio.validateRetention(90).ok, true);
assert.equal(CamsAudio.validateRetention(1).ok, true);
assert.equal(CamsAudio.validateRetention(3650).ok, true);
assert.equal(CamsAudio.validateRetention(0).ok, false);
assert.equal(CamsAudio.validateRetention(3651).ok, false);
assert.equal(CamsAudio.validateRetention(1.5).ok, false);
assert.equal(CamsAudio.validateRetention('abc').ok, false);
assert.equal(CamsAudio.validateRetention(true).ok, false);

function mockFetch(handler) {
  return async (url, opts) => {
    const res = await handler(url, opts || {});
    const status = res.status || 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      async json() { return res.body; },
    };
  };
}

/** Minimal DOM so mount() can be exercised without jsdom. */
function fakeDocument() {
  function Node() { this.childNodes = []; this.parentNode = null; this.style = {}; this.dataset = {}; this._listeners = {}; }
  Node.prototype.appendChild = function (c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    this.childNodes.push(c); c.parentNode = this; return c;
  };
  Node.prototype.removeChild = function (c) {
    const i = this.childNodes.indexOf(c); if (i < 0) throw new Error('not found');
    this.childNodes.splice(i, 1); c.parentNode = null; return c;
  };
  Object.defineProperty(Node.prototype, 'firstChild', { get() { return this.childNodes[0] || null; } });
  Object.defineProperty(Node.prototype, 'textContent', {
    get() {
      if (this.nodeType === 3) return this.data || '';
      return this.childNodes.map(c => c.textContent).join('');
    },
    set(v) { this.childNodes = []; if (v) this.appendChild(doc.createTextNode(String(v))); },
  });
  Node.prototype.setAttribute = function (k, v) { this[k] = v; };
  Node.prototype.addEventListener = function (type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); };
  Node.prototype.click = function () { (this._listeners.click || []).forEach(fn => fn({ preventDefault() {} })); };
  Node.prototype.dispatchEvent = function (type) {
    (this._listeners[type] || []).forEach(fn => fn({ type, target: this, preventDefault() {} }));
  };
  Node.prototype.querySelector = function (sel) {
    const all = [];
    (function walk(n) { all.push(n); (n.childNodes || []).forEach(walk); })(this);
    if (sel.startsWith('#')) return all.find(n => n.id === sel.slice(1)) || null;
    return null;
  };
  const doc = {
    createElement(tag) {
      const n = new Node(); n.tagName = tag.toUpperCase(); n.nodeType = 1;
      n.className = ''; n.id = ''; n.type = ''; n.value = ''; n.checked = false;
      n.disabled = false; n.readOnly = false; n.hidden = false; n.href = '';
      return n;
    },
    createTextNode(text) { const n = new Node(); n.nodeType = 3; n.data = String(text); return n; },
  };
  return doc;
}

// List shape: [{camera_id, name, has_audio, source}]
const inventory = [
  { camera_id: 'front-door', name: 'Front Door', has_audio: true, source: { id: 'src-front-door' } },
  { camera_id: 'back-yard', name: 'Back Yard', has_audio: true, source: { id: 'src-back-yard' } },
  { camera_id: 'front-yard', name: 'Front Yard', has_audio: true, source: null },
  { camera_id: 'mckinley-webcam', name: 'McKinley Webcam', has_audio: false, source: null },
];

const details = {
  'front-door': {
    id: 'front-door', name: 'Front Door', type: 'camera', enabled: true,
    clip_retention_days: 90, transcript_retention_days: 365, camera_id: 'front-door',
    config: { stream: null }, source: { id: 'src-front-door' },
  },
  'back-yard': {
    id: 'back-yard', name: 'Back Yard', type: 'camera', enabled: false,
    clip_retention_days: 90, transcript_retention_days: 365, camera_id: 'back-yard',
    config: { stream: 'back-yard-raw' }, source: { id: 'src-back-yard' },
  },
};

function audioFetch(extra) {
  return mockFetch(async (url, opts) => {
    if (extra) {
      const over = await extra(url, opts);
      if (over) return over;
    }
    const u = String(url);
    if (u.endsWith('/api/cameras')) return { status: 200, body: inventory };
    const m = u.match(/\/api\/cameras\/([^/]+)\/audio$/);
    if (!m) return { status: 404, body: { detail: 'not found' } };
    const id = decodeURIComponent(m[1]);
    if (opts.method === 'PATCH') {
      const row = inventory.find(c => c.camera_id === id);
      if (!row) return { status: 404, body: { detail: 'unknown camera' } };
      const patch = JSON.parse(opts.body || '{}');
      if (patch.enabled === true && row.has_audio !== true) {
        return { status: 409, body: { detail: 'enabling requires audio' } };
      }
      // Enabling an unlinked has_audio camera creates the source.
      if (patch.enabled === true && row.source == null) {
        const created = { id: 'src-' + id };
        row.source = created;
        const body = {
          id, name: row.name, type: 'camera', enabled: true,
          clip_retention_days: 90, transcript_retention_days: 365, camera_id: id,
          config: { stream: null }, source: created,
        };
        details[id] = body;
        return { status: 200, body };
      }
      const cur = details[id];
      if (!cur) return { status: 404, body: { detail: 'unknown camera' } };
      if (patch.enabled != null) cur.enabled = !!patch.enabled;
      if (patch.clip_retention_days != null) cur.clip_retention_days = patch.clip_retention_days;
      if (patch.transcript_retention_days != null) cur.transcript_retention_days = patch.transcript_retention_days;
      if ('stream' in patch) cur.config = { stream: patch.stream };
      return { status: 200, body: { ...cur, config: { ...cur.config }, source: row.source } };
    }
    if (!details[id]) return { status: 404, body: { detail: 'unknown camera' } };
    return { status: 200, body: details[id] };
  });
}

(async () => {
  // ---- installed: load front-door (list + GET detail) ----
  {
    const seen = [];
    const ready = await CamsAudio.loadCameraAudio('front-door', {
      fetch: mockFetch(async url => {
        seen.push(url);
        if (String(url).endsWith('/api/cameras')) return { status: 200, body: inventory };
        return { status: 200, body: details['front-door'] };
      }),
    });
    assert.equal(ready.state, 'ready');
    assert.equal(ready.audio.has_audio, true);
    assert.equal(ready.audio.enabled, true);
    assert.equal(ready.audio.stream, null);
    assert.equal(seen.length, 2);
    assert.match(seen[0], /\/cams-audio\/api\/cameras$/);
    assert.match(seen[1], /\/cams-audio\/api\/cameras\/front-door\/audio$/);
  }

  // ---- has_audio false => hidden ----
  {
    const hidden = await CamsAudio.loadCameraAudio('mckinley-webcam', {
      fetch: audioFetch(),
    });
    assert.equal(hidden.state, 'hidden');
  }

  // ---- camera absent from audio inventory => hidden ----
  {
    const hidden = await CamsAudio.loadCameraAudio('driveway', {
      fetch: audioFetch(),
    });
    assert.equal(hidden.state, 'hidden');
  }

  // ---- has_audio true but source null => ready with Enable off (not a dead-end message) ----
  {
    const r = await CamsAudio.loadCameraAudio('front-yard', { fetch: audioFetch() });
    assert.equal(r.state, 'ready');
    assert.equal(r.audio.enabled, false);
    assert.equal(r.audio.source, null);
    assert.equal(r.audio.clip_retention_days, 90);
    assert.equal(r.audio.transcript_retention_days, 365);
  }

  // ---- not installed: 404 / 502 / network on list ----
  for (const status of [404, 502, 0]) {
    const r = await CamsAudio.loadCameraAudio('front-door', {
      fetch: mockFetch(async () => {
        if (status === 0) throw new Error('boom');
        return { status, body: { detail: 'gone' } };
      }),
    });
    assert.equal(r.state, 'not_installed', 'status ' + status);
  }
  assert.equal(CamsAudio.classifyListError({ status: 404 }), 'not_installed');
  assert.equal(CamsAudio.classifyListError({ status: 502 }), 'not_installed');
  assert.equal(CamsAudio.classifyListError({ status: 0 }), 'not_installed');
  assert.equal(CamsAudio.classifyListError({ status: 500 }), 'error');

  // ---- PATCH 409 when enabling a camera with no audio ----
  {
    const client = CamsAudio.api('/cams-audio', mockFetch(async (url, opts) => {
      if (opts.method === 'PATCH') {
        const body = JSON.parse(opts.body);
        if (body.enabled) return { status: 409, body: { detail: 'enabling requires audio' } };
        return { status: 200, body: { ...details['front-door'], enabled: false } };
      }
      return { status: 200, body: inventory };
    }));
    try {
      await client.patch('front-door', { enabled: true });
      assert.fail('expected 409');
    } catch (e) {
      assert.equal(e.status, 409);
      assert.match(e.message, /enabling|audio/i);
    }
  }

  // ---- PATCH 404 unknown camera ----
  {
    const client = CamsAudio.api('/cams-audio', mockFetch(async (url, opts) => {
      if (opts.method === 'PATCH') return { status: 404, body: { detail: 'unknown camera' } };
      return { status: 200, body: {} };
    }));
    try {
      await client.patch('nope', { enabled: true });
      assert.fail('expected 404');
    } catch (e) {
      assert.equal(e.status, 404);
    }
  }

  // ---- back-yard stream from config.stream ----
  {
    const ready = await CamsAudio.loadCameraAudio('back-yard', { fetch: audioFetch() });
    assert.equal(ready.state, 'ready');
    assert.equal(ready.audio.enabled, false);
    assert.equal(ready.audio.stream, 'back-yard-raw');
  }

  // ---- normalizeAudio defaults ----
  {
    const n = CamsAudio.normalizeAudio({ id: 'x', camera_id: 'x', config: {} }, 'x');
    assert.equal(n.enabled, true);
    assert.equal(n.clip_retention_days, 90);
    assert.equal(n.transcript_retention_days, 365);
    assert.equal(n.stream, null);
  }

  // ---- DOM mount: not installed ----
  {
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'front-door', document, editable: true,
      fetch: mockFetch(async () => ({ status: 404, body: {} })),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().kind, 'not_installed');
    assert.match(host.textContent, /Audio service not installed/);
    assert.equal(host.querySelector('#f-audio-enabled').disabled, true);
    panel.destroy();
  }

  // ---- DOM mount: unlinked source shows normal section with Enable off ----
  {
    // Isolate inventory so this camera stays unlinked for the load assertion.
    const localInv = [
      { camera_id: 'front-yard', name: 'Front Yard', has_audio: true, source: null },
    ];
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'front-yard', document, editable: true,
      fetch: mockFetch(async url => {
        if (String(url).endsWith('/api/cameras')) return { status: 200, body: localInv };
        return { status: 404, body: {} };
      }),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().kind, 'ready');
    assert.equal(panel.getState().audio.source, null);
    assert.equal(host.querySelector('#f-audio-enabled').checked, false);
    assert.equal(host.querySelector('#f-audio-enabled').disabled, false);
    assert.ok(!/No audio source linked/.test(host.textContent));
    panel.destroy();
  }

  // ---- Enabling an unlinked camera PATCHes {enabled:true}, refreshes from returned source ----
  {
    const localInv = [
      { camera_id: 'front-yard', name: 'Front Yard', has_audio: true, source: null },
    ];
    let patched = null;
    const createdSource = { id: 'src-front-yard', kind: 'camera' };
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'front-yard', document, editable: true,
      fetch: mockFetch(async (url, opts) => {
        if (String(url).endsWith('/api/cameras')) return { status: 200, body: localInv };
        if (opts.method === 'PATCH') {
          patched = JSON.parse(opts.body);
          localInv[0].source = createdSource;
          return {
            status: 200,
            body: {
              id: 'front-yard', name: 'Front Yard', type: 'camera', enabled: true,
              clip_retention_days: 90, transcript_retention_days: 365, camera_id: 'front-yard',
              config: { stream: null }, source: createdSource,
            },
          };
        }
        return { status: 404, body: {} };
      }),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().audio.source, null);
    const toggle = host.querySelector('#f-audio-enabled');
    assert.equal(toggle.checked, false);
    toggle.checked = true;
    toggle.dispatchEvent('change');
    await new Promise(r => setTimeout(r, 20));
    assert.deepEqual(patched, { enabled: true });
    assert.deepEqual(panel.getState().audio.source, createdSource);
    assert.equal(panel.getState().audio.enabled, true);
    assert.equal(host.querySelector('#f-audio-enabled').checked, true);
    assert.match(host.querySelector('#f-audio-msg').textContent, /linked/i);
    panel.destroy();
  }

  // ---- DOM mount: ready + 409 inline on save ----
  {
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'front-door', document, editable: true,
      fetch: audioFetch(async (url, opts) => {
        if (opts.method === 'PATCH') {
          const patched = JSON.parse(opts.body);
          if (patched.enabled) return { status: 409, body: { detail: 'enabling requires audio' } };
        }
        return null;
      }),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().kind, 'ready');
    assert.equal(host.querySelector('#f-audio-enabled').checked, true);
    assert.equal(host.querySelector('#f-audio-stream').value, '');
    host.querySelector('#f-audio-enabled').checked = true;
    host.querySelector('#f-audio-clip-days').value = '90';
    host.querySelector('#f-audio-transcript-days').value = '365';
    host.querySelector('#f-audio-save').click();
    await new Promise(r => setTimeout(r, 20));
    const msg = host.querySelector('#f-audio-msg');
    assert.match(msg.textContent, /enabling|no audio|Enabled cannot/i);
    assert.equal(msg.className.includes('err'), true);
    panel.destroy();
  }

  // ---- DOM mount: successful save sends enabled + stream ----
  {
    const document = fakeDocument();
    const host = document.createElement('div');
    let patched = null;
    const panel = CamsAudio.mount(host, {
      cameraId: 'back-yard', document, editable: true,
      fetch: audioFetch(async (url, opts) => {
        if (opts.method === 'PATCH') {
          patched = JSON.parse(opts.body);
          return { status: 200, body: {
            ...details['back-yard'],
            enabled: patched.enabled,
            clip_retention_days: patched.clip_retention_days,
            transcript_retention_days: patched.transcript_retention_days,
            config: { stream: patched.stream },
            source: { id: 'src-back-yard' },
          } };
        }
        return null;
      }),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().kind, 'ready');
    assert.equal(host.querySelector('#f-audio-enabled').checked, false);
    assert.equal(host.querySelector('#f-audio-stream').value, 'back-yard-raw');
    host.querySelector('#f-audio-enabled').checked = true;
    host.querySelector('#f-audio-clip-days').value = '120';
    host.querySelector('#f-audio-transcript-days').value = '400';
    host.querySelector('#f-audio-stream').value = 'back-yard-raw';
    host.querySelector('#f-audio-save').click();
    await new Promise(r => setTimeout(r, 20));
    assert.deepEqual(patched, {
      enabled: true, clip_retention_days: 120, transcript_retention_days: 400, stream: 'back-yard-raw',
    });
    assert.match(host.querySelector('#f-audio-msg').textContent, /saved/i);
    panel.destroy();
  }

  // ---- DOM mount: has_audio false hides the section ----
  {
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'mckinley-webcam', document, editable: true, fetch: audioFetch(),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().kind, 'hidden');
    assert.equal(host.childNodes.length, 0);
    panel.destroy();
  }

  // ---- configurable base path ----
  {
    const seen = [];
    await CamsAudio.loadCameraAudio('front-door', {
      basePath: '/custom-audio/',
      fetch: mockFetch(async url => { seen.push(url); return { status: 404, body: {} }; }),
    });
    assert.equal(seen[0], '/custom-audio/api/cameras');
  }

  console.log('camera audio tests passed');
})().catch(e => { console.error(e); process.exit(1); });
