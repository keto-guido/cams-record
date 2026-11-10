// /cams/ camera app: Cameras grid (snapshots + cams-detect motion overlay), Detections tab, per-camera page.
// Routes (hash): "" grid, "#detections" all detections, "#cam=<id>" camera page (live view + its detections).
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const CD = '/cams-detect', REFRESH = 7000, FAST = 1000, TAIL = 15000, EV_POLL = 12000, PAGE = 25;
  const tiles = [...document.querySelectorAll('.camtile')];
  const hubCams = tiles.map(t => ({ id: t.dataset.id, name: t.dataset.name }));
  const visible = () => document.visibilityState === 'visible';
  const cap = s => String(s || '').charAt(0).toUpperCase() + String(s || '').slice(1);
  const fmt = ts => new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' });
  let route = { view: 'grid' };

  // ---------- snapshots grid ----------
  let timer = null;
  function refresh() {
    tiles.forEach(t => {
      const img = t.querySelector('img'), age = t.querySelector('.camage'), n = new Image();
      n.onload = () => { img.src = n.src; t.classList.remove('off'); age.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }); };
      n.onerror = () => { t.classList.add('off'); age.textContent = 'Camera unavailable'; };
      n.src = '/cams/api/snap/' + encodeURIComponent(t.dataset.id) + '?t=' + Date.now();
    });
  }
  const gridStart = () => { gridStop(); refresh(); timer = setInterval(refresh, REFRESH); };
  const gridStop = () => { clearInterval(timer); timer = null; };

  // ---------- cams-detect mapping + motion overlay ----------
  const cd = {};            // hub camera id -> overlay state
  const detOf = {};         // hub camera id -> cams-detect camera id (configured + enabled)
  const detName = {};       // cams-detect id -> name
  let cdReady = null;
  function cdThumb(c) {
    const n = new Image();
    n.onload = () => { c.img.src = n.src; c.t.classList.remove('off'); };
    n.src = CD + '/api/frame/' + encodeURIComponent(c.det) + '.jpg?t=' + Date.now();
  }
  function cdTick() { if (!visible() || route.view !== 'grid') return; Object.values(cd).forEach(c => { if (c.active || Date.now() - c.ended < TAIL) cdThumb(c); }); }
  function cdApply(m) {
    const hub = Object.keys(detOf).find(h => detOf[h] === m.camera), c = hub && cd[hub]; if (!c) return;
    const objs = m.objects || [], was = c.active;
    c.active = !!m.motion || objs.length > 0;
    if (was && !c.active) c.ended = Date.now();
    c.t.classList.toggle('cd-detect', objs.length > 0);
    c.t.classList.toggle('cd-motion', !!m.motion && !objs.length);
    c.chips.replaceChildren(...objs.map(o => { const e = document.createElement('span'); e.className = 'cd-chip'; e.textContent = cap(o.label) + ' ' + Math.round(o.score * 100) + '%'; return e; }));
    if (c.active && !was && route.view === 'grid') cdThumb(c);
  }
  let es = null, retry = 2000;
  function cdConnect() {
    if (es || !Object.keys(cd).length) return;
    es = new EventSource(CD + '/api/stream');
    es.onopen = () => { retry = 2000; };
    es.onmessage = ev => { try { cdApply(JSON.parse(ev.data)); } catch (e) {} };
    es.onerror = () => { if (es && es.readyState === EventSource.CLOSED) { es = null; setTimeout(() => { if (visible()) cdConnect(); }, retry); retry = Math.min(retry * 2, 60000); } };
  }
  function cdStop() { if (es) { es.close(); es = null; } }
  cdReady = fetch(CD + '/api/cameras', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : []).catch(() => []).then(list => {
    (list || []).filter(x => x && x.configured && x.enabled !== false).forEach(x => {
      detName[x.id] = x.name || x.id;
      const hub = hubCams.find(h => h.id === x.id || h.id === x.stream);
      if (hub) detOf[hub.id] = x.id;
    });
    tiles.forEach(t => {
      const det = detOf[t.dataset.id]; if (!det) return;
      const chips = document.createElement('div'); chips.className = 'cd-chips'; t.appendChild(chips);
      cd[t.dataset.id] = { t, det, img: t.querySelector('img'), chips, active: false, ended: 0 };
    });
    if (Object.keys(cd).length) { setInterval(cdTick, FAST); if (visible()) cdConnect(); }
    fillCamFilter();
  });

  // ---------- save to Photos ----------
  // iOS only offers "Save Image/Video" from the share sheet, and <a download> lands in Files. Sharing needs the bytes up
  // front and a live tap: a slow fetch outlasts the tap, so the file is buffered first and a second tap ("Save to Photos")
  // shares it. Desktop browsers expose navigator.share too but have no Photos step, so they keep the plain link.
  const MAX_SHARE = 250e6, FAST_SHARE_MS = 1000, OBJECT_URL_MS = 60000;
  const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const PHONE = IOS || /Android/.test(navigator.userAgent);
  const shareable = file => { try { return PHONE && typeof navigator.share === 'function' && typeof navigator.canShare === 'function' && navigator.canShare({ files: [file] }); } catch (e) { return false; } };
  const fileStamp = ts => { const d = new Date(ts * 1000), p = n => String(n).padStart(2, '0'); return '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()); };
  function dispositionName(header) {
    let m = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(header || ''), name = '';
    if (m) try { name = decodeURIComponent(m[1].trim()); } catch (e) { name = ''; }
    if (!name && (m = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(header || ''))) name = (m[1] || m[2] || '').trim();
    return name.split(/[\\/]/).pop();
  }
  function saveIdle(j) { if (j.label) j.link.textContent = j.label; j.label = ''; j.link.removeAttribute('aria-busy'); }
  function saveRelease(j) {
    if (j.ctl) j.ctl.abort();
    clearTimeout(j.timer);
    if (j.objUrl) URL.revokeObjectURL(j.objUrl);
    if (j.alt) { j.alt.remove(); j.link.hidden = false; }
    saveIdle(j);
    Object.assign(j, { ctl: null, file: null, alt: null, objUrl: null, plain: false });
  }
  function saveSwap(j, el) { el.classList.add('cd-save'); if (j.alt) j.alt.remove(); j.alt = el; j.link.after(el); j.link.hidden = true; el.focus(); }
  function saveFallback(j, file) {
    const a = document.createElement('a');
    j.objUrl = URL.createObjectURL(file); a.href = j.objUrl; a.download = file.name; a.textContent = 'Save file';
    a.addEventListener('click', () => { clearTimeout(j.timer); j.timer = setTimeout(() => saveRelease(j), OBJECT_URL_MS); });
    saveSwap(j, a);
  }
  // navigator.share() must run synchronously inside the tap: no await may come before it.
  async function saveShare(j, file, fast) {
    if (j.sharing) return;
    j.sharing = true;
    try { await navigator.share({ files: [file] }); if (j.file === file) saveRelease(j); }
    catch (e) { if (j.file === file && e.name !== 'AbortError' && !(fast && e.name === 'NotAllowedError')) saveFallback(j, file); }
    finally { j.sharing = false; }
  }
  function saveOffer(j, file, fast) {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = 'Save to Photos'; b.addEventListener('click', () => saveShare(j, file, false));
    saveSwap(j, b);
    if (fast) saveShare(j, file, true);
  }
  // Oversized but only known once the response arrived: the plain link needs a new tap, as an aborted export holds its slot for a moment.
  function saveTooBig(j) { saveRelease(j); j.plain = true; j.status.textContent = IOS ? 'Large file: tap Download again to save it to Files; open it there and choose Save Video' : 'Large file: tap Download again.'; }
  async function savePrepare(j, o, max) {
    const ctl = j.ctl = new AbortController(), began = performance.now(), link = j.link;
    j.label = link.textContent; link.textContent = 'Preparing…'; link.setAttribute('aria-busy', 'true');
    try {
      const r = await fetch(o.url, { credentials: 'same-origin', signal: ctl.signal });
      if (!r.ok) throw new Error(r.status === 429 ? 'Another export is running. Try again shortly.' : 'Download failed (' + r.status + ').');
      if (+r.headers.get('content-length') > max) return saveTooBig(j);
      const served = (r.headers.get('content-type') || '').split(';')[0].trim(), type = /^(image|video)\//i.test(served) ? served : o.type;
      const name = (o.keepServerName && dispositionName(r.headers.get('content-disposition'))) || o.filename;
      let blob;
      if (r.body && r.body.getReader) {
        const reader = r.body.getReader(), parts = []; let got = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          parts.push(value); got += value.length;
          if (got > max) return saveTooBig(j);
          link.textContent = 'Preparing… ' + Math.round(got / 1e6) + ' MB';
        }
        blob = new Blob(parts, { type });
      } else blob = await r.blob();
      if (j.ctl !== ctl) return;
      const file = new File([blob], name, { type });
      j.ctl = null; j.file = file; saveIdle(j);
      if (shareable(file)) saveOffer(j, file, performance.now() - began < FAST_SHARE_MS); else saveFallback(j, file);
    } catch (e) {
      if (j.ctl !== ctl) return;
      saveRelease(j); j.status.textContent = e instanceof TypeError ? 'Download failed. Check your connection.' : e.message;
    }
  }
  // Returns true when it took over the tap; false leaves the <a download> to navigate as usual.
  function saveMedia({ url, filename, type, maxShareBytes = MAX_SHARE, size = 0, keepServerName = false, job }) {
    if (job.ctl) return true;
    if (job.plain || !shareable(new File(['x'], filename, { type }))) return false;
    if (size > maxShareBytes) { if (IOS) job.status.textContent = 'Large file: saved to Files; open it there and choose Save Video'; return false; }
    savePrepare(job, { url, filename, type, keepServerName }, maxShareBytes);
    return true;
  }
  function saveJob(link, status) {
    const j = { link, status, opts: null, ctl: null, file: null, alt: null, objUrl: null, timer: 0, label: '', plain: false, sharing: false };
    link.addEventListener('click', ev => { if (j.opts && saveMedia({ ...j.opts, job: j })) ev.preventDefault(); });
    return j;
  }
  function saveArm(j, opts) {
    if (j.opts && j.opts.url === opts.url && (j.ctl || j.alt)) return; // A retry must not discard a file being prepared.
    saveRelease(j); j.opts = opts; j.link.href = opts.url; j.link.download = opts.keepServerName ? '' : opts.filename; j.link.hidden = false;
  }

  // ---------- detection clip player (modal) ----------
  // Plays a low-res (sub) clip from cams-record around the detection; downloads use full-res (main) when still kept.
  // The low-res snapshot carries the aligned boxes; the high-res one is grabbed later without boxes, so it is opt-in (HD) and the download.
  const CR = '/cams-record', STALL_MS = 12000, PRE = 5, POST = 5, NOEND = 10, MAXWIN = 120, CLIP_RETRY = 15000;
  const lb = $('cd-lb'), lbV = lb.querySelector('video'), lbImg = lb.querySelector('img'), lbInfo = lb.querySelector('.cd-lb-info'),
    lbMsg = lb.querySelector('.cd-lb-msg'), lbSnap = lb.querySelector('.cd-dl-snap'), lbClip = lb.querySelector('.cd-dl-clip'), lbNote = lb.querySelector('.cd-lb-note'), lbHd = lb.querySelector('.cd-lb-hd');
  const lbSnapSave = saveJob(lbSnap, lbNote), lbClipSave = saveJob(lbClip, lbNote);
  let lbEv = null, lbTimer = null, lbLow = '', lbHigh = '', lbHiRes = false;
  const recCam = e => Object.keys(detOf).find(h => detOf[h] === e.camera) || e.camera;
  function win(e) {
    const s = e.start_ts - PRE; let t = (e.end_ts && e.end_ts > e.start_ts ? e.end_ts : e.start_ts + NOEND) + POST;
    if (t - s > MAXWIN) t = s + MAXWIN; return [Math.floor(s), Math.ceil(t)];
  }
  const clipQ = (e, q, dl) => { const [s, t] = win(e); return CR + '/api/clip?' + new URLSearchParams({ camera: recCam(e), start: s, end: t, quality: q, download: dl ? 1 : 0 }); };
  const infoQ = e => { const [s, t] = win(e); return CR + '/api/clip/info?' + new URLSearchParams({ camera: recCam(e), start: s, end: t }); };
  const stOf = (j, q) => { const x = j && j[q]; return typeof x === 'string' ? x : (x && (x.status || x.state)) || (x && x.available ? 'available' : 'unknown'); };
  function lbClose() {
    lb.hidden = true; clearTimeout(lbTimer); lbTimer = null; lbEv = null;
    lbV.pause(); lbV.removeAttribute('src'); lbV.load(); lbV.hidden = true;
    lbImg.onload = null; lbImg.onerror = null; lbImg.removeAttribute('src');
    delete lbImg.dataset.snapRetry; delete lbImg.dataset.snapBust;
    lbHiRes = false; lbHd.hidden = true; lbHd.setAttribute('aria-pressed', 'false');
    saveRelease(lbSnapSave); saveRelease(lbClipSave);
    document.body.classList.remove('cd-lb-open');
  }
  const lbPlay = () => { const p = lbV.play(); if (p && p.catch) p.catch(() => {}); };
  // HD takes over from the clip while on; turning it off returns to the clip, or to the low-res snapshot if there is none.
  function lbHdSet(on) {
    lbHiRes = on; lbHd.setAttribute('aria-pressed', String(on)); lbImg.src = on ? lbHigh : lbLow;
    const clip = !!lbV.getAttribute('src');
    lbImg.hidden = clip && !on; lbV.hidden = !clip || on;
    if (clip) { if (on) lbV.pause(); else lbPlay(); }
  }
  function lbTry(e) {
    if (lbEv !== e) return;
    fetch(infoQ(e), { credentials: 'same-origin' }).then(r => r.ok ? r.json() : Promise.reject(r.status)).then(j => {
      if (lbEv !== e) return;
      const sub = stOf(j, 'sub'), main = stOf(j, 'main');
      const armClip = q => saveArm(lbClipSave, { url: clipQ(e, q, 1), filename: recCam(e) + '_' + fileStamp(e.start_ts) + '_clip.mp4', type: 'video/mp4', keepServerName: true });
      if (main === 'available') { armClip('main'); lbNote.textContent = ''; }
      else if (sub === 'available') { armClip('sub'); lbNote.textContent = main === 'not_ready' ? '' : 'Full-res footage is no longer kept; the clip download is low-res.'; }
      const play = q => { lbMsg.textContent = q === 'main' ? 'Low-res copy unavailable; playing full-res.' : ''; lbV.src = clipQ(e, q, 0); if (!lbHiRes) { lbImg.hidden = true; lbV.hidden = false; lbPlay(); } };
      if (sub === 'available') play('sub');
      else if (sub === 'not_ready' || main === 'not_ready') { lbMsg.textContent = 'Clip available in about a minute…'; lbTimer = setTimeout(() => lbTry(e), CLIP_RETRY); }
      else if (main === 'available') play('main');
      else lbMsg.textContent = 'No recording available for this detection.';
    }).catch(st => {
      if (lbEv !== e) return;
      lbMsg.textContent = st === 425 ? 'Clip available in about a minute…' : 'Clip unavailable right now. Retrying…';
      lbTimer = setTimeout(() => lbTry(e), CLIP_RETRY);
    });
  }
  // Snapshot load: retry a few times (push deep-links can race the file), then fall back to the other resolution.
  const SNAP_RETRY = 4, SNAP_RETRY_MS = 600;
  function lbSnapUrl(url, bust) { return bust ? url + (url.indexOf('?') >= 0 ? '&' : '?') + 't=' + Date.now() : url; }
  function lbArmSnapshot(e, low, high) {
    lbImg.onload = null; lbImg.onerror = null; delete lbImg.dataset.snapRetry; delete lbImg.dataset.snapBust;
    const primary = e.has_snapshot ? low : (e.has_snapshot_hires ? high : '');
    const fallback = (primary === low && e.has_snapshot_hires) ? high : ((primary === high && e.has_snapshot) ? low : '');
    lbLow = primary || high || low; lbHigh = (e.has_snapshot && e.has_snapshot_hires) ? high : '';
    lbHd.hidden = !lbHigh;
    if (!primary) { lbImg.removeAttribute('src'); lbImg.hidden = true; lbSnap.hidden = true; return; }
    lbImg.alt = cap(e.label) + ' at ' + (e.camera_name || detName[e.camera] || e.camera);
    lbImg.hidden = false;
    saveArm(lbSnapSave, { url: e.has_snapshot_hires ? high : primary, filename: recCam(e) + '_' + fileStamp(e.start_ts) + '.jpg', type: 'image/jpeg' });
    const load = (url, allowFallback) => {
      lbImg.onerror = () => {
        if (lbEv !== e) return;
        const n = +(lbImg.dataset.snapRetry || 0);
        if (n < SNAP_RETRY) {
          lbImg.dataset.snapRetry = String(n + 1);
          setTimeout(() => { if (lbEv === e) { lbImg.src = lbSnapUrl(url, true); } }, SNAP_RETRY_MS * (n + 1));
          return;
        }
        if (allowFallback && fallback) {
          delete lbImg.dataset.snapRetry;
          lbLow = fallback;
          load(fallback, false);
          return;
        }
        lbImg.onerror = null; lbImg.hidden = true;
      };
      lbImg.src = url;
    };
    load(primary, true);
  }
  function lbOpen(e) {
    lbClose(); lbEv = e;
    const name = e.camera_name || detName[e.camera] || e.camera, label = cap(e.label), base = CD + '/api/events/' + encodeURIComponent(e.id), low = base + '/snapshot.jpg', high = base + '/snapshot_hires.jpg';
    lbInfo.textContent = label + ' · ' + name + ' · ' + fmt(e.start_ts);
    lbArmSnapshot(e, low, high);
    lbClip.hidden = true; lbNote.textContent = ''; lbMsg.textContent = 'Loading clip…';
    lb.hidden = false; document.body.classList.add('cd-lb-open'); lb.querySelector('.cd-lb-x').focus();
    lbTry(e);
  }
  lb.querySelector('.cd-lb-x').addEventListener('click', lbClose);
  lbHd.addEventListener('click', () => lbHdSet(!lbHiRes));
  lb.addEventListener('click', ev => { if (ev.target === lb) lbClose(); });
  lbV.addEventListener('error', () => { if (lbEv && lbV.getAttribute('src')) { lbMsg.textContent = 'Clip failed to play. Retrying…'; lbV.removeAttribute('src'); lbV.hidden = true; lbImg.hidden = !lbImg.getAttribute('src'); clearTimeout(lbTimer); const e = lbEv; lbTimer = setTimeout(() => lbTry(e), CLIP_RETRY); } });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !lb.hidden) lbClose(); });

  // ---------- detections list (shared by the tab and the camera page) ----------
  // Detection type icon (24x24 line art in the accent colour); anything else gets a target.
  const KIND = {
    person: [['circle', { cx: 12, cy: 7, r: 3.2 }], ['path', { d: 'M5.5 20.5v-1.5a6.5 6.5 0 0 1 13 0v1.5' }]],
    car: [['path', { d: 'M3 16.5v-4l2-5.5h14l2 5.5v4z' }], ['path', { d: 'M3 12.5h18' }],
      ['circle', { cx: 7.5, cy: 17.5, r: 1.8 }], ['circle', { cx: 16.5, cy: 17.5, r: 1.8 }]],
    dog: [['path', { d: 'M8 8.5a4 4 0 0 1 8 0v5.5a4 4 0 0 1-8 0z' }],
      ['path', { d: 'M8.2 7.2C5.6 5.9 3.2 7 3.4 10.6c.1 2.3 1.2 4 2.6 3.7 1-.2 1.6-1.2 2-2.4' }],
      ['path', { d: 'M15.8 7.2c2.6-1.3 5-.2 4.8 3.4-.1 2.3-1.2 4-2.6 3.7-1-.2-1.6-1.2-2-2.4' }],
      ['circle', { cx: 10.4, cy: 10.6, r: 0.7, fill: 'currentColor' }], ['circle', { cx: 13.6, cy: 10.6, r: 0.7, fill: 'currentColor' }],
      ['path', { d: 'M10.8 13.6h2.4L12 15z', fill: 'currentColor' }]],
    cat: [['path', { d: 'M5 4.5l3.5 3.5h7L19 4.5v9a7 7 0 0 1-14 0z' }],
      ['circle', { cx: 9.5, cy: 12.5, r: 0.75, fill: 'currentColor' }], ['circle', { cx: 14.5, cy: 12.5, r: 0.75, fill: 'currentColor' }],
      ['path', { d: 'M11 15.3h2l-1 1.2z', fill: 'currentColor' }], ['path', { d: 'M2.5 14.3l4 .7M2.5 17.5l4-.7M21.5 14.3l-4 .7M21.5 17.5l-4-.7' }]],
    other: [['circle', { cx: 12, cy: 12, r: 8 }], ['circle', { cx: 12, cy: 12, r: 2, fill: 'currentColor' }]],
  };
  function kindIcon(label) {
    const NS = 'http://www.w3.org/2000/svg', key = KIND[label] ? label : 'other', wrap = document.createElement('span');
    wrap.className = 'cd-kind cd-kind-' + key; wrap.title = cap(label || 'detection'); wrap.setAttribute('aria-hidden', 'true');
    const svg = document.createElementNS(NS, 'svg');
    [['viewBox', '0 0 24 24'], ['fill', 'none'], ['stroke', 'currentColor'], ['stroke-width', '1.7'], ['stroke-linecap', 'round'], ['stroke-linejoin', 'round']].forEach(([k, v]) => svg.setAttribute(k, v));
    KIND[key].forEach(([tag, attrs]) => { const n = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); svg.appendChild(n); });
    wrap.appendChild(svg); return wrap;
  }
  function evRow(e) {
    const it = document.createElement('div'); it.className = 'cd-row';
    const name = e.camera_name || detName[e.camera] || e.camera, label = cap(e.label);
    it.classList.add('cd-row-click'); it.tabIndex = 0; it.setAttribute('role', 'button'); it.setAttribute('aria-label', 'Play clip: ' + label + ' at ' + name);
    it.addEventListener('click', () => lbOpen(e)); it.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); lbOpen(e); } });
    if (e.has_snapshot) {
      const b = document.createElement('div'); b.className = 'cd-row-thumb';
      const im = document.createElement('img'); im.loading = 'lazy'; im.alt = label + ' at ' + name;
      // Zoomed in on what was detected; the full snapshot if cams-detect has no thumbnail route yet.
      const evBase = CD + '/api/events/' + encodeURIComponent(e.id);
      im.addEventListener('error', () => { if (!im.dataset.full) { im.dataset.full = '1'; im.src = evBase + '/snapshot.jpg'; } });
      im.src = evBase + '/thumb.jpg';
      const pl = document.createElement('span'); pl.className = 'cd-play'; pl.textContent = '\u25b6';
      b.append(im, pl); it.appendChild(b);
    } else { const ph = document.createElement('div'); ph.className = 'cd-row-thumb cd-row-none'; ph.textContent = 'No snapshot'; it.appendChild(ph); }
    const m = document.createElement('div'); m.className = 'cd-row-meta';
    const l = document.createElement('b'); l.textContent = label + ' ' + Math.round((e.score || 0) * 100) + '%';
    const c = document.createElement('span'); c.textContent = name;
    const z = document.createElement('span'); z.className = 'muted sm'; z.textContent = (e.zones && e.zones.length) ? 'Zones: ' + e.zones.join(', ') : 'No zone';
    const t = document.createElement('span'); t.className = 'muted sm'; t.textContent = fmt(e.start_ts);
    m.append(l, c, z, t); it.appendChild(m); it.appendChild(kindIcon(String(e.label || '').toLowerCase())); return it;
  }
  // A list bound to a container: newest page is polled; "Load more" pages back with ?before=.
  function EvList(box, more, getQuery) {
    let items = [], key = null, exhausted = false, busy = false;
    const q = extra => { const p = new URLSearchParams(getQuery()); p.set('limit', PAGE); Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v)); return CD + '/api/events?' + p; };
    const draw = () => {
      if (!items.length) { box.innerHTML = '<p class="muted sm">No detections yet.</p>'; more.hidden = true; return; }
      box.replaceChildren(...items.map(evRow)); more.hidden = exhausted;
    };
    const fail = () => { if (key === null) { box.innerHTML = '<p class="muted sm">Detections unavailable right now.</p>'; more.hidden = true; } };
    function poll() {
      return fetch(q(), { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(evs => {
        if (!Array.isArray(evs)) return fail();
        const k = evs.map(e => e.id + ':' + e.has_snapshot + ':' + e.has_snapshot_hires + ':' + e.score + ':' + e.end_ts).join(',');
        if (k === key) return; key = k;
        const ids = new Set(evs.map(e => e.id)), older = items.filter(e => !ids.has(e.id) && evs.length && e.start_ts < evs[evs.length - 1].start_ts);
        if (items.length <= PAGE) exhausted = evs.length < PAGE;
        items = evs.concat(older); draw();
      }).catch(fail);
    }
    more.addEventListener('click', () => {
      if (busy || !items.length) return; busy = true; more.disabled = true;
      fetch(q({ before: items[items.length - 1].start_ts }), { credentials: 'same-origin' }).then(r => r.ok ? r.json() : []).then(evs => {
        const have = new Set(items.map(e => e.id)); items = items.concat((evs || []).filter(e => !have.has(e.id)));
        exhausted = !evs || evs.length < PAGE; draw();
      }).catch(() => {}).then(() => { busy = false; more.disabled = false; });
    });
    return { poll, reset() { items = []; key = null; exhausted = false; box.innerHTML = '<p class="muted sm">Loading…</p>'; more.hidden = true; return poll(); } };
  }
  const fCam = $('det-cam'), fLabel = $('det-label');
  function fillCamFilter() {
    Object.entries(detName).forEach(([id, n]) => { const o = document.createElement('option'); o.value = id; o.textContent = n; fCam.appendChild(o); });
  }
  const allList = EvList($('det-list'), $('det-more'), () => { const p = {}; if (fCam.value) p.camera = fCam.value; if (fLabel.value) p.label = fLabel.value; return p; });
  fCam.addEventListener('change', () => allList.reset());
  fLabel.addEventListener('change', () => allList.reset());
  let camDet = null;
  const camList = EvList($('cam-list'), $('cam-more'), () => ({ camera: camDet || '' }));
  let evTimer = null;
  function evStart(list) { evStop(); evTimer = setInterval(() => { if (visible()) list.poll(); }, EV_POLL); }
  function evStop() { clearInterval(evTimer); evTimer = null; }

  // ---------- live player (camera page) ----------
  const v = $('cam-video'), msg = $('cam-msg'), wrap = $('cam-player');
  function liveStop() { v.onerror = null; v.onplaying = null; v.pause(); v.removeAttribute('src'); v.load(); }
  function liveStart(id) {
    releasePlayback(); liveStop(); mode = 'live'; v.muted = true; v.autoplay = true; updatePill();
    const base = '/cams/api/stream/' + encodeURIComponent(id) + '/';
    msg.textContent = 'Connecting…';
    const ua = navigator.userAgent, desk = /(Chrome|Firefox|Edg)\//.test(ua) && !/iPhone|iPad|iPod/.test(ua);
    const srcs = (!desk && v.canPlayType('application/vnd.apple.mpegurl')) ? [base + 'index.m3u8', base + 'index.m3u8?retry=1', base + 'live.mp4'] : [base + 'live.mp4', base + 'index.m3u8'];
    let k = 0; v.src = srcs[0];
    v.onplaying = () => { if (mode === 'live') msg.textContent = 'Live'; };
    v.onerror = () => { if (mode === 'live' && route.view === 'cam' && ++k < srcs.length) { v.src = srcs[k]; const q = v.play(); if (q && q.catch) q.catch(() => {}); } else if (mode === 'live' && route.view === 'cam') msg.textContent = 'Live stream unavailable. Try again.'; };
    const p = v.play(); if (p && p.catch) p.catch(() => {});
  }
  $('cam-fs').addEventListener('click', () => {
    const el = wrap;
    if (document.fullscreenElement || document.webkitFullscreenElement) { (document.exitFullscreen || document.webkitExitFullscreen).call(document); return; }
    if (el.requestFullscreen) el.requestFullscreen().catch(() => { if (v.webkitEnterFullscreen) v.webkitEnterFullscreen(); });
    else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen(); // iPhone Safari: video-only native fullscreen
  });
  $('cam-back').addEventListener('click', () => { if (history.length > 1 && sessionStorage.getItem('cams-in-app') === '1') history.back(); else location.hash = ''; });

  // One media element; each entry maps concatenated media time back to wall time.
  const bar = $('tl-bar'), ctx = bar.getContext('2d');
  const levels = [86400,43200,21600,7200,3600,1800,600,300];
  const clamp = (n,a,b) => Math.max(a,Math.min(b,n));
  const localDate = d => [d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-');
  const dayBounds = t => { const d=new Date(t*1000);d.setHours(0,0,0,0);const a=+d/1000;d.setDate(d.getDate()+1);return [a,+d/1000]; };
  const get = async url => { const r=await fetch(url,{credentials:'same-origin'});if(!r.ok)throw new Error('Recording unavailable ('+r.status+')');return r.json(); };
  const tlSave=saveJob($('tl-download'),$('tl-export-msg'));
  let tlCam=null,generation=0,hls=null,data=null,mode='live',center=Date.now()/1000,zoom=3;
  let spans={},events=[],selected=false,rangeS=0,rangeE=0,exportTimer=null,exportSeq=0,pollTimer=null,followTimer=null,raf=0,loadTimer=null;
  let seekSequence=0,watchdog=0,gesture=null,windowKey='',playDay='',soundWanted=false,loadedCenter=0;
  let pinned=[],jumpSeq=0,jumpHold=null,lastJumped=null,jumpPrior=null,toastTimer=0;
  const cache=new Map(),playCache=new Map(),pointers=new Map();
  const span=()=>levels[zoom], left=()=>center-span()/2;
  const query=(start,end,extra={})=>new URLSearchParams({cam:tlCam,start:Math.floor(start),end:Math.ceil(end),...extra});
  const icons={back:'M10 8H4V2 M4 8a8 8 0 1 1-1 8 M9 12v6m3-6h3v6h-3z',forward:'M14 8h6V2 M20 8a8 8 0 1 0 1 8 M9 12v6m3-6h3v6h-3z',play:'m9 5 11 7-11 7z',pause:'M8 5v14M16 5v14','det-prev':'m8 6-6 6 6 6M21 12a5 5 0 1 1-10 0 5 5 0 1 1 10 0M16 12h.01','det-next':'m16 6 6 6-6 6M13 12a5 5 0 1 1-10 0 5 5 0 1 1 10 0M8 12h.01',out:'M5 12h14',in:'M5 12h14M12 5v14',calendar:'M4 6h16v15H4zM4 10h16M8 3v5M16 3v5',select:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',cancel:'m6 6 12 12M18 6 6 18'};
  function icon(id,name=id){const b=$('tl-'+id),old=b.querySelector('svg');if(old)old.remove();b.insertAdjacentHTML('afterbegin','<svg viewBox="0 0 24 24" aria-hidden="true"><path d="'+icons[name]+'"/></svg>');}
  Object.keys(icons).filter(k=>k!=='pause').forEach(k=>icon(k));
  function updatePill(){const live=mode==='live'&&!gesture;$('cam-mode').textContent=live?'LIVE':fmt(center);$('cam-mode').classList.toggle('live',live);$('cam-live').hidden=mode==='live';if(mode==='live')$('cam-unmute').hidden=true;icon('play',v.paused?'play':'pause');}
  function drawTimeline(){if(!raf)raf=requestAnimationFrame(paint);}
  function paint(){raf=0;if(!tlCam)return;const w=bar.clientWidth,dpr=devicePixelRatio||1;if(!w)return;bar.width=Math.round(w*dpr);bar.height=60*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);const x=t=>(t-left())/span()*w;
    const style=getComputedStyle(bar),token=n=>style.getPropertyValue(n).trim();ctx.clearRect(0,0,w,60);ctx.fillStyle=style.backgroundColor;ctx.beginPath();ctx.roundRect(0,0,w,44,10);ctx.fill();
    ctx.fillStyle=token('--tl-sub');(spans.sub||[]).forEach(([a,b])=>ctx.fillRect(x(a),10,x(b)-x(a),24));
    ctx.fillStyle=token('--tl-main');(spans.main||[]).forEach(([a,b])=>ctx.fillRect(x(a),32,x(b)-x(a),2));
    ctx.fillStyle=token('--mn-accent');events.forEach(e=>{ctx.beginPath();ctx.arc(x(e.start_ts),22,3,0,Math.PI*2);ctx.fill();});
    if(selected){ctx.globalAlpha=.24;ctx.fillRect(x(rangeS),0,x(rangeE)-x(rangeS),44);ctx.globalAlpha=1;for(const t of [rangeS,rangeE]){ctx.fillRect(x(t)-3,2,6,40);}}
    ctx.fillStyle=token('--tl-now');ctx.fillRect(x(Date.now()/1000),2,1,40);
    ctx.fillStyle=token('--tl-head');ctx.fillRect(w/2-1,0,2,44);ctx.beginPath();ctx.moveTo(w/2-4,0);ctx.lineTo(w/2+4,0);ctx.lineTo(w/2,5);ctx.fill();
    const steps=[60,300,600,900,1800,3600,7200,10800,21600],step=steps.find(s=>s/span()*w>=56)||21600;
    ctx.fillStyle=token('--tl-text');ctx.font='10px system-ui';ctx.textAlign='center';
    for(let t=Math.ceil(left()/step)*step;t<left()+span();t+=step){const d=new Date(t*1000),label=d.toLocaleTimeString([],{hour:'numeric',...(d.getMinutes()?{minute:'2-digit'}:{}),hour12:true}).replace(':00','');if(x(t)>24&&x(t)<w-24)ctx.fillText(label,x(t),57);}
    updatePill();
  }
  function releasePlayback(){seekSequence++;if(hls)hls.destroy();hls=null;data=null;playDay='';v.onerror=null;v.onplaying=null;v.pause();v.removeAttribute('src');v.load();v.disableRemotePlayback=false;$('cam-retry').hidden=true;}
  function timelineStop(){generation++;tlCam=null;saveRelease(tlSave);releasePlayback();clearTimeout(exportTimer);clearTimeout(loadTimer);clearInterval(pollTimer);clearInterval(followTimer);cancelAnimationFrame(raf);raf=0;exportSeq++;pointers.clear();gesture=null;cache.clear();playCache.clear();}
  async function refreshTimeline(force=false){if(!tlCam||!visible())return;const g=generation,a=Math.floor((center-span()*1.5)/3600)*3600,b=Math.ceil((center+span()*1.5)/3600)*3600,key=localDate(new Date(center*1000))+':'+a+':'+b;windowKey=key;
    try{let j=cache.get(key);if(!j||force){const timeline=await get(CR+'/api/timeline?'+query(a,b));await cdReady;if(g!==generation)return;const evs=detOf[tlCam]?await get(CD+'/api/events?'+new URLSearchParams({camera:detOf[tlCam],since:a,until:b,limit:500})).catch(()=>[]):[];j={spans:timeline.spans,events:evs};cache.set(key,j);if(cache.size>30)cache.delete(cache.keys().next().value);}if(g!==generation||key!==windowKey)return;loadedCenter=center;spans=j.spans||{};events=withPinned(Array.isArray(j.events)?j.events:[]);drawTimeline();}catch(e){if(g===generation)$('tl-msg').textContent=e.message;}}
  // Events reached by a jump stay marked even when the cached window has not caught up with them.
  function withPinned(list){const have=new Set(list.map(e=>e.id));return list.concat(pinned.filter(e=>!have.has(e.id))).sort((a,b)=>a.start_ts-b.start_ts);}
  function moved(){center=Math.min(center,Date.now()/1000);drawTimeline();clearTimeout(loadTimer);loadTimer=setTimeout(()=>refreshTimeline(),120);}
  function timelineStart(id){timelineStop();tlCam=id;mode='live';center=Date.now()/1000;zoom=3;spans={};events=[];pinned=[];jumpHold=null;lastJumped=null;selected=false;updateDetButtons();cdReady.then(updateDetButtons);$('tl-range').hidden=true;$('tl-select').setAttribute('aria-pressed','false');$('tl-date').max=localDate(new Date());$('tl-msg').textContent='';$('cam-unmute').hidden=true;refreshTimeline();drawTimeline();pollTimer=setInterval(()=>{if(visible()&&Math.abs(Date.now()/1000-center)<=span()/2){playCache.clear();refreshTimeline(true);}},60000);followTimer=setInterval(()=>{if(tlCam&&visible()&&mode==='live'&&!gesture){center=Date.now()/1000;drawTimeline();}},1000);}
  function enterPlayback(){if(mode==='live')liveStop();mode='playback';msg.textContent='';v.autoplay=false;}
  // Must run inside the tap: iOS only keeps sound for an element that was played during a user gesture.
  function primeSound(){soundWanted=true;v.muted=false;const p=v.play();if(p&&p.catch)p.catch(()=>{});}
  async function playSound(id){v.muted=!soundWanted;try{await v.play();$('cam-unmute').hidden=!v.muted;}catch(e){if(id!==seekSequence||mode!=='playback'||e.name==='AbortError')return;v.muted=true;$('cam-unmute').hidden=false;v.play().catch(()=>{$('tl-msg').textContent='Tap Play to start.';});}}
  function loadFailed(){clearTimeout(watchdog);watchdog=0;$('cam-retry').hidden=false;}
  function armWatchdog(id){clearTimeout(watchdog);$('cam-retry').hidden=true;watchdog=setTimeout(()=>{watchdog=0;if(id===seekSequence&&mode==='playback'&&!gesture&&visible()&&v.readyState<3)loadFailed();},STALL_MS);}
  function attachPlayback(url,media,id){
    if(window.Hls&&Hls.isSupported()){
      v.disableRemotePlayback=true; // ManagedMediaSource on iOS 17+ refuses to attach without it.
      const h=hls=new Hls({enableWorker:false,lowLatencyMode:false,maxBufferLength:30,backBufferLength:60,startPosition:media});
      h.on(Hls.Events.MANIFEST_PARSED,()=>{if(h===hls)playSound(seekSequence);});
      h.on(Hls.Events.ERROR,(_,d)=>{console.warn('hls.js error',d.type,d.details,d.fatal?'(fatal)':'');if(d.fatal&&h===hls)loadFailed();});
      h.loadSource(url);h.attachMedia(v);
    }else{
      v.onerror=()=>{if(mode==='playback')loadFailed();};
      v.addEventListener('loadedmetadata',()=>{if(id!==seekSequence)return;v.currentTime=media;playSound(id);},{once:true});
      v.addEventListener('canplay',()=>{if(id===seekSequence&&Math.abs(v.currentTime-media)>2)v.currentTime=media;},{once:true});
      v.src=url;playSound(id); // iOS may not fetch metadata until play() is called.
    }
  }
  async function seekWall(t){const g=generation,id=++seekSequence,stale=()=>g!==generation||id!==seekSequence;enterPlayback();primeSound();armWatchdog(id);center=Math.min(t,Date.now()/1000);moved();updatePill();const [a,b]=dayBounds(center),key=localDate(new Date(center*1000));
    try{const reuse=data&&playDay===key&&v.readyState>0;let j=data; // Until metadata arrives, a new seek can only be honoured by starting over.
      if(!reuse){j=playCache.get(key);if(!j){j=await get(CR+'/api/playback?'+query(a,b));if(stale())return;playCache.set(key,j);}if(!j.segments.length)throw new Error('No footage for this day');}
      const e=j.segments.find(e=>e.end>center)||j.segments[j.segments.length-1],media=e.offset+clamp(center-e.start,0,e.duration-.01);center=e.start+media-e.offset;
      if(reuse){v.currentTime=media;playSound(id);}else{releasePlayback();seekSequence=id;data=j;playDay=key;attachPlayback(new URL(j.playlist,location.origin+CR+'/api/').href,media,id);}
      $('tl-msg').textContent='';drawTimeline();
    }catch(e){if(!stale()){clearTimeout(watchdog);watchdog=0;$('tl-msg').textContent=e.message;}}}
  v.addEventListener('timeupdate',()=>{if(mode!=='playback'||!data||gesture||v.seeking||v.readyState<2)return;const e=data.segments.find(e=>v.currentTime>=e.offset&&v.currentTime<e.offset+e.duration);if(e){const previous=localDate(new Date(center*1000));center=e.start+v.currentTime-e.offset;drawTimeline();if(previous!==localDate(new Date(center*1000))||Math.abs(center-loadedCenter)>span()/2)refreshTimeline();}});
  v.addEventListener('seeking',()=>{if(mode==='playback'&&data&&!watchdog)armWatchdog(seekSequence);}); // Native scrubber seeks.
  v.addEventListener('playing',()=>{clearTimeout(watchdog);watchdog=0;$('cam-retry').hidden=true;});
  v.addEventListener('play',()=>drawTimeline());v.addEventListener('pause',()=>drawTimeline());
  v.addEventListener('ended',()=>{if(mode==='playback'){const next=data&&data.segments[data.segments.length-1];if(next&&localDate(new Date((next.end+1)*1000))!==playDay)seekWall(next.end+1);}});
  $('cam-live').addEventListener('click',()=>{center=Date.now()/1000;liveStart(tlCam);moved();});
  $('cam-retry').addEventListener('click',()=>{playCache.clear();releasePlayback();seekWall(center);});
  $('cam-unmute').addEventListener('click',()=>{soundWanted=true;v.muted=false;v.play().then(()=>{$('cam-unmute').hidden=true;}).catch(()=>{});});
  function zoomAt(delta){zoom=clamp(zoom+delta,0,levels.length-1);moved();}
  $('tl-in').addEventListener('click',()=>zoomAt(1));$('tl-out').addEventListener('click',()=>zoomAt(-1));
  const xTime=x=>left()+(x-bar.getBoundingClientRect().left)/bar.clientWidth*span();
  bar.addEventListener('wheel',e=>{if(e.ctrlKey||e.metaKey){e.preventDefault();zoomAt(e.deltaY<0?1:-1);}else if(Math.abs(e.deltaX)>Math.abs(e.deltaY)){e.preventDefault();enterPlayback();v.pause();center+=e.deltaX/bar.clientWidth*span();moved();clearTimeout(loadTimer);loadTimer=setTimeout(()=>seekWall(center),220);}},{passive:false});
  bar.addEventListener('pointerdown',e=>{bar.setPointerCapture(e.pointerId);pointers.set(e.pointerId,e.clientX);const t=xTime(e.clientX),hit=span()*22/bar.clientWidth;if(pointers.size===1)gesture={x:e.clientX,center,moved:false,kind:selected&&Math.abs(t-rangeS)<hit?'start':selected&&Math.abs(t-rangeE)<hit?'end':'pan'};else{gesture.pinch=Math.abs([...pointers.values()][0]-[...pointers.values()][1]);gesture.multi=true;}v.pause();});
  bar.addEventListener('pointermove',e=>{if(!pointers.has(e.pointerId)||!gesture)return;pointers.set(e.pointerId,e.clientX);if(pointers.size===2){const p=[...pointers.values()],dist=Math.abs(p[0]-p[1]);if(Math.abs(dist-gesture.pinch)>30){zoomAt(dist>gesture.pinch?1:-1);gesture.pinch=dist;}return;}if(gesture.multi)return;const dx=e.clientX-gesture.x;if(Math.abs(dx)>5)gesture.moved=true;if(!gesture.moved)return;if(gesture.kind==='start'){rangeS=clamp(xTime(e.clientX),rangeE-1800,rangeE-1);syncRange();}else if(gesture.kind==='end'){rangeE=clamp(xTime(e.clientX),rangeS+1,rangeS+1800);syncRange();}else{center=gesture.center-dx/bar.clientWidth*span();moved();}});
  bar.addEventListener('pointerup',e=>{pointers.delete(e.pointerId);if(!gesture||pointers.size)return;const g=gesture;gesture=null;if(g.multi){drawTimeline();return;}if(g.kind!=='pan'&&g.moved)return;let t=g.moved?center:xTime(e.clientX);const hit=span()*10/bar.clientWidth,ev=events.reduce((best,e)=>Math.abs(e.start_ts-t)<hit&&(!best||Math.abs(e.start_ts-t)<Math.abs(best.start_ts-t))?e:best,null);seekWall(!g.moved&&ev?ev.start_ts-5:t);});
  bar.addEventListener('pointercancel',()=>{pointers.clear();gesture=null;drawTimeline();});
  bar.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();seekWall(center+(e.key==='ArrowLeft'?-10:10));}else if(e.key==='+'||e.key==='-'){e.preventDefault();zoomAt(e.key==='+'?1:-1);}});
  $('tl-back').addEventListener('click',()=>seekWall(center-10));$('tl-forward').addEventListener('click',()=>seekWall(center+10));
  $('tl-play').addEventListener('click',()=>{if(mode==='live'||!data)seekWall(center);else if(v.paused)playSound(seekSequence);else v.pause();});
  // ---------- previous/next detection, across the camera's whole history ----------
  const JUMP_PAGE=200;
  function updateDetButtons(){const off=String(!detOf[tlCam]);$('tl-det-prev').setAttribute('aria-disabled',off);$('tl-det-next').setAttribute('aria-disabled',off);}
  function toast(text){const el=$('cam-toast');el.textContent=text;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{el.hidden=true;},2000);}
  const eventsPage=params=>get(CD+'/api/events?'+new URLSearchParams({camera:detOf[tlCam],...params}));
  // A jump lands 5 s ahead of its event: while that event is still playing, previous must search from before it.
  function previousBefore(t){const j=lastJumped,played=j?t-(j.start_ts-5):-1;return played>=0&&played<=30?Math.min(t-1,j.start_ts-0.5):t-6;}
  async function findDetection(dir,t,stale){
    if(dir<0)return (await eventsPage({before:previousBefore(t),limit:1}))[0]||null;
    // Results are newest first, so a full page may stop short of the nearest event: walk back until a short page.
    let best=null,until={};
    for(;;){
      const evs=await eventsPage({since:t+6,limit:JUMP_PAGE,...until});
      if(stale())return null;
      const low=evs.filter(e=>e.start_ts>t+5).reduce((m,e)=>!m||e.start_ts<m.start_ts?e:m,null),closer=low&&(!best||low.start_ts<best.start_ts);
      if(closer)best=low;
      if(evs.length<JUMP_PAGE||!closer)return best;
      until={until:best.start_ts};
    }
  }
  // A playlist fetched earlier today ends before anything recorded since; seekWall would clamp to its end.
  function dropStalePlaylist(target){const key=localDate(new Date(target*1000)),list=(playDay===key&&data)||playCache.get(key),last=list&&list.segments[list.segments.length-1];if(last&&target>=last.end){playCache.delete(key);if(playDay===key)data=null;}}
  async function jumpDetection(dir){
    if(!detOf[tlCam])return;
    const none=dir<0?'No earlier detection':'No later detection',live=mode==='live';
    if(live&&dir>0)return toast(none);
    // After landing past an event (it sits in a footage gap), search from the event so the jump can move on from it.
    const t=live?Date.now()/1000:jumpHold&&Math.abs(center-jumpHold.landed)<30?jumpHold.want:center;
    const g=generation,seen=seekSequence,seq=++jumpSeq,stale=()=>seq!==jumpSeq||g!==generation||seen!==seekSequence||!!gesture;
    // The lookup outlives the tap, so sound must be primed now; undone if the jump ends up going nowhere.
    if(!jumpPrior)jumpPrior={muted:v.muted,wanted:soundWanted,paused:v.paused};
    primeSound();
    const settle=keep=>{const was=jumpPrior;jumpPrior=null;if(!keep){v.muted=was.muted;soundWanted=was.wanted;if(was.paused)v.pause();}};
    let e;
    try{e=await findDetection(dir,t,stale);}catch(err){if(seq===jumpSeq){if(stale())jumpPrior=null;else{settle(false);toast('Detections unavailable');}}return;}
    if(stale()){if(seq===jumpSeq)jumpPrior=null;return;}
    if(!e){settle(false);toast(none);return;}
    settle(true);
    const want=e.start_ts-5;
    lastJumped={id:e.id,start_ts:e.start_ts};
    pinned.push(e);events=withPinned(events);dropStalePlaylist(want);
    await seekWall(want);
    if(seq===jumpSeq)jumpHold=Math.abs(center-want)>1?{want,landed:center}:null;
  }
  $('tl-det-prev').addEventListener('click',()=>jumpDetection(-1));$('tl-det-next').addEventListener('click',()=>jumpDetection(1));
  $('tl-calendar').addEventListener('click',()=>{const d=$('tl-date');d.value=localDate(new Date(center*1000));try{d.showPicker();}catch(e){d.focus();d.click();}});
  $('tl-date').addEventListener('change',async()=>{const t=+new Date($('tl-date').value+'T12:00:00')/1000;if(!Number.isFinite(t))return;const g=generation,[a,b]=dayBounds(t);try{const j=await get(CR+'/api/timeline?'+query(a,b));if(g!==generation)return;seekWall(j.spans.sub.length?j.spans.sub[0][0]:t);}catch(e){$('tl-msg').textContent=e.message;}});
  const timeInput=t=>new Date(t*1000).toTimeString().slice(0,8);
  function syncRange(changed){rangeS=Math.round(rangeS);rangeE=Math.round(rangeE);if(changed==='start')rangeS=clamp(rangeS,rangeE-1800,rangeE-1);else rangeE=clamp(rangeE,rangeS+1,rangeS+1800);$('tl-start').value=timeInput(rangeS);$('tl-end').value=timeInput(rangeE);$('tl-start').title=fmt(rangeS);$('tl-end').title=fmt(rangeE);const duration=rangeE-rangeS;$('tl-duration').textContent=Math.floor(duration/60)+'m '+duration%60+'s';drawTimeline();clearTimeout(exportTimer);const seq=++exportSeq;saveRelease(tlSave);$('tl-download').hidden=true;$('tl-export-msg').textContent='Estimating…';exportTimer=setTimeout(async()=>{try{const j=await get(CR+'/api/export/info?'+query(rangeS,rangeE));if(seq!==exportSeq||!selected)return;const q=j.recommended;$('tl-export-msg').textContent=j.busy?'Another export is running. Try again shortly.':q==='sub'?'Full-res is no longer kept.':!q?(stOf(j,'main')==='not_ready'||stOf(j,'sub')==='not_ready'?'Footage is not ready yet.':'No footage in this range.'):'';if(q&&!j.busy){saveArm(tlSave,{url:CR+'/api/export?'+query(rangeS,rangeE,{quality:'main',fallback:1}),filename:tlCam+'_'+fileStamp(rangeS)+'_export.mp4',type:'video/mp4',keepServerName:true,size:j[q].bytes});tlSave.link.textContent='Download '+(q==='main'?'full-res':'low-res')+' · '+Math.max(1,Math.round(j[q].bytes/1e6))+' MB';}}catch(e){if(seq===exportSeq)$('tl-export-msg').textContent=e.message;}},350);}
  function rangeMode(on){selected=on;$('tl-range').hidden=!on;$('tl-select').setAttribute('aria-pressed',String(on));if(on){rangeS=center-60;rangeE=center+60;zoom=7;moved();syncRange();}else{clearTimeout(exportTimer);exportSeq++;saveRelease(tlSave);drawTimeline();}}
  $('tl-select').addEventListener('click',()=>rangeMode(!selected));$('tl-cancel').addEventListener('click',()=>rangeMode(false));
  for(const id of ['start','end'])$('tl-'+id).addEventListener('change',()=>{const current=id==='start'?rangeS:rangeE,d=new Date(current*1000),parts=$('tl-'+id).value.split(':').map(Number);if(parts.length<2)return;d.setHours(parts[0],parts[1],parts[2]||0,0);if(id==='start')rangeS=+d/1000;else rangeE=+d/1000;syncRange(id);});
  new ResizeObserver(()=>{if(tlCam)drawTimeline();}).observe(bar);
  document.addEventListener('visibilitychange',()=>{if(!visible()){v.pause();clearTimeout(loadTimer);}else if(tlCam){if(mode==='live'){center=Date.now()/1000;liveStart(tlCam);}refreshTimeline(true);drawTimeline();}});
  window.addEventListener('pagehide',timelineStop);


  // ---------- Web Push notification control ----------
  // iPhone/iPad only expose Notification + Push after the PWA is added to the Home Screen (standalone).
  // Safari in a normal browser tab cannot subscribe; the UI must say so explicitly.
  const CR_PUSH = CR + '/api/push';
  const pushBar = $('push-bar'), pushStatus = $('push-status'), pushToggle = $('push-toggle'), pushTest = $('push-test');
  const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const isIOS = IOS;
  function b64ToUint8(b64) {
    const pad = '='.repeat((4 - (b64.length % 4)) % 4), raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    const out = new Uint8Array(raw.length); for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i); return out;
  }
  function pushExplain() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      return { state: 'unsupported', text: isIOS && !isStandalone()
        ? 'On iPhone, add Cameras to your Home Screen first. Open that Home Screen app, then you can enable detection alerts. Safari in a normal tab cannot receive Web Push.'
        : 'This browser does not support Web Push notifications.' };
    }
    if (isIOS && !isStandalone()) {
      return { state: 'ios-browser', text: 'On iPhone/iPad, detection alerts require the Cameras app on your Home Screen (Share → Add to Home Screen). Open that app — not Safari in a tab — then tap Enable.' };
    }
    if (typeof Notification !== 'undefined' && Notification.permission === 'denied') {
      return { state: 'denied', text: 'Notification permission is blocked for this app. Enable it in system Settings, then return here.' };
    }
    return { state: 'ready', text: '' };
  }
  async function pushJson(url, opts) {
    const r = await fetch(url, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json', ...(opts && opts.headers) }, ...opts });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || ('Request failed (' + r.status + ')'));
    return body;
  }
  async function ensurePushWorker() {
    const reg = await navigator.serviceWorker.register('/cams/sw.js', { scope: '/cams/' });
    await navigator.serviceWorker.ready;
    return reg;
  }
  async function currentSubscription(reg) {
    return reg.pushManager.getSubscription();
  }
  async function refreshPushUi() {
    if (!pushBar) return;
    pushBar.hidden = false;
    const info = pushExplain();
    pushTest.hidden = true;
    if (info.state !== 'ready') {
      pushStatus.textContent = info.text;
      pushToggle.hidden = info.state === 'unsupported' || info.state === 'denied';
      pushToggle.textContent = 'Enable';
      pushToggle.disabled = info.state !== 'ios-browser' ? info.state !== 'ready' : true;
      if (info.state === 'ios-browser') { pushToggle.hidden = true; }
      return;
    }
    try {
      const st = await pushJson(CR_PUSH + '/status');
      if (!st.configured) {
        pushStatus.textContent = 'Detection alerts are not configured on the server yet.';
        pushToggle.hidden = true;
        return;
      }
      const reg = await ensurePushWorker();
      const sub = await currentSubscription(reg);
      const on = !!sub;
      pushStatus.textContent = on ? 'Detection alerts are on for this device.' : 'Detection alerts are off on this device.';
      pushToggle.hidden = false;
      pushToggle.disabled = false;
      pushToggle.textContent = on ? 'Disable' : 'Enable';
      pushTest.hidden = !on;
    } catch (e) {
      pushStatus.textContent = 'Could not check alert status. ' + (e.message || '');
      pushToggle.hidden = true;
    }
  }
  async function enablePush() {
    const info = pushExplain();
    if (info.state !== 'ready') { pushStatus.textContent = info.text; return; }
    pushToggle.disabled = true;
    try {
      const reg = await ensurePushWorker();
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        pushStatus.textContent = perm === 'denied'
          ? 'Notification permission is blocked for this app. Enable it in system Settings, then return here.'
          : 'Permission was not granted, so alerts stay off.';
        return;
      }
      const { publicKey } = await pushJson(CR_PUSH + '/vapid-public-key');
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(publicKey) });
      const json = sub.toJSON();
      await pushJson(CR_PUSH + '/subscribe', { method: 'POST', body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys, user_agent: navigator.userAgent }) });
      pushStatus.textContent = 'Detection alerts are on for this device.';
    } catch (e) {
      pushStatus.textContent = e.message || 'Could not enable alerts.';
    } finally {
      await refreshPushUi();
    }
  }
  async function disablePush() {
    pushToggle.disabled = true;
    try {
      const reg = await ensurePushWorker();
      const sub = await currentSubscription(reg);
      if (sub) {
        const endpoint = sub.endpoint;
        try { await sub.unsubscribe(); } catch (e) {}
        await pushJson(CR_PUSH + '/subscribe?endpoint=' + encodeURIComponent(endpoint), {
          method: 'DELETE', body: JSON.stringify({ endpoint }),
        }).catch(() => {});
      }
      pushStatus.textContent = 'Detection alerts are off on this device.';
    } catch (e) {
      pushStatus.textContent = e.message || 'Could not disable alerts.';
    } finally {
      await refreshPushUi();
    }
  }
  if (pushToggle) pushToggle.addEventListener('click', () => {
    if (pushToggle.textContent === 'Disable') disablePush(); else enablePush();
  });
  if (pushTest) pushTest.addEventListener('click', async () => {
    pushTest.disabled = true;
    try {
      await pushJson(CR_PUSH + '/test', { method: 'POST', body: '{}' });
      pushStatus.textContent = 'Test notification sent. If nothing appears, check Focus/Do Not Disturb.';
    } catch (e) {
      pushStatus.textContent = e.message || 'Test failed.';
    } finally {
      pushTest.disabled = false;
      setTimeout(refreshPushUi, 1500);
    }
  });
  navigator.serviceWorker && navigator.serviceWorker.addEventListener('message', ev => {
    const msg = ev.data || {};
    if (msg.type === 'cams-push-open' && msg.url) {
      const hash = String(msg.url).split('#')[1];
      if (hash != null) location.hash = hash;
    }
  });
  refreshPushUi();

  // ---------- deep-link: #event=<id> opens the authorized detection lightbox ----------
  // Production event ids are strings (e.g. "1793897909-7de0b7"), not integers.
  let openingEvent = null;
  async function openEventId(id) {
    const eid = decodeURIComponent(String(id || '')).trim();
    if (!eid || openingEvent === eid) return;
    openingEvent = eid;
    try {
      let ev = null;
      try {
        const ctx = await pushJson(CR_PUSH + '/event-context/' + encodeURIComponent(eid));
        // Real flags from cams-record (paths non-NULL). Never assume both snapshots exist.
        ev = {
          id: ctx.id,
          camera: ctx.camera_id || ctx.camera,
          camera_name: detName[ctx.camera_id || ctx.camera] || ctx.camera_id || ctx.camera,
          label: ctx.label || 'detection',
          score: ctx.score,
          start_ts: ctx.start_ts,
          end_ts: ctx.end_ts,
          has_snapshot: !!ctx.has_snapshot,
          has_snapshot_hires: !!ctx.has_snapshot_hires,
        };
      } catch (e) {
        const list = await fetch(CD + '/api/events?limit=50', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : []);
        ev = (list || []).find(x => String(x.id) === eid) || null;
      }
      if (!ev) { if (pushStatus) pushStatus.textContent = 'That detection is no longer available.'; return; }
      await cdReady;
      lbOpen(ev);
    } finally {
      openingEvent = null;
    }
  }

  // ---------- camera management (Admin tab; admins only, enforced by cams-record) ----------
  // Camera objects live in cams_config (docs/camera-model.md). Every write sends the revision the view
  // was loaded at; a newer change elsewhere answers 409 and nothing is overwritten.
  // Audio settings are a separate cams-audio service (see static/camera_audio.js), not cams_config.
  const ADM = CR + '/api/admin', PURPOSES = [
    ['live', 'Live view'], ['live_hd', 'Live view (HD)'], ['detect', 'Detection'],
    ['verify', 'Checking detections'], ['snapshot', 'Snapshots'],
  ];
  const STREAM_LABEL = { main: 'HD (main)', sub: 'Low-res (sub)', snap: 'Snapshot' }, STREAM_SHORT = { main: 'HD', sub: 'Low-res', snap: 'Snapshot' };
  const adm = { ok: false, revision: 0, editable: false, mode: 'file', cameras: [], current: null, streams: null, audioPanel: null };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  async function admJson(url, opts) {
    const r = await fetch(url, { credentials: 'same-origin', ...opts, headers: { 'Content-Type': 'application/json', ...(opts && opts.headers) } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(body.detail || ('Request failed (' + r.status + ')')); e.status = r.status; throw e; }
    return body;
  }
  const names = s => s.split(',').map(x => x.trim()).filter(Boolean);
  const slug = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64).replace(/-+$/, '');
  function streamGo2rtc(cam, key) {
    const s = cam && cam.streams && cam.streams[key];
    return s && s.go2rtc_name ? s.go2rtc_name : null;
  }
  // One camera object: low-res (sub) and HD (main) are properties. Never invent *-main as its own row.
  function presentCamera(cam) {
    return { ...cam, streams_view: { sub: streamGo2rtc(cam, 'sub'), hd: streamGo2rtc(cam, 'main'), snap: streamGo2rtc(cam, 'snap') } };
  }
  function defaultUse(keys) {
    const low = keys.includes('sub') ? 'sub' : (keys.includes('main') ? 'main' : null);
    const high = keys.includes('main') ? 'main' : low, out = {};
    if (low) { out.live = [low]; out.detect = [low]; out.snapshot = [keys.includes('snap') ? 'snap' : low]; }
    if (high) { out.live_hd = [high]; out.verify = [high]; }
    out.record = ['main', 'sub'].filter(k => keys.includes(k));
    return out;
  }
  function visibilityText(c) {
    if (c.visibility === 'everyone') return 'Everyone';
    const who = [...(c.users || []), ...(c.roles || []).map(r => r + ' (role)')];
    return who.length ? 'Only ' + who.join(', ') : 'Admins only';
  }
  function detectionText(c) {
    const det = c.detection;
    if (!det || !((c.use && c.use.detect) || []).length) return 'No detection';
    return det.enabled === false ? 'Detection off' : 'Detection on';
  }
  async function admCheck() {
    // The tab appears only for admins: everyone else gets 401/403 from cams-record.
    try { await admLoad(); } catch (e) { if (e.status === 401 || e.status === 403 || !e.status) return; adm.ok = true; adm.error = e.message; }
    $('tab-admin').hidden = !adm.ok;
    if (adm.ok && /^admin(=|$)/.test(location.hash.replace(/^#/, ''))) show();  // a direct #admin link waited for this
  }
  async function admLoad() {
    const b = await admJson(ADM + '/cameras');
    Object.assign(adm, { ok: true, error: null, revision: b.revision, editable: b.editable, mode: b.mode,
      cameras: (b.cameras || []).map(presentCamera), source: b.camera_source });
    return b;
  }
  async function admDetectReport() {
    try { const s = await admJson(CD + '/api/status'); return (s.service && s.service.camera_source) || null; } catch (e) { return null; }
  }
  function admBanner(detect) {
    const b = $('adm-banner'); b.textContent = ''; b.className = 'adm-banner';
    const line = (t, cls) => b.appendChild(el('p', cls, t));
    if (adm.error) { b.classList.add('err'); line('Camera settings could not be loaded: ' + adm.error); b.hidden = false; return; }
    const src = adm.source || {};
    if (adm.mode === 'file') {
      line('Read-only. Recording and detection still use their configuration files (camera_source: file), so changes here would have no effect. Set camera_source to shadow to compare, or db to apply.');
    } else if (adm.mode === 'shadow') {
      line('Shadow mode: recording and detection still use their configuration files. Changes here are compared with them, not applied.');
      const diffs = [...(src.differences || []).map(d => 'Recording: ' + d), ...((detect && detect.differences) || []).map(d => 'Detection: ' + d)];
      if (!diffs.length) line(src.differences ? 'No differences: switching to the database would change nothing.' : 'Waiting for the first comparison…', 'muted sm');
      else { line(diffs.length + (diffs.length === 1 ? ' difference:' : ' differences:'), 'adm-warn'); const ul = el('ul', 'adm-diffs'); diffs.forEach(d => ul.appendChild(el('li', null, d))); b.appendChild(ul); }
    } else {
      line('Live: changes apply to recording and detection within a few seconds.');
      if (src.registry && src.registry.error) line('Access list not published: ' + src.registry.error, 'adm-warn');
      if (detect && detect.fallback_to_files) line('Detection is running on its configuration file until the database is reachable.', 'adm-warn');
      if (detect && detect.skipped && detect.skipped.length) line('Detection skipped: ' + detect.skipped.join('; '), 'adm-warn');
    }
    if (src.error) line('Database: ' + src.error, 'adm-warn');
    b.hidden = false;
  }
  function streamChip(label, name) {
    const wrap = el('span'); wrap.appendChild(el('b', null, label)); wrap.appendChild(document.createTextNode(name || '—')); return wrap;
  }
  async function admShowList() {
    $('adm-list').textContent = ''; $('adm-list').appendChild(el('p', 'muted sm', 'Loading…'));
    let detect = null;
    try { [, detect] = await Promise.all([admLoad(), admDetectReport()]); } catch (e) { adm.error = e.message; }
    if (route.view !== 'admin') return;
    admBanner(detect);
    $('adm-add').hidden = !adm.editable || !!adm.error;
    const list = $('adm-list'); list.textContent = '';
    if (adm.error) return;
    if (!adm.cameras.length) { list.appendChild(el('p', 'muted', 'No cameras in the camera settings database yet.')); return; }
    adm.cameras.forEach(c => {
      const row = el('a', 'adm-row'); row.href = '#admin=' + encodeURIComponent(c.id);
      row.dataset.camId = c.id;
      const main = el('div', 'adm-row-main');
      main.appendChild(el('strong', null, c.name));
      main.appendChild(el('span', 'muted sm', c.id));
      const streams = el('div', 'adm-row-streams');
      streams.appendChild(streamChip('Low-res ', (c.streams_view && c.streams_view.sub) || '—'));
      streams.appendChild(streamChip('HD ', (c.streams_view && c.streams_view.hd) || '—'));
      main.appendChild(streams);
      row.appendChild(main);
      const chips = el('div', 'adm-chips');
      chips.appendChild(el('span', 'adm-chip' + (c.enabled ? ' on' : ''), c.enabled ? 'On' : 'Off'));
      chips.appendChild(el('span', 'adm-chip', visibilityText(c)));
      const rec = (c.use && c.use.record) || [];
      chips.appendChild(el('span', 'adm-chip', rec.length ? 'Records ' + rec.map(k => k === 'main' ? 'HD' : 'low-res').join(' + ') : 'Not recorded'));
      chips.appendChild(el('span', 'adm-chip', detectionText(c)));
      Object.entries(c.recording_state || {}).forEach(([q, st]) => {
        if (st && st !== 'recording') chips.appendChild(el('span', 'adm-chip warn', (q === 'main' ? 'HD' : q === 'sub' ? 'low-res' : q) + ': ' + st));
      });
      row.appendChild(chips);
      list.appendChild(row);
    });
  }
  // ---- editor ----
  const F = id => $('f-' + id);
  function streamKeys() { return ['main', 'sub', 'snap'].filter(k => F(k).value.trim()); }
  function renderUse(use) {
    const box = F('use'), keys = streamKeys(); box.textContent = '';
    PURPOSES.forEach(([p, label]) => {
      const l = el('label', 'adm-field'); l.appendChild(document.createTextNode(label));
      const s = el('select'); s.id = 'f-use-' + p; s.appendChild(new Option('Off', ''));
      keys.forEach(k => s.appendChild(new Option(STREAM_SHORT[k] + ' · ' + F(k).value.trim(), k)));
      s.value = (use[p] || [])[0] && keys.includes(use[p][0]) ? use[p][0] : '';
      l.appendChild(s); box.appendChild(l);
    });
    const rec = el('div', 'adm-field'); rec.appendChild(document.createTextNode('Recording'));
    ['main', 'sub'].forEach(k => {
      const c = el('label', 'adm-check'), i = el('input'); i.type = 'checkbox'; i.id = 'f-rec-' + k;
      i.checked = (use.record || []).includes(k) && keys.includes(k); i.disabled = !keys.includes(k) || !adm.editable;
      c.appendChild(i); c.appendChild(document.createTextNode(STREAM_LABEL[k])); rec.appendChild(c);
    });
    box.appendChild(rec);
    box.querySelectorAll('select').forEach(s => { s.disabled = !adm.editable; });
  }
  function readUse() {
    const out = {};
    PURPOSES.forEach(([p]) => { const v = $('f-use-' + p).value; if (v) out[p] = [v]; });
    const rec = ['main', 'sub'].filter(k => $('f-rec-' + k).checked && !$('f-rec-' + k).disabled);
    if (rec.length) out.record = rec;
    return out;
  }
  function whoToggle() { $('f-who').hidden = !$('f-vis-restricted').checked; }
  function admMsg(text, cls) { const m = $('adm-msg'); m.textContent = text || ''; m.className = 'adm-msg' + (cls ? ' ' + cls : ''); }
  function admMountAudio(cameraId) {
    if (adm.audioPanel) { adm.audioPanel.destroy(); adm.audioPanel = null; }
    const host = $('f-audio-panel');
    if (!host) return;
    host.textContent = '';
    if (!cameraId || typeof CamsAudio === 'undefined' || !CamsAudio.mount) {
      if (cameraId) {
        const fs = el('fieldset', 'adm-audio is-disabled');
        fs.appendChild(el('legend', null, 'Audio'));
        fs.appendChild(el('p', 'muted sm', 'Audio service not installed'));
        host.appendChild(fs);
      }
      return;
    }
    adm.audioPanel = CamsAudio.mount(host, { cameraId, editable: adm.editable, basePath: '/cams-audio' });
  }
  async function admLoadStreams() {
    if (adm.streams) return;
    try {
      const b = await admJson(ADM + '/streams'); adm.streams = b;
      const dl = F('go2rtc'); dl.textContent = '';
      b.streams.forEach(s => { const o = new Option(s.used_by.length ? s.name + ' (used by ' + s.used_by.join(', ') + ')' : s.name); o.value = s.name; dl.appendChild(o); });
      F('go2rtc-msg').textContent = b.reachable ? '' : 'go2rtc did not answer, so its stream names cannot be suggested; type them.';
    } catch (e) { F('go2rtc-msg').textContent = 'Stream names could not be loaded: ' + e.message; }
  }
  async function admShowEdit(id) {
    admMsg(''); $('adm-history').textContent = ''; $('adm-form').hidden = true;
    if (adm.audioPanel) { adm.audioPanel.destroy(); adm.audioPanel = null; }
    $('f-audio-panel').textContent = '';
    $('adm-title').textContent = id ? 'Loading…' : 'Add camera';
    let cam, history = [];
    try {
      if (id) { const b = await admJson(ADM + '/cameras/' + encodeURIComponent(id)); Object.assign(adm, { revision: b.revision, editable: b.editable, mode: b.mode }); cam = presentCamera(b.camera); history = b.history; }
      else { await admLoad(); cam = presentCamera({ id: '', name: '', enabled: true, visibility: 'restricted', users: [], roles: [], streams: {}, use: {}, detection: { enabled: true }, recording: null }); }
    } catch (e) { $('adm-title').textContent = id || 'Add camera'; admMsg(e.status === 404 ? 'This camera no longer exists.' : e.message, 'err'); return; }
    if (route.view !== 'admin-edit' || (route.id || null) !== (id || null)) return;
    adm.current = cam;
    $('adm-title').textContent = id ? cam.name : 'Add camera';
    F('id').value = cam.id; F('id').readOnly = !!id; F('id-row').hidden = !!id; $('f-id-help').hidden = !!id;
    F('name').value = cam.name; F('enabled').checked = cam.enabled;
    ['main', 'sub', 'snap'].forEach(k => { F(k).value = (cam.streams[k] && cam.streams[k].go2rtc_name) || ''; });
    renderUse(id ? cam.use : defaultUse(streamKeys()));
    $('f-vis-' + (cam.visibility === 'everyone' ? 'everyone' : 'restricted')).checked = true; whoToggle();
    F('users').value = (cam.users || []).join(', '); F('roles').value = (cam.roles || []).join(', ');
    F('detect').checked = !!cam.detection && cam.detection.enabled !== false;
    const detOn = !!cam.detection && cam.detection.enabled !== false;
    $('f-detect-status').textContent = !cam.detection ? 'Detection is not set up for this camera yet.'
      : detOn ? 'Detection is on. Masks and zones are edited below.' : 'Detection is off for this camera.';
    F('masks').href = '/cams-detect/masks?camera=' + encodeURIComponent(cam.id);
    const hasDet = !!(id && cam.detection);
    $('f-masks-row').hidden = !hasDet; $('f-masks-none').hidden = hasDet;
    F('main-days').value = (cam.recording && cam.recording.main_days) || ''; F('sub-days').value = (cam.recording && cam.recording.sub_days) || '';
    $('adm-form').querySelectorAll('input, select').forEach(i => {
      if (i.id === 'f-audio-stream') return;
      if (i.id === 'f-id' && id) return;
      if (i.id && i.id.indexOf('f-audio-') === 0) return;
      i.disabled = !adm.editable;
    });
    $('adm-save').hidden = !adm.editable; $('adm-delete').hidden = !adm.editable || !id; $('f-defaults').hidden = !adm.editable;
    if (!adm.editable) admMsg('Read-only while camera_source is file.');
    $('adm-form').hidden = false;
    $('adm-history-title').hidden = !history.length;
    history.forEach(h => {
      const li = el('li'), before = h.before || {}, after = h.after || {};
      const changed = Object.keys({ ...before, ...after }).filter(k => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
      li.appendChild(el('strong', null, new Date(h.at).toLocaleString()));
      li.appendChild(document.createTextNode(' · ' + h.actor + ' · ' + h.action + (changed.length && h.action === 'update' ? ': ' + changed.join(', ') : '')));
      $('adm-history').appendChild(li);
    });
    admLoadStreams();
    admMountAudio(id || null);
  }
  function admForm() {
    const streams = {};
    ['main', 'sub', 'snap'].forEach(k => { const v = F(k).value.trim(); if (v) streams[k] = { go2rtc_name: v }; });
    const days = v => v.trim() === '' ? null : Number(v);
    const restricted = $('f-vis-restricted').checked;
    const out = { name: F('name').value.trim(), enabled: F('enabled').checked, visibility: restricted ? 'restricted' : 'everyone',
      users: restricted ? names(F('users').value) : [], roles: restricted ? names(F('roles').value) : [],
      streams, use: readUse(), recording: { main_days: days(F('main-days').value), sub_days: days(F('sub-days').value) },
      detection_enabled: F('detect').checked };
    if (!route.id) out.id = F('id').value.trim();
    return out;
  }
  async function admSave(ev) {
    ev.preventDefault();
    if (!adm.editable) return;
    const form = admForm();
    if (!form.name) return admMsg('Give the camera a name.', 'err');
    if (!route.id && !/^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(form.id)) return admMsg('The ID may use lowercase letters, numbers and dashes, and must start and end with a letter or number.', 'err');
    if (!Object.keys(form.streams).length) return admMsg('Add at least one stream.', 'err');
    $('adm-save').disabled = true; admMsg('Saving…');
    try {
      const body = JSON.stringify({ camera: form, expect_revision: adm.revision });
      if (route.id) await admJson(ADM + '/cameras/' + encodeURIComponent(route.id), { method: 'PUT', body });
      else await admJson(ADM + '/cameras', { method: 'POST', body });
      sessionStorage.setItem('cams-adm-flash', (route.id ? 'Saved ' : 'Added ') + form.name + (adm.mode === 'db' ? '. It applies within a few seconds.' : '.'));
      location.hash = 'admin';
    } catch (e) {
      admMsg(e.status === 409 && /revision/.test(e.message) ? 'Someone else changed the cameras since this page loaded. Reload to see their change, then make yours again.' : e.message, 'err');
    } finally { $('adm-save').disabled = false; }
  }
  async function admDelete() {
    const cam = adm.current;
    if (!cam || !route.id || !window.confirm('Delete ' + cam.name + '? Recording and detection stop for it. Existing recordings are kept until retention removes them.')) return;
    try {
      await admJson(ADM + '/cameras/' + encodeURIComponent(cam.id) + '?expect_revision=' + adm.revision, { method: 'DELETE' });
      sessionStorage.setItem('cams-adm-flash', 'Deleted ' + cam.name + '.'); location.hash = 'admin';
    } catch (e) { admMsg(e.message, 'err'); }
  }
  $('adm-form').addEventListener('submit', admSave);
  $('adm-delete').addEventListener('click', admDelete);
  $('adm-cancel').addEventListener('click', () => { location.hash = 'admin'; });
  $('adm-back').addEventListener('click', () => { location.hash = 'admin'; });
  $('adm-add').addEventListener('click', () => { location.hash = 'admin=new'; });
  $('f-defaults').addEventListener('click', () => renderUse(defaultUse(streamKeys())));
  ['main', 'sub', 'snap'].forEach(k => F(k).addEventListener('change', () => renderUse(route.id ? readUse() : defaultUse(streamKeys()))));
  F('name').addEventListener('input', () => { if (!route.id && !F('id').dataset.touched) F('id').value = slug(F('name').value); });
  F('id').addEventListener('input', () => { F('id').dataset.touched = '1'; });
  $('f-vis-everyone').addEventListener('change', whoToggle); $('f-vis-restricted').addEventListener('change', whoToggle);
  function admShow() {
    if (route.view === 'admin') {
      if (adm.audioPanel) { adm.audioPanel.destroy(); adm.audioPanel = null; }
      const flash = sessionStorage.getItem('cams-adm-flash'); sessionStorage.removeItem('cams-adm-flash');
      admShowList().then(() => { if (flash && route.view === 'admin') { const b = $('adm-banner'); b.insertBefore(el('p', 'adm-ok', flash), b.firstChild); b.hidden = false; } });
      document.title = 'Cameras · Admin · MERSINO NETWORK';
    } else if (route.view === 'admin-edit') {
      F('id').dataset.touched = ''; admShowEdit(route.id);
      document.title = (route.id ? 'Edit camera' : 'Add camera') + ' · Cameras · MERSINO NETWORK';
    }
  }

  // ---------- routing ----------
  function parse() {
    const h = location.hash.replace(/^#/, '');
    if (h === 'admin') return { view: 'admin' };
    const am = h.match(/^admin=(new|[a-z0-9][a-z0-9-]{0,63})$/);
    if (am) return { view: 'admin-edit', id: am[1] === 'new' ? null : am[1] };
    const ev = h.match(/^event=([^&]+)$/);
    if (ev) return { view: 'event', eventId: ev[1] };
    if (h === 'detections') return { view: 'det' };
    const m = h.match(/^cam=([a-z0-9][a-z0-9-]{0,31})$/);
    if (m && hubCams.some(c => c.id === m[1])) return { view: 'cam', id: m[1] };
    return { view: 'grid' };
  }
  function show() {
    const prev = route; route = parse();
    if (route.view === 'event') {
      openEventId(route.eventId);
      // Keep grid visible underneath while the lightbox opens.
      route = { view: 'grid' };
    }
    if (prev.view === 'cam' && (route.view !== 'cam' || route.id !== prev.id)) { liveStop(); timelineStop(); }
    if ((route.view === 'admin' || route.view === 'admin-edit') && !adm.ok) route = { view: 'grid' };  // not (yet) known to be an admin
    $('view-grid').hidden = route.view !== 'grid'; $('view-det').hidden = route.view !== 'det'; $('view-cam').hidden = route.view !== 'cam';
    $('view-admin').hidden = route.view !== 'admin'; $('view-admin-edit').hidden = route.view !== 'admin-edit';
    $('cams-tabs').hidden = route.view === 'cam' || route.view === 'admin-edit';
    const tabView = route.view === 'admin-edit' ? 'admin' : route.view;
    document.querySelectorAll('.cams-tab').forEach(a => a.setAttribute('aria-current', a.dataset.view === tabView ? 'page' : 'false'));
    admShow();
    evStop();
    if (route.view !== 'cam') sessionStorage.removeItem('cams-in-app');
    if (route.view === 'grid') { if (visible()) gridStart(); } else gridStop();
    if (route.view === 'det') { allList.reset(); evStart(allList); }
    if (route.view === 'cam') {
      const c = hubCams.find(x => x.id === route.id);
      $('cam-name').textContent = c.name; document.title = c.name + ' · Cameras · MERSINO NETWORK';
      try { window.scrollTo(0, 0); } catch (e) {}
      if (prev.view !== 'cam' || prev.id !== route.id) { timelineStart(c.id); liveStart(c.id); }
      $('cam-list').innerHTML = '<p class="muted sm">Loading…</p>'; $('cam-more').hidden = true; $('cam-off').hidden = true;
      cdReady.then(() => {
        if (route.view !== 'cam' || route.id !== c.id) return;
        camDet = detOf[c.id] || null;
        if (!camDet) { $('cam-off').hidden = false; $('cam-list').innerHTML = ''; return; }
        camList.reset(); evStart(camList);
      });
    } else if (route.view !== 'admin' && route.view !== 'admin-edit') document.title = 'Cameras · MERSINO NETWORK';
  }
  tiles.forEach(t => t.addEventListener('click', () => { sessionStorage.setItem('cams-in-app', '1'); location.hash = 'cam=' + t.dataset.id; }));
  document.querySelectorAll('.cams-tab').forEach(a => a.addEventListener('click', () => sessionStorage.setItem('cams-in-app', '1')));
  window.addEventListener('hashchange', show);
  window.addEventListener('pagehide', liveStop);
  document.addEventListener('visibilitychange', () => {
    if (visible()) { cdConnect(); if (route.view === 'grid') gridStart(); else if (route.view === 'det') allList.poll(); else if (route.view === 'cam' && camDet) camList.poll(); }
    else { cdStop(); gridStop(); }
  });
  show();
  admCheck();
})();
