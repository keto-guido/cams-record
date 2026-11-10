'use strict';
// CamsAudio panel: installed / not-installed / 409, retention validation, DOM mount.
// Run: node apphub/test_camera_audio.js
const assert = require('assert');
const CamsAudio = require('./static/camera_audio');

assert.deepEqual(CamsAudio.defaults(), {
  listening: false, clip_retention_days: 90, transcript_retention_days: 365,
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

const inventory = [
  { camera_id: 'front-door', name: 'Front Door',
    audio: { listening: true, clip_retention_days: 90, transcript_retention_days: 365, stream: null, has_audio: true } },
  { camera_id: 'back-yard', name: 'Back Yard',
    audio: { listening: false, clip_retention_days: 90, transcript_retention_days: 365, stream: 'back-yard-raw', has_audio: true } },
  { camera_id: 'mckinley-webcam', name: 'McKinley Webcam',
    audio: { listening: false, clip_retention_days: 90, transcript_retention_days: 365, stream: null, has_audio: false } },
];

(async () => {
  // ---- installed: load front-door ----
  {
    const ready = await CamsAudio.loadCameraAudio('front-door', {
      fetch: mockFetch(async url => {
        assert.match(url, /\/cams-audio\/api\/cameras$/);
        return { status: 200, body: inventory };
      }),
    });
    assert.equal(ready.state, 'ready');
    assert.equal(ready.audio.has_audio, true);
    assert.equal(ready.audio.listening, true);
    assert.equal(ready.audio.stream, null);
  }

  // ---- has_audio false => hidden (no hard-coded camera list) ----
  {
    const hidden = await CamsAudio.loadCameraAudio('mckinley-webcam', {
      fetch: mockFetch(async () => ({ status: 200, body: inventory })),
    });
    assert.equal(hidden.state, 'hidden');
  }

  // ---- camera absent from audio inventory => hidden ----
  {
    const hidden = await CamsAudio.loadCameraAudio('driveway', {
      fetch: mockFetch(async () => ({ status: 200, body: inventory })),
    });
    assert.equal(hidden.state, 'hidden');
  }

  // ---- not installed: 404 / 502 / network ----
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

  // ---- PATCH 409 when listening cannot be enabled ----
  {
    const client = CamsAudio.api('/cams-audio', mockFetch(async (url, opts) => {
      if (opts.method === 'PATCH') {
        const body = JSON.parse(opts.body);
        if (body.listening) return { status: 409, body: { detail: 'listening requires has_audio' } };
        return { status: 200, body: { listening: false, clip_retention_days: 90, transcript_retention_days: 365, stream: null, has_audio: true } };
      }
      return { status: 200, body: inventory };
    }));
    try {
      await client.patch('front-door', { listening: true });
      assert.fail('expected 409');
    } catch (e) {
      assert.equal(e.status, 409);
      assert.match(e.message, /listening|has_audio/i);
    }
  }

  // ---- back-yard stream override is preserved ----
  {
    const ready = await CamsAudio.loadCameraAudio('back-yard', {
      fetch: mockFetch(async () => ({ status: 200, body: inventory })),
    });
    assert.equal(ready.state, 'ready');
    assert.equal(ready.audio.stream, 'back-yard-raw');
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
    assert.equal(host.querySelector('#f-audio-listening').disabled, true);
    panel.destroy();
  }

  // ---- DOM mount: ready + 409 inline on save ----
  {
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'front-door', document, editable: true,
      fetch: mockFetch(async (url, opts) => {
        if (String(url).endsWith('/api/cameras')) return { status: 200, body: inventory };
        if (opts.method === 'PATCH') {
          const patched = JSON.parse(opts.body);
          if (patched.listening) return { status: 409, body: { detail: 'listening requires has_audio' } };
          return { status: 200, body: { ...inventory[0].audio, ...patched } };
        }
        return { status: 404, body: {} };
      }),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().kind, 'ready');
    assert.equal(host.querySelector('#f-audio-listening').checked, true);
    assert.equal(host.querySelector('#f-audio-stream').value, '');
    host.querySelector('#f-audio-listening').checked = true;
    host.querySelector('#f-audio-clip-days').value = '90';
    host.querySelector('#f-audio-transcript-days').value = '365';
    host.querySelector('#f-audio-save').click();
    await new Promise(r => setTimeout(r, 20));
    const msg = host.querySelector('#f-audio-msg');
    assert.match(msg.textContent, /has_audio|Listening cannot/i);
    assert.equal(msg.className.includes('err'), true);
    panel.destroy();
  }

  // ---- DOM mount: has_audio false hides the section ----
  {
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'mckinley-webcam', document, editable: true,
      fetch: mockFetch(async () => ({ status: 200, body: inventory })),
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
