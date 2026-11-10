'use strict';
// Self-contained Audio panel for the Cameras Admin tab. Settings live in cams-audio
// (GET/PATCH /cams-audio/api/cameras...), not cams_config. Auth is the same-origin
// apphub_token cookie, so callers only need credentials: 'same-origin'.
//
// Browser: globalThis.CamsAudio. Node tests: require('./camera_audio').

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CamsAudio = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const DEFAULT_BASE = '/cams-audio';
  const RETENTION_MIN = 1;
  const RETENTION_MAX = 3650;
  const DEFAULTS = Object.freeze({
    listening: false,
    clip_retention_days: 90,
    transcript_retention_days: 365,
  });

  function defaults() {
    return { listening: DEFAULTS.listening, clip_retention_days: DEFAULTS.clip_retention_days,
      transcript_retention_days: DEFAULTS.transcript_retention_days };
  }

  function validateRetention(value) {
    if (typeof value === 'boolean' || value === '' || value == null) {
      return { ok: false, message: 'Retention must be a whole number of days from 1 to 3650.' };
    }
    const n = typeof value === 'number' ? value : Number(String(value).trim());
    if (!Number.isInteger(n) || n < RETENTION_MIN || n > RETENTION_MAX) {
      return { ok: false, message: 'Retention must be a whole number of days from 1 to 3650.' };
    }
    return { ok: true, value: n };
  }

  /** Classify a list/GET failure: 404/502/network => service not installed. */
  function classifyListError(err) {
    const status = err && err.status;
    if (status === 404 || status === 502 || status == null || status === 0) return 'not_installed';
    return 'error';
  }

  function api(basePath, fetchImpl) {
    const base = (basePath || DEFAULT_BASE).replace(/\/$/, '');
    const f = fetchImpl || fetch;
    async function req(path, opts) {
      let r;
      try {
        r = await f(base + path, { credentials: 'same-origin', ...opts,
          headers: { Accept: 'application/json', ...(opts && opts.body ? { 'Content-Type': 'application/json' } : {}),
            ...(opts && opts.headers) } });
      } catch (e) {
        const err = new Error('network');
        err.status = 0;
        err.cause = e;
        throw err;
      }
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const err = new Error((body && (body.detail || body.message)) || ('Request failed (' + r.status + ')'));
        err.status = r.status;
        err.body = body;
        throw err;
      }
      return body;
    }
    return {
      base,
      list: () => req('/api/cameras'),
      get: id => req('/api/cameras/' + encodeURIComponent(id) + '/audio'),
      patch: (id, partial) => req('/api/cameras/' + encodeURIComponent(id) + '/audio', {
        method: 'PATCH', body: JSON.stringify(partial || {}),
      }),
    };
  }

  /**
   * Load audio for one camera. Returns:
   *   { state: 'not_installed' }
   *   { state: 'hidden' }           // service up, has_audio false / unknown camera with no audio
   *   { state: 'ready', audio }     // has_audio true
   *   { state: 'error', message }
   */
  async function loadCameraAudio(cameraId, opts) {
    const o = opts || {};
    const client = api(o.basePath, o.fetch);
    let list;
    try {
      list = await client.list();
    } catch (e) {
      if (classifyListError(e) === 'not_installed') return { state: 'not_installed' };
      return { state: 'error', message: e.message || 'Audio service error' };
    }
    if (!Array.isArray(list)) return { state: 'not_installed' };
    const row = list.find(c => c && c.camera_id === cameraId);
    if (!row) {
      // Service is installed but this camera is absent from its inventory: no Audio section.
      return { state: 'hidden' };
    }
    if (!row.audio || row.audio.has_audio !== true) return { state: 'hidden' };
    const audio = {
      listening: !!row.audio.listening,
      clip_retention_days: row.audio.clip_retention_days != null ? row.audio.clip_retention_days : DEFAULTS.clip_retention_days,
      transcript_retention_days: row.audio.transcript_retention_days != null
        ? row.audio.transcript_retention_days : DEFAULTS.transcript_retention_days,
      stream: row.audio.stream == null ? null : String(row.audio.stream),
      has_audio: true,
    };
    return { state: 'ready', audio, name: row.name || cameraId, client };
  }

  /**
   * Mount the Audio fieldset into `container`. Clears container first.
   * opts: { cameraId, basePath, fetch, editable, document }
   * Returns { reload, destroy, getState }.
   */
  function mount(container, opts) {
    if (!container) throw new Error('container required');
    const o = opts || {};
    const doc = o.document || (typeof document !== 'undefined' ? document : null);
    if (!doc) throw new Error('document required');
    let state = { kind: 'loading' };
    let alive = true;
    const el = (tag, cls, text) => {
      const e = doc.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    };

    function clear() { while (container.firstChild) container.removeChild(container.firstChild); }

    function showDisabled(message) {
      clear();
      const fs = el('fieldset', 'adm-audio is-disabled');
      fs.appendChild(el('legend', null, 'Audio'));
      const p = el('p', 'muted sm adm-audio-status', message);
      p.id = 'f-audio-status';
      fs.appendChild(p);
      // Keep the shape visible but inert so the layout stays predictable.
      const listening = el('label', 'adm-check');
      const li = el('input'); li.type = 'checkbox'; li.id = 'f-audio-listening'; li.disabled = true;
      listening.appendChild(li); listening.appendChild(doc.createTextNode('Listening'));
      fs.appendChild(listening);
      [['Clip retention, days', 'f-audio-clip-days', DEFAULTS.clip_retention_days],
        ['Transcript retention, days', 'f-audio-transcript-days', DEFAULTS.transcript_retention_days]]
        .forEach(([label, id, val]) => {
          const l = el('label', 'adm-field'); l.appendChild(doc.createTextNode(label));
          const i = el('input'); i.type = 'number'; i.id = id; i.min = String(RETENTION_MIN);
          i.max = String(RETENTION_MAX); i.step = '1'; i.value = String(val); i.disabled = true;
          l.appendChild(i); fs.appendChild(l);
        });
      const stream = el('label', 'adm-field'); stream.appendChild(doc.createTextNode('Audio stream'));
      const si = el('input'); si.id = 'f-audio-stream'; si.readOnly = true; si.disabled = true; si.value = '';
      stream.appendChild(si); fs.appendChild(stream);
      container.appendChild(fs);
      state = { kind: 'not_installed', message };
    }

    function showReady(audio, client) {
      clear();
      const fs = el('fieldset', 'adm-audio');
      fs.appendChild(el('legend', null, 'Audio'));
      const status = el('p', 'muted sm adm-audio-status', 'Transcription add-on settings. Saved separately from the camera object.');
      status.id = 'f-audio-status';
      fs.appendChild(status);

      const listening = el('label', 'adm-check');
      const li = el('input'); li.type = 'checkbox'; li.id = 'f-audio-listening';
      li.checked = !!audio.listening; li.disabled = o.editable === false;
      listening.appendChild(li); listening.appendChild(doc.createTextNode('Listening'));
      fs.appendChild(listening);

      const clip = el('label', 'adm-field'); clip.appendChild(doc.createTextNode('Clip retention, days'));
      const ci = el('input'); ci.type = 'number'; ci.id = 'f-audio-clip-days';
      ci.min = String(RETENTION_MIN); ci.max = String(RETENTION_MAX); ci.step = '1';
      ci.value = String(audio.clip_retention_days); ci.disabled = o.editable === false;
      clip.appendChild(ci); fs.appendChild(clip);

      const tr = el('label', 'adm-field'); tr.appendChild(doc.createTextNode('Transcript retention, days'));
      const ti = el('input'); ti.type = 'number'; ti.id = 'f-audio-transcript-days';
      ti.min = String(RETENTION_MIN); ti.max = String(RETENTION_MAX); ti.step = '1';
      ti.value = String(audio.transcript_retention_days); ti.disabled = o.editable === false;
      tr.appendChild(ti); fs.appendChild(tr);

      const stream = el('label', 'adm-field'); stream.appendChild(doc.createTextNode('Audio stream'));
      const si = el('input'); si.id = 'f-audio-stream'; si.readOnly = true;
      si.value = audio.stream == null ? '' : String(audio.stream);
      si.placeholder = audio.stream == null ? 'Default (camera stream)' : '';
      stream.appendChild(si);
      stream.appendChild(el('span', 'muted sm', 'Read-only override from the audio service.'));
      fs.appendChild(stream);

      const msg = el('p', 'adm-msg'); msg.id = 'f-audio-msg'; msg.setAttribute('role', 'status');
      fs.appendChild(msg);

      if (o.editable !== false) {
        const actions = el('div', 'adm-audio-actions');
        const save = el('button', 'adm-btn'); save.type = 'button'; save.id = 'f-audio-save';
        save.textContent = 'Save audio';
        actions.appendChild(save);
        fs.appendChild(actions);
        save.addEventListener('click', async () => {
          msg.textContent = ''; msg.className = 'adm-msg';
          const clipV = validateRetention(ci.value);
          const trV = validateRetention(ti.value);
          if (!clipV.ok) { msg.textContent = clipV.message; msg.className = 'adm-msg err'; return; }
          if (!trV.ok) { msg.textContent = trV.message; msg.className = 'adm-msg err'; return; }
          save.disabled = true; msg.textContent = 'Saving…';
          try {
            const updated = await client.patch(o.cameraId, {
              listening: !!li.checked,
              clip_retention_days: clipV.value,
              transcript_retention_days: trV.value,
            });
            li.checked = !!updated.listening;
            if (updated.clip_retention_days != null) ci.value = String(updated.clip_retention_days);
            if (updated.transcript_retention_days != null) ti.value = String(updated.transcript_retention_days);
            if ('stream' in updated) si.value = updated.stream == null ? '' : String(updated.stream);
            msg.textContent = 'Audio settings saved.';
            msg.className = 'adm-msg adm-ok';
            state = { kind: 'ready', audio: { ...audio, ...updated, has_audio: true } };
          } catch (e) {
            if (e.status === 409) {
              msg.textContent = e.message || 'Listening cannot be turned on: this camera has no audio.';
              li.checked = false;
            } else if (e.status === 404) {
              msg.textContent = 'Unknown camera for the audio service.';
            } else {
              msg.textContent = e.message || 'Could not save audio settings.';
            }
            msg.className = 'adm-msg err';
          } finally { save.disabled = false; }
        });
      }
      container.appendChild(fs);
      state = { kind: 'ready', audio };
    }

    async function reload() {
      if (!alive) return;
      clear();
      container.appendChild(el('p', 'muted sm', 'Loading audio…'));
      const result = await loadCameraAudio(o.cameraId, o);
      if (!alive) return;
      if (result.state === 'not_installed') {
        showDisabled('Audio service not installed');
        return;
      }
      if (result.state === 'hidden') {
        clear();
        state = { kind: 'hidden' };
        return;
      }
      if (result.state === 'error') {
        showDisabled('Audio service not installed');
        state = { kind: 'not_installed', message: result.message };
        return;
      }
      showReady(result.audio, result.client || api(o.basePath, o.fetch));
    }

    reload();
    return {
      reload,
      destroy() { alive = false; clear(); },
      getState() { return state; },
    };
  }

  return {
    DEFAULT_BASE, RETENTION_MIN, RETENTION_MAX, DEFAULTS,
    defaults, validateRetention, classifyListError, api, loadCameraAudio, mount,
  };
});
