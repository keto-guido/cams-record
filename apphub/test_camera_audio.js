'use strict';
// CamsAudio panel: installed / null-source GET / enable-links-source / 409, retention, DOM mount.
// Run: node apphub/test_camera_audio.js
const assert = require('assert');
const CamsAudio = require('./static/camera_audio');

assert.deepEqual(CamsAudio.defaults(), {
  enabled: true, clip_retention_days: 90, transcript_retention_days: 365,
});
assert.equal(CamsAudio.validateRetention(90).ok, true);
assert.equal(CamsAudio.validateRetention(0).ok, false);
assert.equal(CamsAudio.validateRetention(3651).ok, false);

function mockFetch(handler) {
  return async (url, opts) => {
    const res = await handler(url, opts || {});
    const status = res.status || 200;
    const body = Object.prototype.hasOwnProperty.call(res, 'body') ? res.body : {};
    return {
      ok: status >= 200 && status < 300,
      status,
      async text() {
        if (body === null) return 'null';
        return JSON.stringify(body);
      },
      async json() { return body; },
    };
  };
}

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

function sourceObj(id, name, extra) {
  return {
    id, name, type: 'camera', enabled: true,
    clip_retention_days: 90, transcript_retention_days: 365, camera_id: id,
    config: { stream: null }, ...(extra || {}),
  };
}

(async () => {
  // ---- sourceFromResponse: body IS the source; nested source also accepted ----
  {
    const direct = { id: 'front-door', enabled: true, camera_id: 'front-door', config: { stream: null } };
    assert.strictEqual(CamsAudio.sourceFromResponse(direct), direct);
    const nested = { source: direct, enabled: true };
    assert.strictEqual(CamsAudio.sourceFromResponse(nested), direct);
    assert.strictEqual(CamsAudio.sourceFromResponse(null), null);
  }

  // ---- linked: list + GET source object ----
  {
    const src = sourceObj('front-door', 'Front Door');
    const ready = await CamsAudio.loadCameraAudio('front-door', {
      fetch: mockFetch(async url => {
        if (String(url).endsWith('/api/cameras')) {
          return { status: 200, body: [{ camera_id: 'front-door', name: 'Front Door', has_audio: true, source: { id: 'src-front-door' } }] };
        }
        return { status: 200, body: src };
      }),
    });
    assert.equal(ready.state, 'ready');
    assert.equal(ready.audio.enabled, true);
    assert.equal(ready.audio.source.id, 'front-door');
  }

  // ---- GET null body (no linked source) => ready with Enable off ----
  {
    const r = await CamsAudio.loadCameraAudio('front-yard', {
      fetch: mockFetch(async url => {
        if (String(url).endsWith('/api/cameras')) {
          return { status: 200, body: [{ camera_id: 'front-yard', name: 'Front Yard', has_audio: true, source: null }] };
        }
        return { status: 200, body: null };
      }),
    });
    assert.equal(r.state, 'ready');
    assert.equal(r.audio.enabled, false);
    assert.equal(r.audio.source, null);
  }

  // ---- has_audio false => hidden ----
  {
    const hidden = await CamsAudio.loadCameraAudio('mckinley-webcam', {
      fetch: mockFetch(async () => ({
        status: 200,
        body: [{ camera_id: 'mckinley-webcam', name: 'McKinley Webcam', has_audio: false, source: null }],
      })),
    });
    assert.equal(hidden.state, 'hidden');
  }

  // ---- not installed on list ----
  for (const status of [404, 502, 0]) {
    const r = await CamsAudio.loadCameraAudio('front-door', {
      fetch: mockFetch(async () => {
        if (status === 0) throw new Error('boom');
        return { status, body: { detail: 'gone' } };
      }),
    });
    assert.equal(r.state, 'not_installed', 'status ' + status);
  }

  // ---- DOM: Enable on unlinked camera PATCHes {enabled:true}; response body IS the source ----
  {
    const localInv = [{ camera_id: 'front-yard', name: 'Front Yard', has_audio: true, source: null }];
    let patched = null;
    const created = sourceObj('front-yard', 'Front Yard', { enabled: true });
    const document = fakeDocument();
    const host = document.createElement('div');
    const panel = CamsAudio.mount(host, {
      cameraId: 'front-yard', document, editable: true,
      fetch: mockFetch(async (url, opts) => {
        if (String(url).endsWith('/api/cameras')) return { status: 200, body: localInv };
        if (opts.method === 'PATCH') {
          patched = JSON.parse(opts.body);
          localInv[0].source = { id: 'src-front-yard' };
          return { status: 200, body: created }; // source object itself
        }
        return { status: 200, body: null };
      }),
    });
    await new Promise(r => setTimeout(r, 20));
    assert.equal(panel.getState().audio.source, null);
    const toggle = host.querySelector('#f-audio-enabled');
    toggle.checked = true;
    toggle.dispatchEvent('change');
    await new Promise(r => setTimeout(r, 20));
    assert.deepEqual(patched, { enabled: true });
    assert.equal(panel.getState().audio.enabled, true);
    assert.equal(panel.getState().audio.source.id, 'front-yard');
    assert.match(host.querySelector('#f-audio-msg').textContent, /linked/i);

    // ---- Save after auto-create still sends retention and stream ----
    patched = null;
    host.querySelector('#f-audio-clip-days').value = '120';
    host.querySelector('#f-audio-transcript-days').value = '400';
    host.querySelector('#f-audio-stream').value = 'front-yard-raw';
    host.querySelector('#f-audio-save').click();
    await new Promise(r => setTimeout(r, 20));
    assert.deepEqual(patched, {
      enabled: true, clip_retention_days: 120, transcript_retention_days: 400, stream: 'front-yard-raw',
    });
    assert.match(host.querySelector('#f-audio-msg').textContent, /saved/i);
    panel.destroy();
  }

  // ---- nested source on PATCH still accepted ----
  {
    const nested = { source: sourceObj('back-yard', 'Back Yard', { enabled: false, config: { stream: 'back-yard-raw' } }) };
    const n = CamsAudio.normalizeAudio(CamsAudio.sourceFromResponse(nested), 'back-yard');
    assert.equal(n.enabled, false);
    assert.equal(n.stream, 'back-yard-raw');
  }

  // ---- 409 on enable ----
  {
    const client = CamsAudio.api('/cams-audio', mockFetch(async (url, opts) => {
      if (opts.method === 'PATCH') return { status: 409, body: { detail: 'enabling requires audio' } };
      return { status: 200, body: {} };
    }));
    try {
      await client.patch('x', { enabled: true });
      assert.fail('expected 409');
    } catch (e) {
      assert.equal(e.status, 409);
    }
  }

  console.log('camera audio tests passed');
})().catch(e => { console.error(e); process.exit(1); });
