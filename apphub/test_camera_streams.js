'use strict';
// Pure presentation of cams_config cameras: one object with sub/HD properties.
// Run: node apphub/test_camera_streams.js
const assert = require('assert');
const { presentCamera, presentCameras, isOwnedHdStream } = require('./camera_streams');

const front = {
  id: 'front-door', name: 'Front Door', sort_order: 0, enabled: true,
  streams: {
    main: { go2rtc_name: 'front-door-main' },
    sub: { go2rtc_name: 'front-door' },
  },
  use: { live: ['sub'], live_hd: ['main'], detect: ['sub'], record: ['main', 'sub'] },
};
const backYard = {
  id: 'back-yard', name: 'Back Yard', sort_order: 1, enabled: true,
  streams: {
    main: { go2rtc_name: 'back-yard-main' },
    sub: { go2rtc_name: 'back-yard-raw' },
    snap: { go2rtc_name: 'back-yard-raw' },
  },
  use: { live: ['sub'], detect: ['sub'], record: ['main', 'sub'] },
};

const presented = presentCameras([backYard, front]);
assert.deepEqual(presented.map(c => c.id), ['front-door', 'back-yard']);
assert.equal(presented.length, 2);
assert.equal(presented[0].streams_view.sub, 'front-door');
assert.equal(presented[0].streams_view.hd, 'front-door-main');
assert.equal(presented[1].streams_view.sub, 'back-yard-raw');
assert.equal(presented[1].streams_view.hd, 'back-yard-main');

// HD helpers are owned by the parent camera — never separate "not a camera yet" rows.
for (const name of ['front-door-main', 'back-yard-main', 'back-yard-raw', 'front-door-raw']) {
  assert.equal(isOwnedHdStream(name, [front, backYard]), true, name);
}
assert.equal(isOwnedHdStream('garage-main', [front, backYard]), false);
assert.equal(isOwnedHdStream('front-door', [front, backYard]), false);
assert.equal(isOwnedHdStream('mckinley-webcam', [front, backYard]), false);

assert.equal(presentCamera(null), null);
assert.equal(presentCamera({ name: 'x' }), null);
const lone = presentCamera({ id: 'solo', name: 'Solo', streams: { sub: { go2rtc_name: 'solo' } } });
assert.equal(lone.streams_view.hd, null);
assert.equal(lone.streams_view.sub, 'solo');

console.log('camera streams tests passed');
