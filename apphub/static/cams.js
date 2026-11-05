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
    releasePlayback(); liveStop(); mode = 'live'; v.muted = true; v.autoplay = true; updatePill();
    const base = '/cams/api/stream/' + encodeURIComponent(id) + '/';
    msg.textContent = 'Connecting…';
    const ua = navigator.userAgent, desk = /(Chrome|Firefox|Edg)\//.test(ua) && !/iPhone|iPad|iPod/.test(ua);
    const srcs = (!desk && v.canPlayType('application/vnd.apple.mpegurl')) ? [base + 'index.m3u8', base + 'index.m3u8?retry=1', base + 'live.mp4'] : [base + 'live.mp4', base + 'index.m3u8'];
    let k = 0; v.src = srcs[0];
    v.onplaying = () => { msg.textContent = 'Live'; };
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
  let tlCam=null,generation=0,hls=null,data=null,mode='live',center=Date.now()/1000,zoom=3;
  let spans={},events=[],selected=false,rangeS=0,rangeE=0,exportTimer=null,exportSeq=0,pollTimer=null,followTimer=null,raf=0,loadTimer=null;
  let seekSequence=0,gesture=null,windowKey='',playDay='',soundWanted=false,loadedCenter=0;
  const cache=new Map(),playCache=new Map(),pointers=new Map();
  const span=()=>levels[zoom], left=()=>center-span()/2;
  const query=(start,end,extra={})=>new URLSearchParams({cam:tlCam,start:Math.floor(start),end:Math.ceil(end),...extra});
  const icons={back:'M10 8H4V2 M4 8a8 8 0 1 1-1 8 M9 12v6m3-6h3v6h-3z',forward:'M14 8h6V2 M20 8a8 8 0 1 0 1 8 M9 12v6m3-6h3v6h-3z',play:'m9 5 11 7-11 7z',pause:'M8 5v14M16 5v14','det-prev':'M6 5v14m13-14-10 7 10 7z','det-next':'M18 5v14M5 5l10 7-10 7z',out:'M5 12h14',in:'M5 12h14M12 5v14',calendar:'M4 6h16v15H4zM4 10h16M8 3v5M16 3v5',select:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',cancel:'m6 6 12 12M18 6 6 18'};
  function icon(id,name=id){$('tl-'+id).innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="'+icons[name]+'"/></svg>';}
  Object.keys(icons).filter(k=>k!=='pause').forEach(k=>icon(k));
  function updatePill(){const live=mode==='live'&&!gesture;$('cam-mode').textContent=live?'LIVE':fmt(center);$('cam-mode').classList.toggle('live',live);$('cam-live').hidden=mode==='live';if(mode==='live')$('cam-unmute').hidden=true;icon('play',v.paused?'play':'pause');}
  function drawTimeline(){if(!raf)raf=requestAnimationFrame(paint);}
  function paint(){raf=0;if(!tlCam)return;const w=bar.clientWidth,dpr=devicePixelRatio||1;if(!w)return;bar.width=Math.round(w*dpr);bar.height=60*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);const x=t=>(t-left())/span()*w;
    ctx.clearRect(0,0,w,60);ctx.fillStyle=getComputedStyle(bar).backgroundColor;ctx.beginPath();ctx.roundRect(0,0,w,44,10);ctx.fill();
    ctx.fillStyle='rgba(255,255,255,.18)';(spans.sub||[]).forEach(([a,b])=>ctx.fillRect(x(a),10,x(b)-x(a),24));
    ctx.fillStyle='rgba(255,255,255,.24)';(spans.main||[]).forEach(([a,b])=>ctx.fillRect(x(a),32,x(b)-x(a),2));
    const accent=getComputedStyle(bar).getPropertyValue('--acc').trim()||'#a58aff';ctx.fillStyle=accent;events.forEach(e=>{ctx.beginPath();ctx.arc(x(e.start_ts),22,3,0,Math.PI*2);ctx.fill();});
    if(selected){ctx.globalAlpha=.24;ctx.fillRect(x(rangeS),0,x(rangeE)-x(rangeS),44);ctx.globalAlpha=1;for(const t of [rangeS,rangeE]){ctx.fillRect(x(t)-3,2,6,40);}}
    ctx.fillStyle='rgba(255,255,255,.25)';ctx.fillRect(x(Date.now()/1000),2,1,40);
    ctx.fillStyle='#fff';ctx.fillRect(w/2-1,0,2,44);ctx.beginPath();ctx.moveTo(w/2-4,0);ctx.lineTo(w/2+4,0);ctx.lineTo(w/2,5);ctx.fill();
    const steps=[60,300,600,900,1800,3600,7200,10800,21600],step=steps.find(s=>s/span()*w>=56)||21600;
    ctx.fillStyle='#9298a3';ctx.font='10px system-ui';ctx.textAlign='center';
    for(let t=Math.ceil(left()/step)*step;t<left()+span();t+=step){const d=new Date(t*1000),label=d.toLocaleTimeString([],{hour:'numeric',...(d.getMinutes()?{minute:'2-digit'}:{}),hour12:true}).replace(':00','');if(x(t)>24&&x(t)<w-24)ctx.fillText(label,x(t),57);}
    updatePill();
  }
  function releasePlayback(){seekSequence++;if(hls)hls.destroy();hls=null;data=null;playDay='';v.onerror=null;v.onplaying=null;v.pause();v.removeAttribute('src');v.load();}
  function timelineStop(){generation++;tlCam=null;releasePlayback();clearTimeout(exportTimer);clearTimeout(loadTimer);clearInterval(pollTimer);clearInterval(followTimer);cancelAnimationFrame(raf);raf=0;exportSeq++;pointers.clear();gesture=null;cache.clear();playCache.clear();}
  async function refreshTimeline(force=false){if(!tlCam||!visible())return;const g=generation,a=Math.floor((center-span()*1.5)/3600)*3600,b=Math.ceil((center+span()*1.5)/3600)*3600,key=localDate(new Date(center*1000))+':'+a+':'+b;windowKey=key;
    try{let j=cache.get(key);if(!j||force){const timeline=await get(CR+'/api/timeline?'+query(a,b));await cdReady;if(g!==generation)return;const evs=detOf[tlCam]?await get(CD+'/api/events?'+new URLSearchParams({camera:detOf[tlCam],since:a,until:b,limit:500})).catch(()=>[]):[];j={spans:timeline.spans,events:evs};cache.set(key,j);if(cache.size>30)cache.delete(cache.keys().next().value);}if(g!==generation||key!==windowKey)return;loadedCenter=center;spans=j.spans||{};events=(Array.isArray(j.events)?j.events:[]).sort((a,b)=>a.start_ts-b.start_ts);drawTimeline();}catch(e){if(g===generation)$('tl-msg').textContent=e.message;}}
  function moved(){center=Math.min(center,Date.now()/1000);drawTimeline();clearTimeout(loadTimer);loadTimer=setTimeout(()=>refreshTimeline(),120);}
  function timelineStart(id){timelineStop();tlCam=id;mode='live';center=Date.now()/1000;zoom=3;spans={};events=[];selected=false;$('tl-range').hidden=true;$('tl-select').setAttribute('aria-pressed','false');$('tl-date').max=localDate(new Date());$('tl-msg').textContent='';$('cam-unmute').hidden=true;refreshTimeline();drawTimeline();pollTimer=setInterval(()=>{if(visible()&&Math.abs(Date.now()/1000-center)<=span()/2){playCache.clear();refreshTimeline(true);}},60000);followTimer=setInterval(()=>{if(tlCam&&visible()&&mode==='live'&&!gesture){center=Date.now()/1000;drawTimeline();}},1000);}
  async function playSound(){v.muted=!soundWanted;try{await v.play();$('cam-unmute').hidden=v.muted===false;}catch(e){v.muted=true;$('cam-unmute').hidden=false;v.play().catch(()=>{$('tl-msg').textContent='Tap Play to start.';});}}
  async function seekWall(t){const g=generation,id=++seekSequence;if(mode==='live')liveStop();mode='playback';soundWanted=true;center=Math.min(t,Date.now()/1000);moved();updatePill();const [a,b]=dayBounds(center),key=localDate(new Date(center*1000));
    try{if(!data||playDay!==key){let j=playCache.get(key);if(!j){j=await get(CR+'/api/playback?'+query(a,b));if(g!==generation||id!==seekSequence)return;playCache.set(key,j);}if(!j.segments.length)throw new Error('No footage for this day');releasePlayback();seekSequence=id;data=j;playDay=key;const url=new URL(j.playlist,location.origin+CR+'/api/').href;if(window.Hls&&Hls.isSupported()){hls=new Hls({enableWorker:false,lowLatencyMode:false,maxBufferLength:30,backBufferLength:60});hls.loadSource(url);hls.attachMedia(v);}else v.src=url;}
      if(g!==generation||id!==seekSequence)return;const e=data.segments.find(e=>e.end>center)||data.segments[data.segments.length-1],media=e.offset+clamp(center-e.start,0,e.duration-.01);center=e.start+media-e.offset;
      const seek=()=>{if(g!==generation||id!==seekSequence)return;v.currentTime=media;playSound();};if(v.readyState)seek();else v.addEventListener('loadedmetadata',seek,{once:true});$('tl-msg').textContent='';drawTimeline();
    }catch(e){if(g===generation)$('tl-msg').textContent=e.message;}}
  v.addEventListener('timeupdate',()=>{if(mode!=='playback'||!data||gesture)return;const e=data.segments.find(e=>v.currentTime>=e.offset&&v.currentTime<e.offset+e.duration);if(e){const previous=localDate(new Date(center*1000));center=e.start+v.currentTime-e.offset;drawTimeline();if(previous!==localDate(new Date(center*1000))||Math.abs(center-loadedCenter)>span()/2)refreshTimeline();}});
  v.addEventListener('play',()=>drawTimeline());v.addEventListener('pause',()=>drawTimeline());
  v.addEventListener('ended',()=>{if(mode==='playback'){const next=data&&data.segments[data.segments.length-1];if(next&&localDate(new Date((next.end+1)*1000))!==playDay)seekWall(next.end+1);}});
  $('cam-live').addEventListener('click',()=>{center=Date.now()/1000;liveStart(tlCam);moved();});
  $('cam-unmute').addEventListener('click',()=>{soundWanted=true;v.muted=false;v.play().then(()=>{$('cam-unmute').hidden=true;}).catch(()=>{});});
  function zoomAt(delta){zoom=clamp(zoom+delta,0,levels.length-1);moved();}
  $('tl-in').addEventListener('click',()=>zoomAt(1));$('tl-out').addEventListener('click',()=>zoomAt(-1));
  const xTime=x=>left()+(x-bar.getBoundingClientRect().left)/bar.clientWidth*span();
  bar.addEventListener('wheel',e=>{if(e.ctrlKey||e.metaKey){e.preventDefault();zoomAt(e.deltaY<0?1:-1);}else if(Math.abs(e.deltaX)>Math.abs(e.deltaY)){e.preventDefault();if(mode==='live')liveStop();mode='playback';v.pause();center+=e.deltaX/bar.clientWidth*span();moved();clearTimeout(loadTimer);loadTimer=setTimeout(()=>seekWall(center),220);}},{passive:false});
  bar.addEventListener('pointerdown',e=>{bar.setPointerCapture(e.pointerId);pointers.set(e.pointerId,e.clientX);const t=xTime(e.clientX),hit=span()*22/bar.clientWidth;if(pointers.size===1)gesture={x:e.clientX,center,moved:false,kind:selected&&Math.abs(t-rangeS)<hit?'start':selected&&Math.abs(t-rangeE)<hit?'end':'pan'};else{gesture.pinch=Math.abs([...pointers.values()][0]-[...pointers.values()][1]);gesture.multi=true;}v.pause();});
  bar.addEventListener('pointermove',e=>{if(!pointers.has(e.pointerId)||!gesture)return;pointers.set(e.pointerId,e.clientX);if(pointers.size===2){const p=[...pointers.values()],dist=Math.abs(p[0]-p[1]);if(Math.abs(dist-gesture.pinch)>30){zoomAt(dist>gesture.pinch?1:-1);gesture.pinch=dist;}return;}if(gesture.multi)return;const dx=e.clientX-gesture.x;if(Math.abs(dx)>5)gesture.moved=true;if(!gesture.moved)return;if(gesture.kind==='start'){rangeS=clamp(xTime(e.clientX),rangeE-1800,rangeE-1);syncRange();}else if(gesture.kind==='end'){rangeE=clamp(xTime(e.clientX),rangeS+1,rangeS+1800);syncRange();}else{center=gesture.center-dx/bar.clientWidth*span();moved();}});
  bar.addEventListener('pointerup',e=>{pointers.delete(e.pointerId);if(!gesture||pointers.size)return;const g=gesture;gesture=null;if(g.multi){drawTimeline();return;}if(g.kind!=='pan'&&g.moved)return;let t=g.moved?center:xTime(e.clientX);const hit=span()*10/bar.clientWidth,ev=events.reduce((best,e)=>Math.abs(e.start_ts-t)<hit&&(!best||Math.abs(e.start_ts-t)<Math.abs(best.start_ts-t))?e:best,null);seekWall(!g.moved&&ev?ev.start_ts-5:t);});
  bar.addEventListener('pointercancel',()=>{pointers.clear();gesture=null;drawTimeline();});
  bar.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();seekWall(center+(e.key==='ArrowLeft'?-10:10));}else if(e.key==='+'||e.key==='-'){e.preventDefault();zoomAt(e.key==='+'?1:-1);}});
  $('tl-back').addEventListener('click',()=>seekWall(center-10));$('tl-forward').addEventListener('click',()=>seekWall(center+10));
  $('tl-play').addEventListener('click',()=>{if(mode==='live'||!data)seekWall(center);else if(v.paused)playSound();else v.pause();});
  for(const [id,dir] of [['det-prev',-1],['det-next',1]])$('tl-'+id).addEventListener('click',()=>{const list=dir<0?[...events].reverse():events,e=list.find(e=>dir<0?e.start_ts<center-1:e.start_ts>center+6);if(e)seekWall(e.start_ts-5);});
  $('tl-calendar').addEventListener('click',()=>{const d=$('tl-date');d.value=localDate(new Date(center*1000));try{d.showPicker();}catch(e){d.focus();d.click();}});
  $('tl-date').addEventListener('change',async()=>{const t=+new Date($('tl-date').value+'T12:00:00')/1000;if(!Number.isFinite(t))return;const g=generation,[a,b]=dayBounds(t);try{const j=await get(CR+'/api/timeline?'+query(a,b));if(g!==generation)return;seekWall(j.spans.sub.length?j.spans.sub[0][0]:t);}catch(e){$('tl-msg').textContent=e.message;}});
  const timeInput=t=>new Date(t*1000).toTimeString().slice(0,8);
  function syncRange(changed){rangeS=Math.round(rangeS);rangeE=Math.round(rangeE);if(changed==='start')rangeS=clamp(rangeS,rangeE-1800,rangeE-1);else rangeE=clamp(rangeE,rangeS+1,rangeS+1800);$('tl-start').value=timeInput(rangeS);$('tl-end').value=timeInput(rangeE);$('tl-start').title=fmt(rangeS);$('tl-end').title=fmt(rangeE);const duration=rangeE-rangeS;$('tl-duration').textContent=Math.floor(duration/60)+'m '+duration%60+'s';drawTimeline();clearTimeout(exportTimer);const seq=++exportSeq;$('tl-download').hidden=true;$('tl-export-msg').textContent='Estimating…';exportTimer=setTimeout(async()=>{try{const j=await get(CR+'/api/export/info?'+query(rangeS,rangeE));if(seq!==exportSeq||!selected)return;const q=j.recommended;$('tl-export-msg').textContent=j.busy?'Another export is running. Try again shortly.':q==='sub'?'Full-res is no longer kept.':!q?(stOf(j,'main')==='not_ready'||stOf(j,'sub')==='not_ready'?'Footage is not ready yet.':'No footage in this range.'):'';if(q&&!j.busy){const a=$('tl-download');a.href=CR+'/api/export?'+query(rangeS,rangeE,{quality:'main',fallback:1});a.textContent='Download '+(q==='main'?'full-res':'low-res')+' · '+Math.max(1,Math.round(j[q].bytes/1e6))+' MB';a.hidden=false;}}catch(e){if(seq===exportSeq)$('tl-export-msg').textContent=e.message;}},350);}
  function rangeMode(on){selected=on;$('tl-range').hidden=!on;$('tl-select').setAttribute('aria-pressed',String(on));if(on){rangeS=center-60;rangeE=center+60;zoom=7;moved();syncRange();}else{clearTimeout(exportTimer);exportSeq++;drawTimeline();}}
  $('tl-select').addEventListener('click',()=>rangeMode(!selected));$('tl-cancel').addEventListener('click',()=>rangeMode(false));
  for(const id of ['start','end'])$('tl-'+id).addEventListener('change',()=>{const current=id==='start'?rangeS:rangeE,d=new Date(current*1000),parts=$('tl-'+id).value.split(':').map(Number);if(parts.length<2)return;d.setHours(parts[0],parts[1],parts[2]||0,0);if(id==='start')rangeS=+d/1000;else rangeE=+d/1000;syncRange(id);});
  new ResizeObserver(()=>{if(tlCam)drawTimeline();}).observe(bar);
  document.addEventListener('visibilitychange',()=>{if(!visible()){v.pause();clearTimeout(loadTimer);}else if(tlCam){if(mode==='live'){center=Date.now()/1000;liveStart(tlCam);}refreshTimeline(true);drawTimeline();}});
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
      if (prev.view !== 'cam' || prev.id !== route.id) { timelineStart(c.id); liveStart(c.id); }
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
