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

  // ---------- detection clip player (modal) ----------
  // Plays a low-res (sub) clip from cams-record around the detection; downloads use full-res (main) when still kept.
  const CR = '/cams-record', PRE = 5, POST = 5, NOEND = 10, MAXWIN = 120, CLIP_RETRY = 15000;
  const lb = $('cd-lb'), lbV = lb.querySelector('video'), lbImg = lb.querySelector('img'), lbInfo = lb.querySelector('.cd-lb-info'),
    lbMsg = lb.querySelector('.cd-lb-msg'), lbSnap = lb.querySelector('.cd-dl-snap'), lbClip = lb.querySelector('.cd-dl-clip'), lbNote = lb.querySelector('.cd-lb-note');
  let lbEv = null, lbTimer = null;
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
    lbV.pause(); lbV.removeAttribute('src'); lbV.load(); lbV.hidden = true; lbImg.removeAttribute('src');
    document.body.classList.remove('cd-lb-open');
  }
  function lbTry(e) {
    if (lbEv !== e) return;
    fetch(infoQ(e), { credentials: 'same-origin' }).then(r => r.ok ? r.json() : Promise.reject(r.status)).then(j => {
      if (lbEv !== e) return;
      const sub = stOf(j, 'sub'), main = stOf(j, 'main');
      if (main === 'available') { lbClip.href = clipQ(e, 'main', 1); lbClip.hidden = false; lbNote.textContent = ''; }
      else if (sub === 'available') { lbClip.href = clipQ(e, 'sub', 1); lbClip.hidden = false; lbNote.textContent = main === 'not_ready' ? '' : 'Full-res footage is no longer kept; the clip download is low-res.'; }
      const play = q => { lbMsg.textContent = q === 'main' ? 'Low-res copy unavailable; playing full-res.' : ''; lbImg.hidden = true; lbV.hidden = false; lbV.src = clipQ(e, q, 0); const p = lbV.play(); if (p && p.catch) p.catch(() => {}); };
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
  function lbOpen(e) {
    lbClose(); lbEv = e;
    const name = e.camera_name || detName[e.camera] || e.camera, label = cap(e.label), snap = CD + '/api/events/' + encodeURIComponent(e.id) + '/snapshot' + (e.has_snapshot_hires ? '_hires' : '') + '.jpg';
    lbInfo.textContent = label + ' · ' + name + ' · ' + fmt(e.start_ts);
    if (e.has_snapshot || e.has_snapshot_hires) { lbImg.src = snap; lbImg.alt = label + ' at ' + name; lbImg.hidden = false; lbSnap.href = snap; lbSnap.hidden = false; }
    else { lbImg.hidden = true; lbSnap.hidden = true; }
    lbSnap.setAttribute('download', recCam(e) + '_' + e.id + '.jpg');
    lbClip.hidden = true; lbNote.textContent = ''; lbMsg.textContent = 'Loading clip…';
    lb.hidden = false; document.body.classList.add('cd-lb-open'); lb.querySelector('.cd-lb-x').focus();
    lbTry(e);
  }
  lb.querySelector('.cd-lb-x').addEventListener('click', lbClose);
  lb.addEventListener('click', ev => { if (ev.target === lb) lbClose(); });
  lbV.addEventListener('error', () => { if (lbEv && !lbV.hidden && lbV.getAttribute('src')) { lbMsg.textContent = 'Clip failed to play. Retrying…'; lbV.hidden = true; lbImg.hidden = !lbImg.getAttribute('src'); clearTimeout(lbTimer); const e = lbEv; lbTimer = setTimeout(() => lbTry(e), CLIP_RETRY); } });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !lb.hidden) lbClose(); });

  // ---------- detections list (shared by the tab and the camera page) ----------
  function evRow(e) {
    const it = document.createElement('div'); it.className = 'cd-row';
    const name = e.camera_name || detName[e.camera] || e.camera, label = cap(e.label);
    it.classList.add('cd-row-click'); it.tabIndex = 0; it.setAttribute('role', 'button'); it.setAttribute('aria-label', 'Play clip: ' + label + ' at ' + name);
    it.addEventListener('click', () => lbOpen(e)); it.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); lbOpen(e); } });
    if (e.has_snapshot) {
      const b = document.createElement('div'); b.className = 'cd-row-thumb';
      const im = document.createElement('img'); im.loading = 'lazy'; im.alt = label + ' at ' + name;
      im.src = CD + '/api/events/' + encodeURIComponent(e.id) + '/snapshot.jpg';
      const pl = document.createElement('span'); pl.className = 'cd-play'; pl.textContent = '\u25b6';
      b.append(im, pl); it.appendChild(b);
    } else { const ph = document.createElement('div'); ph.className = 'cd-row-thumb cd-row-none'; ph.textContent = 'No snapshot'; it.appendChild(ph); }
    const m = document.createElement('div'); m.className = 'cd-row-meta';
    const l = document.createElement('b'); l.textContent = label + ' ' + Math.round((e.score || 0) * 100) + '%';
    const c = document.createElement('span'); c.textContent = name;
    const z = document.createElement('span'); z.className = 'muted sm'; z.textContent = (e.zones && e.zones.length) ? 'Zones: ' + e.zones.join(', ') : 'No zone';
    const t = document.createElement('span'); t.className = 'muted sm'; t.textContent = fmt(e.start_ts);
    m.append(l, c, z, t); it.appendChild(m); return it;
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
    const base = '/cams/api/stream/' + encodeURIComponent(id) + '/';
    msg.textContent = 'Connecting…';
    const ua = navigator.userAgent, desk = /(Chrome|Firefox|Edg)\//.test(ua) && !/iPhone|iPad|iPod/.test(ua);
    const srcs = (!desk && v.canPlayType('application/vnd.apple.mpegurl')) ? [base + 'index.m3u8', base + 'index.m3u8?retry=1', base + 'live.mp4'] : [base + 'live.mp4', base + 'index.m3u8'];
    let k = 0; v.src = srcs[0];
    v.onplaying = () => { msg.textContent = 'Live'; };
    v.onerror = () => { if (route.view === 'cam' && ++k < srcs.length) { v.src = srcs[k]; const q = v.play(); if (q && q.catch) q.catch(() => {}); } else if (route.view === 'cam') msg.textContent = 'Live stream unavailable. Try again.'; };
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

  // ---------- routing ----------
  function parse() {
    const h = location.hash.replace(/^#/, '');
    if (h === 'detections') return { view: 'det' };
    const m = h.match(/^cam=([a-z0-9][a-z0-9-]{0,31})$/);
    if (m && hubCams.some(c => c.id === m[1])) return { view: 'cam', id: m[1] };
    return { view: 'grid' };
  }
  function show() {
    const prev = route; route = parse();
    if (prev.view === 'cam' && (route.view !== 'cam' || route.id !== prev.id)) liveStop();
    $('view-grid').hidden = route.view !== 'grid'; $('view-det').hidden = route.view !== 'det'; $('view-cam').hidden = route.view !== 'cam';
    $('cams-tabs').hidden = route.view === 'cam';
    document.querySelectorAll('.cams-tab').forEach(a => a.setAttribute('aria-current', a.dataset.view === route.view ? 'page' : 'false'));
    evStop();
    if (route.view !== 'cam') sessionStorage.removeItem('cams-in-app');
    if (route.view === 'grid') { if (visible()) gridStart(); } else gridStop();
    if (route.view === 'det') { allList.reset(); evStart(allList); }
    if (route.view === 'cam') {
      const c = hubCams.find(x => x.id === route.id);
      $('cam-name').textContent = c.name; document.title = c.name + ' · Cameras';
      try { window.scrollTo(0, 0); } catch (e) {}
      if (prev.view !== 'cam' || prev.id !== route.id) liveStart(c.id);
      $('cam-list').innerHTML = '<p class="muted sm">Loading…</p>'; $('cam-more').hidden = true; $('cam-off').hidden = true;
      cdReady.then(() => {
        if (route.view !== 'cam' || route.id !== c.id) return;
        camDet = detOf[c.id] || null;
        if (!camDet) { $('cam-off').hidden = false; $('cam-list').innerHTML = ''; return; }
        camList.reset(); evStart(camList);
      });
    } else document.title = 'Cameras · App Hub';
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
})();
