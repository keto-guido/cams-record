'use strict';
// Cameras page (/cams/). Access is enforced by server.js (app "cams" ACL, 404 otherwise) before handle() runs.
// Live view: MPEG-TS HLS (go2rtc's fMP4 HLS segments fail native HLS demuxers) for Safari/iOS, fMP4 progressive for desktop.
// The browser only sees opaque camera ids; RTSP URLs/credentials live in /volume1/docker/cams/go2rtc.yaml (root 600).
// Per-camera access: every route below goes through `access`, keyed on the session user. Client x-apphub-* headers
// reach this module unstripped, so they are never read. A camera the user may not see answers exactly like an unknown one.
const http = require('http'), fs = require('fs'), path = require('path');
const G = { host: '127.0.0.1', port: 1984 };
const CFG = path.join(require('./lib').DIR, 'cams.json');
const SID_TTL = 120e3, SID_MAX = 1000;
module.exports = function (C) {
  const { esc, send, page, notFound } = C;
  const access = C.cameras || require('./camera_acl').fileAccess(CFG), RECHECK = C.recheckMs || 15e3;
  const streamHeaders = type => ({ 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'", 'x-accel-buffering': 'no' });
  const CSP = "default-src 'self'; worker-src 'self'; manifest-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";
  const snaps = new Map(); // id -> {t, buf, waiters}
  // still(): checked periodically, so a revocation also cuts a connection that is already open.
  function upstream(res, gpath, type, timeout, still) {
    const up = http.get({ ...G, path: gpath }, ur => {
      if (ur.statusCode !== 200) { ur.resume(); return send(res, 502, 'camera unavailable', 'text/plain'); }
      res.writeHead(200, streamHeaders(type || ur.headers['content-type'] || 'application/octet-stream'));
      ur.pipe(res);
    });
    up.setTimeout(timeout || 30e3, () => up.destroy(new Error('timeout')));
    up.on('error', () => { if (!res.headersSent) send(res, 502, 'camera unavailable', 'text/plain'); else res.destroy(); });
    const watch = still && setInterval(() => { if (!still()) res.destroy(); }, RECHECK);
    if (watch) watch.unref();
    res.on('close', () => { clearInterval(watch); up.destroy(); });
  }
  // go2rtc's HLS session ids are not tied to a camera, so record whose session each one is: without this,
  // access to one camera plus another camera's session id would reach the other camera's segments.
  const sids = new Map(); // sid -> {cam, t}
  setInterval(() => { const c = Date.now() - SID_TTL; for (const [k, v] of sids) if (v.t < c) sids.delete(k); }, 30e3).unref();
  function hlsIndex(res, id) {
    const up = http.get({ ...G, path: `/api/stream.m3u8?src=${id}` }, ur => {
      if (ur.statusCode !== 200) { ur.resume(); return send(res, 502, 'camera unavailable', 'text/plain'); }
      const a = [];
      ur.on('data', d => a.push(d));
      ur.on('end', () => {
        const body = Buffer.concat(a);
        for (const m of body.toString().matchAll(/[?&]id=([A-Za-z0-9_-]{1,64})/g)) {
          if (sids.size >= SID_MAX) sids.delete(sids.keys().next().value);
          sids.set(m[1], { cam: id, t: Date.now() });
        }
        res.writeHead(200, streamHeaders('application/vnd.apple.mpegurl')); res.end(body);
      });
    });
    up.setTimeout(30e3, () => up.destroy(new Error('timeout')));
    up.on('error', () => { if (!res.headersSent) send(res, 502, 'camera unavailable', 'text/plain'); else res.destroy(); });
    res.on('close', () => up.destroy());
  }
  function snap(res, id, src) {
    const c = snaps.get(id) || {}; snaps.set(id, c);
    const out = b => b ? send(res, 200, b, 'image/jpeg') : send(res, 502, 'camera unavailable', 'text/plain');
    if (c.buf && Date.now() - c.t < 3000) return out(c.buf);
    if (c.waiters) return c.waiters.push(out);
    c.waiters = [out];
    // Always clear waiters exactly once — a thrown/rejected http.get or timeout must not wedge later snaps.
    let settled = false;
    const done = b => {
      if (settled) return;
      settled = true;
      const w = c.waiters; c.waiters = null;
      if (b) { c.buf = b; c.t = Date.now(); }
      (w || []).forEach(f => f(b || null));
    };
    let up;
    try {
      up = http.get({ ...G, path: '/api/frame.jpeg?src=' + encodeURIComponent(src) }, ur => {
        const a = [];
        ur.on('data', d => a.push(d));
        ur.on('end', () => done(ur.statusCode === 200 ? Buffer.concat(a) : null));
        ur.on('error', () => done(null));
      });
    } catch (e) {
      return done(null);
    }
    up.setTimeout(15e3, () => { up.destroy(new Error('timeout')); done(null); });
    up.on('error', () => done(null));
  }
  // go2rtc hands out each HLS segment/init only once; players (Safari, Chrome) often re-request the same one,
  // which then comes back empty/garbled -> "could not parse". Cache them briefly so repeats get identical bytes.
  const segs = new Map(); // gpath -> {t, buf, waiters}
  setInterval(() => { const c = Date.now() - 30e3; for (const [k, v] of segs) if (!v.waiters && v.t < c) segs.delete(k); }, 10e3).unref();
  function seg(res, gp, type) {
    const out = b => b ? send(res, 200, b, type) : send(res, 404, 'not found', 'text/plain');
    let c = segs.get(gp);
    if (c && c.buf) return out(c.buf);
    if (c && c.waiters) return c.waiters.push(out);
    if (segs.size > 300) segs.delete(segs.keys().next().value);
    c = { t: Date.now(), waiters: [out] }; segs.set(gp, c);
    const done = b => { const w = c.waiters; c.waiters = null; c.t = Date.now(); if (b && b.length) c.buf = b; else segs.delete(gp); w.forEach(f => f(b && b.length ? b : null)); };
    const up = http.get({ ...G, path: gp }, ur => { const a = []; ur.on('data', d => a.push(d)); ur.on('end', () => done(ur.statusCode === 200 ? Buffer.concat(a) : null)); });
    up.setTimeout(30e3, () => up.destroy(new Error('timeout'))); up.on('error', () => done(null));
  }
  function html(s) {
    const cams = access.list(s.user);
    return page('Cameras · MERSINO NETWORK', `<nav class="mn-subnav cams-tabs" id="cams-tabs" aria-label="Sections"><a class="cams-tab" data-view="grid" href="#">Live</a><a class="cams-tab" data-view="det" href="#detections">Detections</a><a class="cams-tab" data-view="alerts" href="#alerts">Alerts</a><a class="cams-tab" data-view="admin" href="#admin" id="tab-admin" hidden>Admin</a></nav>
<main class="wrap"><section id="view-grid"><div class="push-bar" id="push-bar" hidden><div class="push-copy"><strong class="push-title">Detection alerts</strong><p class="muted sm" id="push-status" role="status">Checking notification support…</p></div><div class="push-actions"><button type="button" class="ghost" id="push-toggle" hidden>Enable</button><button type="button" class="ghost" id="push-test" hidden>Send test</button></div></div><p class="muted sm">Snapshots refresh every few seconds. Tap a camera to open it.</p><div class="camgrid" id="camgrid">${cams.map(c => `<button type="button" class="camtile" data-id="${esc(c.id)}" data-cam="${esc(c.id)}" data-name="${esc(c.name)}"><img alt="${esc(c.name)} snapshot" src="/cams/api/snap/${esc(c.id)}"><span class="camname">${esc(c.name)}</span><span class="camage muted sm"></span></button>`).join('') || '<p class="muted">No cameras configured.</p>'}</div></section>
<section id="view-det" hidden><div class="cd-filters"><select id="det-cam" aria-label="Camera"><option value="">All cameras</option></select><select id="det-label" aria-label="Label"><option value="">All labels</option><option value="person">Person</option><option value="car">Car</option><option value="dog">Dog</option><option value="cat">Cat</option></select></div><div class="cd-rows" id="det-list"></div><button type="button" class="ghost cd-more" id="det-more" hidden>Load more</button></section>
<section id="view-alerts" hidden><h1 class="ap-title">Phone alerts</h1><p class="muted sm">Pause a camera's phone notifications for a while. Recording, the Detections list and Home Assistant keep working, and nothing that happened during the pause is sent afterwards.</p><p class="muted sm" id="ap-status" role="status"></p><div class="ap-rows" id="ap-list"></div></section>
<section id="view-cam" hidden><div class="cam-head"><button type="button" class="ghost cam-back" id="cam-back">← Cameras</button><h1 id="cam-name"></h1><span class="ap-badge" id="cam-paused" hidden></span></div><div class="cam-player" id="cam-player"><video id="cam-video" playsinline muted autoplay controls></video><button type="button" id="cam-retry" hidden>Couldn't load this recording. Tap to retry.</button><output id="cam-mode" class="cam-mode live">LIVE</output><output id="cam-toast" hidden></output><button type="button" id="cam-live" hidden>LIVE</button><button type="button" id="cam-unmute" hidden>Tap for sound</button><button type="button" class="cam-fs" id="cam-fs" aria-label="Full screen">⛶</button><button type="button" class="ap-bell ap-bell-player" id="cam-bell" aria-haspopup="menu" aria-expanded="false" aria-controls="ap-menu" hidden></button></div><p class="muted sm" id="cam-msg" role="status"></p><section id="timeline" aria-label="Recording history"><canvas id="tl-bar" tabindex="0" aria-label="Recording history: drag to scrub, pinch to zoom"></canvas><div class="tl-controls"><button type="button" class="tl-icon" id="tl-back" aria-label="Back 10 seconds" title="Back 10 seconds"></button><button type="button" class="tl-icon" id="tl-play" aria-label="Play or pause" title="Play or pause"></button><button type="button" class="tl-icon" id="tl-forward" aria-label="Forward 10 seconds" title="Forward 10 seconds"></button><button type="button" class="tl-icon tl-det" id="tl-det-prev" aria-label="Previous detection" title="Previous detection"><span class="tl-cap">Prev event</span></button><button type="button" class="tl-icon tl-det" id="tl-det-next" aria-label="Next detection" title="Next detection"><span class="tl-cap">Next event</span></button><div class="tl-zoom"><button type="button" class="tl-icon" id="tl-out" aria-label="Zoom out" title="Zoom out"></button><button type="button" class="tl-icon" id="tl-in" aria-label="Zoom in" title="Zoom in"></button></div><button type="button" class="tl-icon" id="tl-calendar" aria-label="Choose date" title="Choose date"></button><button type="button" class="tl-icon" id="tl-select" aria-label="Download range" title="Download range" aria-pressed="false"></button><input id="tl-date" type="date" aria-label="Recording date" tabindex="-1"></div><p id="tl-msg" role="status"></p><div id="tl-range" hidden><button type="button" class="tl-icon tl-cancel" id="tl-cancel" aria-label="Cancel download" title="Cancel download"></button><div class="tl-times"><label>Start<input id="tl-start" type="time" step="1"></label><label>End<input id="tl-end" type="time" step="1"></label><output id="tl-duration"></output></div><p id="tl-export-msg" role="status"></p><a id="tl-download" download hidden>Download</a></div></section><h2 class="cam-sub">Detections</h2><p class="muted" id="cam-off" hidden>Detection is off for this camera.</p><div class="cd-rows" id="cam-list"></div><button type="button" class="ghost cd-more" id="cam-more" hidden>Load more</button></section><section id="view-admin" hidden><div class="adm-head"><div class="adm-head-copy"><h1>Cameras</h1><p class="muted sm" id="adm-intro">One place per camera: name, low-res and HD streams, detection, masks, access and audio. Live tiles always use the low-res stream; HD is for downloads and exports.</p></div><button type="button" class="adm-btn primary" id="adm-add" hidden>Add camera</button></div><div class="adm-banner" id="adm-banner" role="status" hidden></div><div class="adm-list" id="adm-list"><p class="muted sm">Loading…</p></div></section><section id="view-admin-edit" hidden><div class="cam-head"><button type="button" class="ghost cam-back" id="adm-back">← Cameras</button><h1 id="adm-title"></h1></div><form class="adm-form" id="adm-form" novalidate><fieldset class="adm-section" id="sec-name"><legend>Name</legend><label class="adm-field" id="f-id-row">ID<input id="f-id" autocomplete="off" spellcheck="false" maxlength="64" aria-describedby="f-id-help"></label><p class="muted sm" id="f-id-help">Lowercase letters, numbers and dashes. It cannot be changed later: recordings and Home Assistant use it.</p><label class="adm-field">Display name<input id="f-name" maxlength="128" autocomplete="off"></label><label class="adm-check"><input type="checkbox" id="f-enabled">Camera on</label></fieldset><fieldset class="adm-section" id="sec-streams"><legend>Streams</legend><p class="muted sm">Low-res is for live view and detection. HD is for downloads and exports only. Leave one empty if the camera has a single stream.</p><label class="adm-field">Low-res stream (sub)<input id="f-sub" list="f-go2rtc" autocomplete="off" spellcheck="false"></label><label class="adm-field">HD stream (main)<input id="f-main" list="f-go2rtc" autocomplete="off" spellcheck="false"></label><label class="adm-field">Snapshot stream (optional)<input id="f-snap" list="f-go2rtc" autocomplete="off" spellcheck="false"></label><datalist id="f-go2rtc"></datalist><p class="muted sm" id="f-go2rtc-msg"></p><details class="adm-advanced" id="f-advanced"><summary>Advanced: which stream does what</summary><div class="adm-use" id="f-use"></div><button type="button" class="adm-btn" id="f-defaults">Use defaults</button><p class="muted sm">Defaults: low-res for live view and detection, HD for recording, HD live and checking detections.</p></details></fieldset><fieldset class="adm-section" id="sec-detection"><legend>Detection</legend><label class="adm-check"><input type="checkbox" id="f-detect">Detect people, cars and animals</label><p class="muted sm" id="f-detect-status"></p></fieldset><fieldset class="adm-section" id="sec-masks"><legend>Masks</legend><p class="muted sm" id="f-masks-row"><a id="f-masks" href="#">Open mask and zone editor</a> for this camera. Masks are saved by the detection service.</p><p class="muted sm" id="f-masks-none" hidden>Add detection first to edit masks.</p></fieldset><fieldset class="adm-section" id="sec-access"><legend>Access</legend><label class="adm-check"><input type="radio" name="f-vis" id="f-vis-everyone" value="everyone">Everyone with the Cameras app</label><label class="adm-check"><input type="radio" name="f-vis" id="f-vis-restricted" value="restricted">Only these people and roles</label><div id="f-who"><label class="adm-field">People<input id="f-users" autocomplete="off" spellcheck="false" placeholder="e.g. ann, bob"></label><label class="adm-field">Roles<input id="f-roles" autocomplete="off" spellcheck="false" placeholder="e.g. family"></label><p class="muted sm">Separate names with commas. Admins always see every camera; with nobody listed, only admins do.</p></div></fieldset><fieldset class="adm-section" id="sec-recording"><legend>Recording</legend><label class="adm-field">HD retention, days<input id="f-main-days" type="number" min="0.1" step="0.1" inputmode="decimal" placeholder="Default"></label><label class="adm-field">Low-res retention, days<input id="f-sub-days" type="number" min="0.1" step="0.1" inputmode="decimal" placeholder="Default"></label></fieldset><div id="f-audio-panel" class="adm-audio-host" aria-live="polite"></div><p class="adm-msg" id="adm-msg" role="status"></p><div class="adm-actions"><button type="submit" class="adm-btn primary" id="adm-save">Save camera</button><button type="button" class="adm-btn" id="adm-cancel">Cancel</button><button type="button" class="adm-btn danger" id="adm-delete">Delete camera</button></div></form><h2 class="cam-sub" id="adm-history-title">History</h2><ol class="adm-history" id="adm-history"></ol></section></main>
<div class="ap-menu" id="ap-menu" role="menu" aria-label="Pause phone alerts" hidden><button type="button" role="menuitem" data-d="1h">1 h</button><button type="button" role="menuitem" data-d="3h">3 h</button><button type="button" role="menuitem" data-d="morning">Until morning</button><button type="button" role="menuitem" data-d="resume">Resume</button></div><output class="ap-toast" id="ap-toast" role="status" hidden></output>
<div id="cd-lb" class="cd-lb" role="dialog" aria-modal="true" aria-label="Detection clip" hidden><button type="button" class="cd-lb-x" aria-label="Close">\u00d7</button><div class="cd-lb-info"></div><div class="cd-lb-media"><video playsinline controls preload="metadata" hidden></video><img alt="" hidden></div><div class="cd-lb-msg" aria-live="polite"></div><div class="cd-lb-acts"><button type="button" class="cd-lb-hd" aria-pressed="false" aria-label="Show high-resolution snapshot" hidden>HD</button><a class="cd-dl-snap" href="#" download hidden>Download snapshot</a><a class="cd-dl-clip" href="#" download hidden>Download clip</a></div><div class="cd-lb-note"></div></div>
<script src="/cams/static/hls.min.js?v=1.7.4" defer></script>
<script src="/cams/static/camera_audio.js?v=3" defer></script>
<script src="/cams/static/cams.js?v=23" defer></script>`, { app: 'cams', name: 'Cameras' }).replace('</head>', '<link rel="stylesheet" href="/cams/static/cams.css?v=18"><link rel="manifest" href="/cams/manifest.webmanifest"><link rel="apple-touch-icon" href="/cams/static/apple-touch-icon.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Cameras"><meta name="mobile-web-app-capable" content="yes"><meta name="theme-color" content="#1a5fb4"></head>');
  }
  const STAT = {
    'hls.min.js': 'application/javascript; charset=utf-8',
    'cams.js': 'application/javascript; charset=utf-8',
    'camera_audio.js': 'application/javascript; charset=utf-8',
    'cams.css': 'text/css; charset=utf-8',
    'sw.js': 'application/javascript; charset=utf-8',
    'manifest.webmanifest': 'application/manifest+json; charset=utf-8',
    'icon-192.png': 'image/png',
    'icon-512.png': 'image/png',
    'apple-touch-icon.png': 'image/png',
  };
  const STATIC_RE = /^\/cams\/static\/(cams\.js|camera_audio\.js|cams\.css|hls\.min\.js|sw\.js|manifest\.webmanifest|icon-192\.png|icon-512\.png|apple-touch-icon\.png)$/;
  function handle(req, res, s, p, u) {
    if (!s || typeof s.user !== 'string' || !s.user) return notFound(res);
    if (req.method !== 'GET' && req.method !== 'HEAD') return send(res, 405, 'method not allowed', 'text/plain');
    if (p === '/cams') return res.writeHead(302, { Location: '/cams/', 'Cache-Control': 'no-store' }), res.end();
    if (p === '/cams/') return send(res, 200, html(s), 'text/html; charset=utf-8', { 'Content-Security-Policy': CSP });
    // Service worker must be served from /cams/ (not /cams/static/) so its default scope covers the PWA.
    if (p === '/cams/sw.js') {
      return fs.readFile(path.join(__dirname, 'static', 'sw.js'), (e, d) => e ? notFound(res) : send(res, 200, d, STAT['sw.js'], {
        'Cache-Control': 'no-store',
        'Service-Worker-Allowed': '/cams/',
      }));
    }
    if (p === '/cams/manifest.webmanifest') {
      return fs.readFile(path.join(__dirname, 'static', 'manifest.webmanifest'), (e, d) => e ? notFound(res) : send(res, 200, d, STAT['manifest.webmanifest'], { 'Cache-Control': 'private, max-age=3600' }));
    }
    let m;
    if ((m = p.match(STATIC_RE))) {
      const name = m[1];
      const cache = name === 'hls.min.js' ? 'private, max-age=86400' : (name.endsWith('.png') ? 'private, max-age=86400' : 'private, max-age=3600');
      return fs.readFile(path.join(__dirname, 'static', name), (e, d) => e ? notFound(res) : send(res, 200, d, STAT[name], { 'Cache-Control': cache }));
    }
    if (p === '/cams/api/list') return send(res, 200, JSON.stringify(access.list(s.user).map(({ id, name }) => ({ id, name }))), 'application/json');
    if ((m = p.match(/^\/cams\/api\/snap\/([^/]+)$/))) {
      const cam = access.get(s.user, m[1]), src = cam && (cam.snap || cam.id);
      if (cam && access.canStream(s.user, src)) return snap(res, cam.id, src);
    }
    if ((m = p.match(/^\/cams\/api\/stream\/([^/]+)\/(.+)$/)) && access.get(s.user, m[1]) && access.canStream(s.user, m[1])) {
      const id = m[1], rest = m[2], still = () => access.canStream(s.user, id);
      if (rest === 'index.m3u8') return hlsIndex(res, id);
      if (rest === 'live.mp4') return upstream(res, `/api/stream.mp4?src=${id}`, 'video/mp4', 0, still);
      const hm = rest.match(/^hls\/(playlist\.m3u8|segment\.m4s|segment\.ts|init\.mp4)$/), sid = u.searchParams.get('id') || '', n = u.searchParams.get('n'), owner = sids.get(sid);
      if (hm && owner && owner.cam === id && (n == null || /^\d{1,9}$/.test(n))) {
        owner.t = Date.now();
        const gp = `/api/hls/${hm[1]}?id=${sid}${n != null ? '&n=' + n : ''}`;
        return hm[1] === 'playlist.m3u8' ? upstream(res, gp, 'application/vnd.apple.mpegurl') : seg(res, gp, hm[1] === 'segment.ts' ? 'video/mp2t' : 'video/mp4');
      }
    }
    return notFound(res);
  }
  return { handle };
};
