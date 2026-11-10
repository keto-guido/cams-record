'use strict';
// Present cams_config camera objects as one camera with low-res (sub) and HD (main) stream
// properties. HD go2rtc names (*-main, *-raw used as main) are never separate cameras.
// Used by the Admin UI and by tests; cams-detect should apply the same rule when listing cameras.

const HD_SUFFIX_RE = /-(?:main|raw)$/;

function streamName(cam, key) {
  const s = cam && cam.streams && cam.streams[key];
  return s && typeof s.go2rtc_name === 'string' && s.go2rtc_name ? s.go2rtc_name : null;
}

/** One camera as the Admin UI expects it: sub/hd as properties, never orphan HD rows. */
function presentCamera(cam) {
  if (!cam || typeof cam !== 'object' || typeof cam.id !== 'string') return null;
  const sub = streamName(cam, 'sub');
  const hd = streamName(cam, 'main');
  const snap = streamName(cam, 'snap');
  return {
    ...cam,
    streams_view: {
      sub, // low-res: live tiles and detection
      hd, // high-res: downloads / exports / verify
      snap,
      labels: {
        sub: sub || '—',
        hd: hd || '—',
        snap: snap || '—',
      },
    },
  };
}

/** Stable list for the Admin camera list: one row per cams_config camera, sorted. */
function presentCameras(cameras) {
  const list = Array.isArray(cameras) ? cameras : [];
  return list.map(presentCamera).filter(Boolean)
    .sort((a, b) => (a.sort_order - b.sort_order) || a.id.localeCompare(b.id));
}

/**
 * True when a go2rtc name looks like an HD helper that already belongs to a configured camera.
 * Used to explain why detect UIs must not list these as "not a camera yet".
 */
function isOwnedHdStream(name, cameras) {
  if (typeof name !== 'string' || !name) return false;
  const list = Array.isArray(cameras) ? cameras : [];
  for (const cam of list) {
    if (streamName(cam, 'main') === name) return true;
    if (streamName(cam, 'snap') === name) return true;
    // back-yard records sub as back-yard-raw: that alias is still this camera, not a new one.
    if (streamName(cam, 'sub') === name && name !== cam.id) return true;
  }
  if (!HD_SUFFIX_RE.test(name)) return false;
  const base = name.replace(HD_SUFFIX_RE, '');
  return list.some(c => c.id === base);
}

module.exports = { presentCamera, presentCameras, isOwnedHdStream, streamName, HD_SUFFIX_RE };
