'use strict';
// Cameras page (/cams/). Access is enforced by server.js (app "cams" ACL, 404 otherwise) before handle() runs.
// Live view: MPEG-TS HLS (go2rtc's fMP4 HLS segments fail native HLS demuxers) for Safari/iOS, fMP4 progressive for desktop.
// The browser only sees opaque camera ids; RTSP URLs/credentials live in /volume1/docker/cams/go2rtc.yaml (root 600).
const http = require('http'), fs = require('fs'), path = require('path');
const G = { host: '127.0.0.1', port: 1984 };
const CFG = path.join(require('./lib').DIR, 'cams.json');
const ID = /^[a-z0-9][a-z0-9-]{0,31}$/;
module.exports = function (C) {
  const { esc, send, page, notFound } = C;
  let cams = [];
  const load = () => { try { cams = JSON.parse(fs.readFileSync(CFG, 'utf8')).filter(c => c && ID.test(c.id) && c.name && (!c.snap || ID.test(c.snap))); } catch (e) { console.error('cams.json:', e.message); } };
  load(); setInterval(load, 30e3).unref();
  const CSP = "default-src 'self'; worker-src 'self'; manifest-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";
  const snaps = new Map(); // id -> {t, buf, waiters}
  function upstream(res, gpath, type, timeout) {
    const up = http.get({ ...G, path: gpath }, ur => {
      if (ur.statusCode !== 200) { ur.resume(); return send(res, 502, 'camera unavailable', 'text/plain'); }
      res.writeHead(200, { 'Content-Type': type || ur.headers['content-type'] || 'application/octet-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'", 'x-accel-buffering': 'no' });
      ur.pipe(res);
    });
    up.setTimeout(timeout || 30e3, () => up.destroy(new Error('timeout')));
    up.on('error', () => { if (!res.headersSent) send(res, 502, 'camera unavailable', 'text/plain'); else res.destroy(); });
    res.on('close', () => up.destroy());
  }
  function snap(res, id) {
    const c = snaps.get(id) || {}; snaps.set(id, c);
    const out = b => b ? send(res, 200, b, 'image/jpeg') : send(res, 502, 'camera unavailable', 'text/plain');
    if (c.buf && Date.now() - c.t < 3000) return out(c.buf);
    if (c.waiters) return c.waiters.push(out);
    c.waiters = [out];
    const done = b => { const w = c.waiters; c.waiters = null; if (b) { c.buf = b; c.t = Date.now(); } w.forEach(f => f(b || null)); };
    const up = http.get({ ...G, path: '/api/frame.jpeg?src=' + ((cams.find(x => x.id === id) || {}).snap || id) }, ur => { const a = []; ur.on('data', d => a.push(d)); ur.on('end', () => done(ur.statusCode === 200 ? Buffer.concat(a) : null)); });
    up.setTimeout(15e3, () => up.destroy(new Error('timeout'))); up.on('error', () => done(null));
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
    return page('Cameras · MERSINO NETWORK', `<nav class="mn-subnav cams-tabs" id="cams-tabs" aria-label="Sections"><a class="cams-tab" data-view="grid" href="#">Live</a><a class="cams-tab" data-view="det" href="#detections">Detections</a></nav>
<main class="wrap"><section id="view-grid"><p class="muted sm">Snapshots refresh every few seconds. Tap a camera to open it.</p><div class="camgrid" id="camgrid">${cams.map(c => `<button type="button" class="camtile" data-id="${esc(c.id)}" data-cam="${esc(c.id)}" data-name="${esc(c.name)}"><img alt="${esc(c.name)} snapshot" src="/cams/api/snap/${esc(c.id)}"><span class="camname">${esc(c.name)}</span><span class="camage muted sm"></span></button>`).join('') || '<p class="muted">No cameras configured.</p>'}</div></section>
<section id="view-det" hidden><div class="cd-filters"><select id="det-cam" aria-label="Camera"><option value="">All cameras</option></select><select id="det-label" aria-label="Label"><option value="">All labels</option><option value="person">Person</option><option value="car">Car</option><option value="dog">Dog</option><option value="cat">Cat</option></select></div><div class="cd-rows" id="det-list"></div><button type="button" class="ghost cd-more" id="det-more" hidden>Load more</button></section>
<section id="view-cam" hidden><div class="cam-head"><button type="button" class="ghost cam-back" id="cam-back">← Cameras</button><h1 id="cam-name"></h1></div><div class="cam-player" id="cam-player"><video id="cam-video" playsinline muted autoplay controls></video><button type="button" id="cam-retry" hidden>Couldn't load this recording. Tap to retry.</button><output id="cam-mode" class="cam-mode live">LIVE</output><output id="cam-toast" hidden></output><button type="button" id="cam-live" hidden>LIVE</button><button type="button" id="cam-unmute" hidden>Tap for sound</button><button type="button" class="cam-fs" id="cam-fs" aria-label="Full screen">⛶</button></div><p class="muted sm" id="cam-msg" role="status"></p><section id="timeline" aria-label="Recording history"><canvas id="tl-bar" tabindex="0" aria-label="Recording history: drag to scrub, pinch to zoom"></canvas><div class="tl-controls"><button type="button" class="tl-icon" id="tl-back" aria-label="Back 10 seconds" title="Back 10 seconds"></button><button type="button" class="tl-icon" id="tl-play" aria-label="Play or pause" title="Play or pause"></button><button type="button" class="tl-icon" id="tl-forward" aria-label="Forward 10 seconds" title="Forward 10 seconds"></button><button type="button" class="tl-icon tl-det" id="tl-det-prev" aria-label="Previous detection" title="Previous detection"><span class="tl-cap">Prev event</span></button><button type="button" class="tl-icon tl-det" id="tl-det-next" aria-label="Next detection" title="Next detection"><span class="tl-cap">Next event</span></button><div class="tl-zoom"><button type="button" class="tl-icon" id="tl-out" aria-label="Zoom out" title="Zoom out"></button><button type="button" class="tl-icon" id="tl-in" aria-label="Zoom in" title="Zoom in"></button></div><button type="button" class="tl-icon" id="tl-calendar" aria-label="Choose date" title="Choose date"></button><button type="button" class="tl-icon" id="tl-select" aria-label="Download range" title="Download range" aria-pressed="false"></button><input id="tl-date" type="date" aria-label="Recording date" tabindex="-1"></div><p id="tl-msg" role="status"></p><div id="tl-range" hidden><button type="button" class="tl-icon tl-cancel" id="tl-cancel" aria-label="Cancel download" title="Cancel download"></button><div class="tl-times"><label>Start<input id="tl-start" type="time" step="1"></label><label>End<input id="tl-end" type="time" step="1"></label><output id="tl-duration"></output></div><p id="tl-export-msg" role="status"></p><a id="tl-download" download hidden>Download</a></div></section><h2 class="cam-sub">Detections</h2><p class="muted" id="cam-off" hidden>Detection is off for this camera.</p><div class="cd-rows" id="cam-list"></div><button type="button" class="ghost cd-more" id="cam-more" hidden>Load more</button></section></main>
<div id="cd-lb" class="cd-lb" role="dialog" aria-modal="true" aria-label="Detection clip" hidden><button type="button" class="cd-lb-x" aria-label="Close">\u00d7</button><div class="cd-lb-info"></div><div class="cd-lb-media"><video playsinline controls preload="metadata" hidden></video><img alt="" hidden></div><div class="cd-lb-msg" aria-live="polite"></div><div class="cd-lb-acts"><button type="button" class="cd-lb-hd" aria-pressed="false" aria-label="Show high-resolution snapshot" hidden>HD</button><a class="cd-dl-snap" href="#" download hidden>Download snapshot</a><a class="cd-dl-clip" href="#" download hidden>Download clip</a></div><div class="cd-lb-note"></div></div>
<script src="/cams/static/hls.min.js?v=1.7.4" defer></script>
<script src="/cams/static/cams.js?v=15" defer></script>`, { app: 'cams', name: 'Cameras' }).replace('</head>', '<link rel="stylesheet" href="/cams/static/cams.css?v=11"></head>');
  }
  const STAT = { 'hls.min.js': 'application/javascript; charset=utf-8', 'cams.js': 'application/javascript; charset=utf-8', 'cams.css': 'text/css; charset=utf-8' };
  function handle(req, res, s, p, u) {
    if (req.method !== 'GET' && req.method !== 'HEAD') return send(res, 405, 'method not allowed', 'text/plain');
    if (p === '/cams') return res.writeHead(302, { Location: '/cams/', 'Cache-Control': 'no-store' }), res.end();
    if (p === '/cams/') return send(res, 200, html(s), 'text/html; charset=utf-8', { 'Content-Security-Policy': CSP });
    let m;
    if ((m = p.match(/^\/cams\/static\/(cams\.js|cams\.css|hls\.min\.js)$/))) return fs.readFile(path.join(__dirname, 'static', m[1]), (e, d) => e ? notFound(res) : send(res, 200, d, STAT[m[1]], { 'Cache-Control': m[1] === 'hls.min.js' ? 'private, max-age=86400' : 'private, max-age=3600' }));
    if (p === '/cams/api/list') return send(res, 200, JSON.stringify(cams.map(({ id, name }) => ({ id, name }))), 'application/json');
    const known = id => cams.some(c => c.id === id);
    if ((m = p.match(/^\/cams\/api\/snap\/([^/]+)$/)) && known(m[1])) return snap(res, m[1]);
    if ((m = p.match(/^\/cams\/api\/stream\/([^/]+)\/(.+)$/)) && known(m[1])) {
      const id = m[1], rest = m[2];
      if (rest === 'index.m3u8') return upstream(res, `/api/stream.m3u8?src=${id}`, 'application/vnd.apple.mpegurl');
      if (rest === 'live.mp4') return upstream(res, `/api/stream.mp4?src=${id}`, 'video/mp4', 0);
      const hm = rest.match(/^hls\/(playlist\.m3u8|segment\.m4s|segment\.ts|init\.mp4)$/), sid = u.searchParams.get('id') || '', n = u.searchParams.get('n');
      if (hm && /^[A-Za-z0-9_-]{1,64}$/.test(sid) && (n == null || /^\d{1,9}$/.test(n))) {
        const gp = `/api/hls/${hm[1]}?id=${sid}${n != null ? '&n=' + n : ''}`;
        return hm[1] === 'playlist.m3u8' ? upstream(res, gp, 'application/vnd.apple.mpegurl') : seg(res, gp, hm[1] === 'segment.ts' ? 'video/mp2t' : 'video/mp4');
      }
    }
    return notFound(res);
  }
  return { handle };
};
