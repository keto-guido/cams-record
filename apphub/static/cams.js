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

  // Recording timeline: wall time is mapped through indexed media offsets, never through gaps.
  const tv = $('tl-video'), bar = $('tl-bar'), ctx = bar.getContext('2d');
  const levels = [86400,43200,21600,10800,3600,1800,600,300];
  const localDate = d => [d.getFullYear(), String(d.getMonth()+1).padStart(2,'0'), String(d.getDate()).padStart(2,'0')].join('-');
  let tlCam = null, generation = 0, hls = null, data = null, spans = {}, events = [], dayS = 0, dayE = 0;
  let left = 0, zoom = 0, wall = 0, selected = false, rangeS = 0, rangeE = 0, exportTimer = null, exportSeq = 0;
  const pointers = new Map(); let gesture = null;
  const widthS = () => zoom === 0 ? dayE-dayS : levels[zoom];
  const clamp = (n,a,b) => Math.max(a,Math.min(b,n));
  const query = extra => new URLSearchParams({cam:tlCam,start:dayS,end:dayE,...extra});
  const get = async url => { const r = await fetch(url,{credentials:'same-origin'}); if(!r.ok) throw new Error('Recording unavailable ('+r.status+')'); return r.json(); };
  function releasePlayback() { if(hls) hls.destroy(); hls=null; tv.pause(); tv.removeAttribute('src'); tv.load(); data=null; }
  function timelineStop() { generation++; tlCam=null; releasePlayback(); clearTimeout(exportTimer); exportSeq++; pointers.clear(); }
  function xTime(x) { const r=bar.getBoundingClientRect(); return clamp(left+(x-r.left)/r.width*widthS(),dayS,dayE); }
  function drawTimeline() {
    const r=bar.getBoundingClientRect(), dpr=window.devicePixelRatio||1;
    bar.width=Math.round(r.width*dpr); bar.height=120*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
    const w=r.width, x=t=>(t-left)/widthS()*w;
    ctx.fillStyle='#111820'; ctx.fillRect(0,0,w,120);
    for(const [q,y,h,color] of [['sub',46,24,'#4c9de8'],['main',75,8,'#5bd095']]) {
      ctx.fillStyle=color; (spans[q]||[]).forEach(([a,b])=>ctx.fillRect(x(a),y,x(b)-x(a),h));
    }
    const step=widthS()>21600?7200:widthS()>3600?1800:widthS()>600?300:60;
    ctx.font='11px system-ui';
    for(let t=Math.ceil(left/step)*step;t<left+widthS();t+=step) { ctx.fillStyle='#77818d'; ctx.fillRect(x(t),28,1,60); ctx.fillText(new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),x(t)+3,20); }
    ctx.fillStyle='#ffb34f'; events.forEach(e=>ctx.fillRect(x(e.start_ts)-2,36,4,52));
    const marker=(t,color,label)=>{ if(t<left||t>left+widthS()) return; ctx.fillStyle=color; ctx.fillRect(x(t)-1,26,2,78); ctx.fillText(label,clamp(x(t)+4,0,w-50),114); };
    if(Date.now()/1000>=dayS&&Date.now()/1000<dayE) marker(Date.now()/1000,'#ed5967','Now');
    marker(wall,'#fff','Play');
    if(selected) { ctx.fillStyle='#ffffff22';ctx.fillRect(x(rangeS),26,x(rangeE)-x(rangeS),78); marker(rangeS,'#b99bff','Start'); marker(rangeE,'#b99bff','End'); }
    $('tl-span').textContent=widthS()>=3600?(widthS()/3600).toFixed(1)+' h':Math.round(widthS()/60)+' min';
    $('tl-clock').textContent=wall?new Date(wall*1000).toLocaleString([],{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'}):'';
    $('tl-next').disabled=$('tl-date').value>=localDate(new Date());
  }
  async function refreshTimeline() {
    if(!tlCam||!visible()) return;
    const g=generation, q=query();
    try { const j=await get(CR+'/api/timeline?'+q); if(g!==generation)return; spans=j.spans; drawTimeline(); } catch(e) { if(g===generation)$('tl-msg').textContent=e.message; }
    await cdReady; if(g!==generation)return;
    if(detOf[tlCam]) { try { const j=await get(CD+'/api/events?'+new URLSearchParams({camera:detOf[tlCam],since:dayS,until:dayE,limit:500})); if(g===generation){events=(Array.isArray(j)?j:[]).sort((a,b)=>a.start_ts-b.start_ts);drawTimeline();} } catch(e){} }
  }
  function changeDay() {
    generation++; releasePlayback(); spans={}; events=[]; selected=false; $('tl-range').hidden=true; $('tl-select').setAttribute('aria-pressed','false'); $('tl-player').hidden=true;
    const d=new Date($('tl-date').value+'T00:00:00'); dayS=d.getTime()/1000; d.setDate(d.getDate()+1);dayE=d.getTime()/1000;
    left=dayS;wall=dayS;zoom=0;drawTimeline();refreshTimeline();
  }
  function timelineStart(id) { timelineStop();tlCam=id;$('tl-date').max=localDate(new Date());$('tl-date').value=localDate(new Date());changeDay(); }
  async function loadPlayback() {
    const g=generation, j=await get(CR+'/api/playback?'+query()); if(g!==generation)return false;
    releasePlayback(); data=j; if(!j.segments.length)throw new Error('No footage for this day');
    const url=new URL(j.playlist,location.origin+CR+'/api/').href;
    $('tl-player').hidden=false;
    if(window.Hls&&window.Hls.isSupported()) { hls=new window.Hls({enableWorker:false,lowLatencyMode:false,maxBufferLength:30,backBufferLength:60});hls.loadSource(url);hls.attachMedia(tv); }
    else if(tv.canPlayType('application/vnd.apple.mpegurl')) tv.src=url;
    else throw new Error('Playback not supported in this browser');
    return true;
  }
  let seekSequence = 0;
  async function seekWall(t,reload=false) {
    const g=generation, seekId=++seekSequence;
    try {
      if(!data||reload) { if(!await loadPlayback())return; }
      if(g!==generation)return;
      const e=data.segments.find(e=>e.end>t)||data.segments[data.segments.length-1];
      const media=e.offset+clamp(t-e.start,0,e.duration-0.01);wall=e.start+media-e.offset;
      const seek=()=>{if(g!==generation||seekId!==seekSequence)return;tv.currentTime=media;tv.play().catch(()=>{$('tl-msg').textContent='Tap Play to start with sound.';});};
      if(tv.readyState)seek();else tv.addEventListener('loadedmetadata',seek,{once:true});
      $('tl-msg').textContent='';drawTimeline();
    } catch(e){if(g===generation)$('tl-msg').textContent=e.message;}
  }
  tv.addEventListener('timeupdate',()=>{if(!data||gesture)return;const e=data.segments.find(e=>tv.currentTime>=e.offset&&tv.currentTime<e.offset+e.duration);if(e){wall=e.start+tv.currentTime-e.offset;drawTimeline();}});
  tv.addEventListener('ended',()=>{if($('tl-date').value===localDate(new Date()))seekWall(wall+0.1,true);});
  $('tl-date').addEventListener('change',changeDay);
  for(const [id,delta] of [['prev',-1],['next',1]])$('tl-'+id).addEventListener('click',()=>{const d=new Date($('tl-date').value+'T12:00:00');d.setDate(d.getDate()+delta);if(localDate(d)>localDate(new Date()))return;$('tl-date').value=localDate(d);changeDay();});
  function zoomAt(delta,t=left+widthS()/2){const old=widthS(),ratio=(t-left)/old;zoom=clamp(zoom+delta,0,levels.length-1);left=clamp(t-ratio*widthS(),dayS,dayE-widthS());drawTimeline();}
  $('tl-in').addEventListener('click',()=>zoomAt(1));$('tl-out').addEventListener('click',()=>zoomAt(-1));
  bar.addEventListener('wheel',e=>{if(e.ctrlKey||e.metaKey){e.preventDefault();zoomAt(e.deltaY<0?1:-1,xTime(e.clientX));}else if(zoom&&Math.abs(e.deltaX)>Math.abs(e.deltaY)){e.preventDefault();left=clamp(left+e.deltaX/bar.clientWidth*widthS(),dayS,dayE-widthS());drawTimeline();}},{passive:false});
  bar.addEventListener('pointerdown',e=>{bar.setPointerCapture(e.pointerId);pointers.set(e.pointerId,e.clientX);const t=xTime(e.clientX),hit=widthS()*22/bar.clientWidth;gesture={x:e.clientX,left,t,moved:false,kind:selected&&Math.abs(t-rangeS)<hit?'start':selected&&Math.abs(t-rangeE)<hit?'end':Math.abs(t-wall)<hit?'head':'pan'};if(pointers.size===2)gesture.pinch=Math.abs([...pointers.values()][0]-[...pointers.values()][1]);});
  bar.addEventListener('pointermove',e=>{if(!pointers.has(e.pointerId)||!gesture)return;pointers.set(e.pointerId,e.clientX);if(pointers.size===2){const a=[...pointers.values()],distance=Math.abs(a[0]-a[1]);if(gesture.pinch&&Math.abs(distance-gesture.pinch)>35){zoomAt(distance>gesture.pinch?1:-1,xTime((a[0]+a[1])/2));gesture.pinch=distance;}gesture.moved=true;return;}const dx=e.clientX-gesture.x;if(Math.abs(dx)>6)gesture.moved=true;if(!gesture.moved)return;const t=xTime(e.clientX);if(gesture.kind==='start'){rangeS=Math.min(t,rangeE-1);syncRange('start');}else if(gesture.kind==='end'){rangeE=Math.max(t,rangeS+1);syncRange('end');}else if(gesture.kind==='head')wall=t;else if(zoom)left=clamp(gesture.left-dx/bar.clientWidth*widthS(),dayS,dayE-widthS());drawTimeline();});
  bar.addEventListener('pointerup',e=>{pointers.delete(e.pointerId);if(!gesture)return;const g=gesture;gesture=null;if(pointers.size)return;if(g.kind==='head'&&g.moved)seekWall(wall);else if(!g.moved){const t=xTime(e.clientX),hit=widthS()*8/bar.clientWidth,ev=events.find(ev=>Math.abs(ev.start_ts-t)<hit);seekWall(ev?ev.start_ts-5:t);}});
  bar.addEventListener('pointercancel',()=>{pointers.clear();gesture=null;});
  $('tl-back').addEventListener('click',()=>seekWall(wall-10));$('tl-forward').addEventListener('click',()=>seekWall(wall+10));
  $('tl-play').addEventListener('click',()=>{if(!data)seekWall(wall);else if(tv.paused)tv.play().catch(()=>{$('tl-msg').textContent='Tap video Play to start.';});else tv.pause();});
  for(const [id,dir] of [['det-prev',-1],['det-next',1]])$('tl-'+id).addEventListener('click',()=>{const list=dir<0?[...events].reverse():events;const e=list.find(e=>dir<0?e.start_ts<wall-1:e.start_ts>wall+1);if(e)seekWall(e.start_ts-5);});
  $('tl-fs').addEventListener('click',()=>{const el=$('tl-player');if(document.fullscreenElement||document.webkitFullscreenElement)(document.exitFullscreen||document.webkitExitFullscreen).call(document);else if(el.requestFullscreen)el.requestFullscreen().catch(()=>{if(tv.webkitEnterFullscreen)tv.webkitEnterFullscreen();});else if(el.webkitRequestFullscreen)el.webkitRequestFullscreen();else if(tv.webkitEnterFullscreen)tv.webkitEnterFullscreen();});
  const timeInput=t=>new Date(t*1000).toTimeString().slice(0,8);
  function syncRange(changed) {
    rangeS=clamp(Math.round(rangeS),dayS,dayE-1);rangeE=clamp(Math.round(rangeE),rangeS+1,dayE);
    if(rangeE-rangeS>1800){if(changed==='start')rangeS=rangeE-1800;else rangeE=rangeS+1800;$('tl-export-msg').textContent='Range limited to 30 minutes.';}
    $('tl-start').value=timeInput(rangeS);$('tl-end').value=timeInput(rangeE);$('tl-duration').textContent=(rangeE-rangeS)+' s';drawTimeline();
    clearTimeout(exportTimer);const seq=++exportSeq;$('tl-download').hidden=true;
    exportTimer=setTimeout(async()=>{try{const j=await get(CR+'/api/export/info?'+query({start:rangeS,end:rangeE}));if(seq!==exportSeq||!selected)return;const q=j.recommended;
      $('tl-export-msg').textContent=j.busy?'another export is running, try again shortly':q==='sub'?'Full-res is no longer kept.':!q?(j.main.status==='not_ready'||j.sub.status==='not_ready'?'Footage is not ready yet.':'No footage in this range.'):'';
      if(q&&!j.busy){const a=$('tl-download');a.href=CR+'/api/export?'+query({start:rangeS,end:rangeE,quality:q,fallback:1});a.textContent='Download '+(q==='main'?'full-res':'low-res')+' (~'+Math.max(1,Math.round(j[q].bytes/1e6))+' MB)';a.hidden=false;}
    }catch(e){if(seq===exportSeq)$('tl-export-msg').textContent=e.message;}},350);
  }
  $('tl-select').addEventListener('click',()=>{selected=!selected;$('tl-select').setAttribute('aria-pressed',String(selected));$('tl-range').hidden=!selected;if(selected){rangeS=wall-60;rangeE=wall+60;syncRange();}else{exportSeq++;clearTimeout(exportTimer);drawTimeline();}});
  for(const id of ['start','end'])$('tl-'+id).addEventListener('change',()=>{const t=new Date($('tl-date').value+'T'+$('tl-'+id).value).getTime()/1000;if(!Number.isFinite(t))return;if(id==='start')rangeS=t;else rangeE=t;syncRange(id);});
  new ResizeObserver(()=>{if(tlCam)drawTimeline();}).observe(bar);
  setInterval(()=>{if(tlCam&&visible()&&$('tl-date').value===localDate(new Date()))refreshTimeline();},60000);
  window.addEventListener('pagehide',timelineStop);

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
    if (prev.view === 'cam' && (route.view !== 'cam' || route.id !== prev.id)) { liveStop(); timelineStop(); }
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
      if (prev.view !== 'cam' || prev.id !== route.id) { liveStart(c.id); timelineStart(c.id); }
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
