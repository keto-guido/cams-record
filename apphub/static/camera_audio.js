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
    enabled: true,
    clip_retention_days: 90,
    transcript_retention_days: 365,
  });

  function defaults() {
    return { enabled: DEFAULTS.enabled, clip_retention_days: DEFAULTS.clip_retention_days,
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

  function streamOf(body) {
    if (!body || typeof body !== 'object') return null;
    if (body.config && Object.prototype.hasOwnProperty.call(body.config, 'stream')) {
      return body.config.stream == null ? null : String(body.config.stream);
    }
    if (Object.prototype.hasOwnProperty.call(body, 'stream')) {
      return body.stream == null ? null : String(body.stream);
    }
    return null;
  }

  function normalizeAudio(body, cameraId) {
    const b = body || {};
    return {
      enabled: b.enabled != null ? !!b.enabled : DEFAULTS.enabled,
      clip_retention_days: b.clip_retention_days != null ? b.clip_retention_days : DEFAULTS.clip_retention_days,
      transcript_retention_days: b.transcript_retention_days != null
        ? b.transcript_retention_days : DEFAULTS.transcript_retention_days,
      stream: streamOf(b),
      has_audio: true,
      camera_id: b.camera_id || cameraId,
      name: b.name || cameraId,
      type: b.type || 'camera',
      source: Object.prototype.hasOwnProperty.call(b, 'source') ? b.source : undefined,
    };
  }

  /** Unlinked has_audio camera: show the form with Enable off until PATCH creates a source. */
  function unlinkedAudio(cameraId, name) {
    return {
      enabled: false,
      clip_retention_days: DEFAULTS.clip_retention_days,
      transcript_retention_days: DEFAULTS.transcript_retention_days,
      stream: null,
      has_audio: true,
      camera_id: cameraId,
      name: name || cameraId,
      type: 'camera',
      source: null,
    };
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
   *   { state: 'hidden' }              // service up, has_audio false / camera absent
   *   { state: 'ready', audio }        // has_audio true (source may still be null → Enable off)
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
    if (!row) return { state: 'hidden' };
    if (row.has_audio !== true) return { state: 'hidden' };
    if (row.source == null) {
      return { state: 'ready', audio: unlinkedAudio(cameraId, row.name), name: row.name || cameraId, client };
    }
    let detail;
    try {
      detail = await client.get(cameraId);
    } catch (e) {
      if (e.status === 404) return { state: 'error', message: 'Unknown camera for the audio service.' };
      return { state: 'error', message: e.message || 'Audio service error' };
    }
    const audio = normalizeAudio({ ...detail, source: row.source }, cameraId);
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

    function showDisabled(message, kind) {
      clear();
      const fs = el('fieldset', 'adm-audio is-disabled');
      fs.appendChild(el('legend', null, 'Audio'));
      const p = el('p', 'muted sm adm-audio-status', message);
      p.id = 'f-audio-status';
      fs.appendChild(p);
      const enabled = el('label', 'adm-check');
      const ei = el('input'); ei.type = 'checkbox'; ei.id = 'f-audio-enabled'; ei.disabled = true;
      ei.checked = DEFAULTS.enabled;
      enabled.appendChild(ei); enabled.appendChild(doc.createTextNode('Enabled'));
      fs.appendChild(enabled);
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
      state = { kind: kind || 'not_installed', message };
    }

    function applyAudioToForm(audio, ei, ci, ti, si) {
      ei.checked = !!audio.enabled;
      ci.value = String(audio.clip_retention_days);
      ti.value = String(audio.transcript_retention_days);
      si.value = audio.stream == null ? '' : String(audio.stream);
    }

    function showReady(audio, client) {
      clear();
      const fs = el('fieldset', 'adm-audio');
      fs.appendChild(el('legend', null, 'Audio'));
      const status = el('p', 'muted sm adm-audio-status', 'Transcription add-on settings. Saved separately from the camera object.');
      status.id = 'f-audio-status';
      fs.appendChild(status);

      const enabled = el('label', 'adm-check');
      const ei = el('input'); ei.type = 'checkbox'; ei.id = 'f-audio-enabled';
      ei.checked = !!audio.enabled; ei.disabled = o.editable === false;
      enabled.appendChild(ei); enabled.appendChild(doc.createTextNode('Enabled'));
      fs.appendChild(enabled);

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
      const si = el('input'); si.id = 'f-audio-stream';
      si.value = audio.stream == null ? '' : String(audio.stream);
      si.placeholder = 'Optional stream override';
      si.disabled = o.editable === false;
      stream.appendChild(si);
      stream.appendChild(el('span', 'muted sm', 'Optional override sent to the audio service.'));
      fs.appendChild(stream);

      const msg = el('p', 'adm-msg'); msg.id = 'f-audio-msg'; msg.setAttribute('role', 'status');
      fs.appendChild(msg);

      let current = audio;

      async function saveFromForm() {
        msg.textContent = ''; msg.className = 'adm-msg';
        const wantEnabled = !!ei.checked;
        // Unlinked source: enabling creates the source via PATCH {enabled:true}, then refresh from the response.
        const linking = current.source == null && wantEnabled;
        if (!linking) {
          const clipV = validateRetention(ci.value);
          const trV = validateRetention(ti.value);
          if (!clipV.ok) { msg.textContent = clipV.message; msg.className = 'adm-msg err'; return; }
          if (!trV.ok) { msg.textContent = trV.message; msg.className = 'adm-msg err'; return; }
        }
        const saveBtn = fs.querySelector('#f-audio-save');
        if (saveBtn) saveBtn.disabled = true;
        msg.textContent = linking ? 'Linking audio source…' : 'Saving…';
        try {
          const streamVal = String(si.value || '').trim();
          const payload = linking
            ? { enabled: true }
            : {
              enabled: wantEnabled,
              clip_retention_days: validateRetention(ci.value).value,
              transcript_retention_days: validateRetention(ti.value).value,
              stream: streamVal === '' ? null : streamVal,
            };
          const updated = await client.patch(o.cameraId, payload);
          const next = normalizeAudio({
            ...current,
            ...updated,
            config: updated.config || { stream: streamOf(updated) },
            source: Object.prototype.hasOwnProperty.call(updated, 'source') ? updated.source : current.source,
          }, o.cameraId);
          current = next;
          applyAudioToForm(next, ei, ci, ti, si);
          msg.textContent = linking ? 'Audio source linked.' : 'Audio settings saved.';
          msg.className = 'adm-msg adm-ok';
          state = { kind: 'ready', audio: next };
        } catch (e) {
          if (e.status === 409) {
            msg.textContent = e.message || 'Enabled cannot be turned on: this camera has no audio.';
            ei.checked = false;
          } else if (e.status === 404) {
            msg.textContent = 'Unknown camera for the audio service.';
          } else {
            msg.textContent = e.message || 'Could not save audio settings.';
          }
          msg.className = 'adm-msg err';
        } finally {
          if (saveBtn) saveBtn.disabled = false;
        }
      }

      if (o.editable !== false) {
        const actions = el('div', 'adm-audio-actions');
        const save = el('button', 'adm-btn'); save.type = 'button'; save.id = 'f-audio-save';
        save.textContent = 'Save audio';
        actions.appendChild(save);
        fs.appendChild(actions);
        save.addEventListener('click', () => { saveFromForm(); });
        // Turning Enable on while unlinked links the source immediately (PATCH {enabled:true}).
        ei.addEventListener('change', () => {
          if (current.source == null && ei.checked) saveFromForm();
        });
      }
      container.appendChild(fs);
      state = { kind: 'ready', audio: current };
    }

    async function reload() {
      if (!alive) return;
      clear();
      container.appendChild(el('p', 'muted sm', 'Loading audio…'));
      const result = await loadCameraAudio(o.cameraId, o);
      if (!alive) return;
      if (result.state === 'not_installed') {
        showDisabled('Audio service not installed', 'not_installed');
        return;
      }
      if (result.state === 'hidden') {
        clear();
        state = { kind: 'hidden' };
        return;
      }
      if (result.state === 'error') {
        showDisabled(result.message || 'Audio service not installed', 'error');
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
    defaults, validateRetention, classifyListError, normalizeAudio, streamOf, unlinkedAudio,
    api, loadCameraAudio, mount,
  };
});
