import json
import re
import subprocess
from pathlib import Path

import pytest

from cams_record.clips import merge_spans
from cams_record.media import Media, playback_entries
from tests.conftest import make_clip, needs_ffmpeg
from tests.test_clips import seg
from tests.test_clips import setup as clip_setup
from tests.test_web import tok

setup = clip_setup

HEADERS = {'x-apphub-token': tok(['cams'])}


def rows():
    return [seg(0, 60) | {'id': 1, 'camera_id': 'cam', 'quality': 'sub'},
            seg(50, 100) | {'id': 2}, seg(120, 180) | {'id': 3}]


def test_merge():
    assert merge_spans([seg(), seg(62, 120), seg(150, 180), seg(180, 200, missing=1)],
                       10, 170) == [[10, 120], [150, 170]]
    assert merge_spans([], 0, 100) == []
    assert merge_spans([seg(), seg(10, 20)], 0, 100) == [[0, 60]]


def test_timeline_and_playlist(setup):
    cfg, db, client = setup
    db.segment_rows.return_value = rows()
    url = '/cams-record/api/timeline?cam=cam&start=0&end=200'
    assert client.get(url).status_code == 401
    assert client.get(url, headers={'x-apphub-token': tok(['other'])}).status_code == 403
    j = client.get(url, headers=HEADERS).json()
    assert j['spans']['sub'] == [[0, 100], [120, 180]]
    assert client.get(url.replace('cam=cam', 'camera=cam'), headers=HEADERS).json() == j
    for end in [0, 8*86400+1]:
        assert client.get('/api/timeline', params=dict(cam='cam', start=0, end=end), headers=HEADERS).status_code == 400
    assert client.get('/api/timeline?cam=cam', headers=HEADERS).status_code == 200
    p = client.get('/api/playback?cam=cam&start=0&end=200', headers=HEADERS).json()
    assert [e['id'] for e in p['segments']] == [1, 2, 3]
    assert p['segments'][2]['offset'] == 110
    assert p['playlist'].startswith('hls/cam/')
    r = client.get('/api/hls/cam/index.m3u8?start=0&end=200', headers=HEADERS)
    assert r.status_code == 200
    assert '#EXTINF:60.000,\nseg/1.ts?o=0\n' in r.text
    assert re.findall(r'seg/(\d+)\.ts\?o=(\d+)', r.text) == [
        (str(e['id']), str(round(e['offset'] * 1000))) for e in p['segments']]
    assert 'seg/3.ts?o=110000\n' in r.text
    assert '#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:02:00.000Z' in r.text
    assert '#EXT-X-DISCONTINUITY' not in r.text  # 100..120 is a recording gap.
    assert r.text.endswith('#EXT-X-ENDLIST\n')
    assert r.headers['cache-control'] == 'no-store'
    db.segment_rows.return_value = []
    assert client.get('/api/hls/cam/index.m3u8?start=0&end=200', headers=HEADERS).status_code == 404


def test_discontinuity_only_on_codec_change():
    from cams_record.media import playlist
    entries = playback_entries([seg(86340, 86400) | {'id': 1}, seg(86400, 86460) | {'id': 2},
                                seg(86500, 86520) | {'id': 3, 'codec': 'h264'}], 86340, 86520)
    text = playlist(entries)
    assert text.count('#EXT-X-DISCONTINUITY') == 1
    assert text.index('seg/2.ts?o=60000') < text.index('#EXT-X-DISCONTINUITY') < text.index('seg/3.ts?o=120000')


@pytest.mark.parametrize('timestamps,tolerance', [
    ([(1793849883.0, 1793849943.0),
      (1793849941.999, 1793850001.999),
      (1793850001.998, 1793850061.998)], 2.0),
    ([(1793846425.0, 1793846466.133),
      (1793846461.999, 1793846521.999),
      (1793846521.998, 1793846581.998)], 5.0),
])
def test_playback_boundary_overlap(setup, timestamps, tolerance):
    cfg, db, client = setup
    cfg['clips']['gap_tolerance_s'] = tolerance
    db.segment_rows.return_value = [seg(a, b) | {'id': i}
                                    for i, (a, b) in enumerate(timestamps, 1)]
    params = dict(cam='cam', start=timestamps[0][0], end=timestamps[-1][1])
    response = client.get('/api/playback', params=params, headers=HEADERS)
    assert response.status_code == 200
    entries = response.json()['segments']
    assert [e['id'] for e in entries] == [1, 2, 3]
    durations = [b - a for a, b in timestamps]
    assert [e['duration'] for e in entries] == pytest.approx(durations)
    assert [e['offset'] for e in entries] == pytest.approx([0, durations[0], sum(durations[:2])])
    assert all(a['end'] - b['start'] <= tolerance for a, b in zip(entries, entries[1:], strict=False))
    response = client.get('/api/hls/cam/index.m3u8', params=params, headers=HEADERS)
    assert response.status_code == 200
    assert response.text.count('#EXTINF:') == 3
    assert '#EXT-X-DISCONTINUITY' not in response.text
    assert re.findall(r'\?o=(\d+)', response.text) == [str(round(e['offset'] * 1000)) for e in entries]


def test_playback_duplicates_and_overlap_limit():
    source = [seg(1, 60.970), seg(1.999, 61.969),
              seg(61, 120.970), seg(61.999, 121.969),
              seg(120, 120.9700005), seg(120.5, 180), seg(121, 181)]
    entries = playback_entries([r | {'id': i} for i, r in enumerate(source)], 10, 200)
    assert [e['id'] for e in entries] == [1, 2, 5]
    from cams_record.media import playlist
    entries = playback_entries([
        seg(1793846425.0, 1793846466.133) | {'id': 2629},
        seg(1793846461.999, 1793846521.973) | {'id': 2639},
        seg(1793846521.973, 1793846581.973) | {'id': 2640}],
        1793846425.0, 1793846581.973)
    assert [e['id'] for e in entries] == [2629, 2639, 2640]
    assert [e['duration'] for e in entries] == pytest.approx([41.133, 59.974, 60])
    text = playlist(entries)
    assert '#EXT-X-DISCONTINUITY' not in text
    assert text.index('seg/2629.ts?o=0') < text.index('seg/2639.ts?o=41133') < text.index('seg/2640.ts?o=101107')



@needs_ffmpeg
def test_ts_cache_and_audio(setup, monkeypatch):
    cfg, db, client = setup
    source = Path(cfg['recording']['root']) / 'a.mp4'
    make_clip(source, 3)
    row = rows()[0] | {'codec': 'h264', 'end_ts': 3}
    db.segment_by_id.return_value = row
    assert client.get('/api/hls/wrong/seg/1.ts?o=0', headers=HEADERS).status_code == 404
    db.segment_by_id.return_value = row | {'quality': 'main'}
    assert client.get('/api/hls/cam/seg/1.ts?o=0', headers=HEADERS).status_code == 404
    db.segment_by_id.return_value = row
    response = client.get('/api/hls/cam/seg/1.ts?o=0', headers=HEADERS)
    assert response.status_code == 200
    output = source.parent / 'probe.ts'
    output.write_bytes(response.content)
    info = json.loads(subprocess.run(
        ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output)],
        capture_output=True, check=True).stdout)
    assert {s['codec_type'] for s in info['streams']} == {'video', 'audio'}
    monkeypatch.setattr(Media, 'run', lambda *a: pytest.fail('cache missed'))
    assert client.get('/api/hls/cam/seg/1.ts?o=0', headers=HEADERS).content == response.content


def ffprobe_json(path, *args):
    return json.loads(subprocess.run(['ffprobe', '-v', 'error', *args, '-of', 'json', str(path)],
                                     capture_output=True, check=True).stdout)


@pytest.mark.parametrize('value', ['', 'abc', '-1', '1.5', '+5', '1_000', '108000001', '9' * 10, '\u0663'])
def test_ts_offset_invalid(setup, value):
    cfg, db, client = setup
    assert client.get('/api/hls/cam/seg/1.ts', params={'o': value}, headers=HEADERS).status_code == 400


def test_ts_offset_required(setup):
    cfg, db, client = setup
    assert client.get('/api/hls/cam/seg/1.ts', headers=HEADERS).status_code == 400


@needs_ffmpeg
def test_ts_timestamps_follow_offset(setup):
    cfg, db, client = setup
    source = Path(cfg['recording']['root']) / 'a.mp4'
    make_clip(source, 3)
    # A start time unrelated to the offset: the UTC time of day must not influence timestamps.
    db.segment_by_id.return_value = rows()[0] | {'start_ts': 1793849883.0, 'end_ts': 1793849886.0}
    media = client.app.app.state.media
    for count, offset_ms in enumerate((0, 16665250), 1):
        response = client.get(f'/api/hls/cam/seg/1.ts?o={offset_ms}', headers=HEADERS)
        assert response.status_code == 200
        output = source.parent / f'probe{offset_ms}.ts'
        output.write_bytes(response.content)
        streams = ffprobe_json(output, '-show_streams')['streams']
        assert {s['codec_type'] for s in streams} == {'video', 'audio'}
        frame = ffprobe_json(output, '-select_streams', 'v:0', '-show_frames',
                             '-read_intervals', '%+#1')['frames'][0]
        assert float(frame['pts_time']) == pytest.approx(offset_ms / 1000 + 10, abs=0.1)
        assert frame['key_frame'] == 1
        assert len(list(media.hls.glob('*.ts'))) == count


def test_export_limits_fallback_cleanup_busy(setup, monkeypatch):
    cfg, db, client = setup
    db.segment_rows.return_value = [rows()[0]]
    db.clip_segments.side_effect = lambda cam, quality, start, end: (
        ([seg(deleted_at=True)], 60) if quality == 'main' else ([seg()], 60))
    def run(self, command, timeout):
        Path(command[-1]).write_bytes(b'export')
        return True
    monkeypatch.setattr(Media, 'run', run)
    assert client.get('/api/export?cam=cam&start=0&end=1801', headers=HEADERS).status_code == 400
    assert client.get('/api/clip?camera=cam&start=0&end=301', headers=HEADERS).status_code == 400
    r = client.get('/api/export?cam=cam&start=10&end=40', headers=HEADERS)
    assert r.status_code == 200
    assert r.headers['x-export-quality'] == 'sub'
    assert 'attachment' in r.headers['content-disposition']
    assert not list((Path(cfg['recording']['state_dir']) / 'exports').glob('*.mp4'))
    media = client.app.app.state.media
    media.export_slot.acquire()
    try:
        assert client.get('/api/export/info?cam=cam&start=10&end=40', headers=HEADERS).json()['busy']
        r = client.get('/api/export?cam=cam&start=10&end=40', headers=HEADERS)
        assert r.status_code == 429 and r.headers['retry-after'] == '15'
    finally:
        media.export_slot.release()


def test_playback_limits_and_export_not_ready(setup):
    cfg, db, client = setup
    db.segment_rows.return_value = []
    assert client.get('/api/playback?cam=cam&start=0&end=93601', headers=HEADERS).status_code == 400
    db.clip_segments.return_value = ([], None)
    assert client.get('/api/export?cam=cam&start=0&end=1800', headers=HEADERS).status_code == 425
    for row in [rows()[0] | {'missing': True}, rows()[0] | {'deleted_at': True},
                rows()[0] | {'path': '../escape.mp4'}]:
        db.segment_by_id.return_value = row
        assert client.get('/api/hls/cam/seg/1.ts?o=0', headers=HEADERS).status_code == 404


async def disconnect_request(app, url, on_body):
    path, _, query = url.partition('?')
    scope = dict(type='http', asgi={'version': '3.0', 'spec_version': '2.4'}, http_version='1.1', method='GET',
                 scheme='http', path=path, raw_path=path.encode(), query_string=query.encode(),
                 root_path='', headers=[(k.encode(), v.encode()) for k, v in HEADERS.items()],
                 server=('testserver', 80), client=('testclient', 123))

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        if message['type'] == 'http.response.start':
            assert message['status'] == 200
        if message['type'] == 'http.response.body':
            on_body()
            raise OSError('client disconnected')

    with pytest.raises(OSError, match='client disconnected'):
        await app(scope, receive, send)


def test_export_disconnect_cleanup(setup, monkeypatch):
    import asyncio

    cfg, db, client = setup
    db.segment_rows.return_value = [rows()[0] | {'size': 7}]
    media = client.app.app.state.media

    def run(self, command, timeout):
        Path(command[-1]).write_bytes(b'export')
        return True

    monkeypatch.setattr(Media, 'run', run)
    url = '/api/export?cam=cam&start=10&end=40'

    def during_send():
        assert list(media.exports.glob('*.mp4'))
        assert not media.export_slot.acquire(blocking=False)

    asyncio.run(disconnect_request(client.app, url, during_send))
    assert not list(media.exports.glob('*.mp4'))
    assert client.get(url, headers=HEADERS).status_code == 200
    assert not list(media.exports.glob('*.mp4'))


def test_ts_disconnect_cleanup(setup, monkeypatch):
    import asyncio

    cfg, db, client = setup
    db.segment_by_id.return_value = rows()[0]
    media = client.app.app.state.media

    def run(self, command, timeout):
        Path(command[-1]).write_bytes(b'transport stream')
        return True

    monkeypatch.setattr(Media, 'run', run)

    def during_send():
        assert list(media.hls.glob('*.lease'))

    asyncio.run(disconnect_request(client.app, '/api/hls/cam/seg/1.ts?o=0', during_send))
    assert not list(media.hls.glob('*.lease'))
    assert list(media.hls.glob('*.ts'))
    assert client.get('/api/hls/cam/seg/1.ts?o=0', headers=HEADERS).status_code == 200
    assert not list(media.hls.glob('*.lease'))


def test_stale_media_cleanup(setup, monkeypatch):
    import os
    import time

    cfg, db, client = setup
    media = client.app.app.state.media
    media.exports.mkdir(parents=True, exist_ok=True)
    old = media.exports / 'old.mp4'
    fresh = media.exports / 'fresh.mp4'
    for path in (old, fresh):
        path.write_bytes(b'export')
    os.utime(old, (time.time() - 7201,) * 2)
    Media(media.clips, cfg)
    assert not old.exists()
    assert fresh.exists()

    old.write_bytes(b'export')
    os.utime(old, (time.time() - 7201,) * 2)
    db.segment_rows.return_value = [rows()[0] | {'size': 7}]
    db.segment_by_id.return_value = rows()[0]

    def run(self, command, timeout):
        Path(command[-1]).write_bytes(b'media')
        return True

    monkeypatch.setattr(Media, 'run', run)
    assert client.get('/api/export?cam=cam&start=10&end=40', headers=HEADERS).status_code == 200
    assert not old.exists()
    assert fresh.exists()

    media.hls.mkdir(parents=True, exist_ok=True)
    stale_lease, fresh_lease = media.hls / 'old.lease', media.hls / 'fresh.lease'
    for path in (stale_lease, fresh_lease):
        path.write_bytes(b'lease')
    os.utime(stale_lease, (time.time() - 3601,) * 2)
    assert client.get('/api/hls/cam/seg/1.ts?o=0', headers=HEADERS).status_code == 200
    assert not stale_lease.exists()
    assert fresh_lease.exists()
    stale_lease.write_bytes(b'lease')
    os.utime(stale_lease, (time.time() - 3601,) * 2)
    monkeypatch.setattr(Media, 'run', lambda *a: pytest.fail('cache missed'))
    assert client.get('/api/hls/cam/seg/1.ts?o=0', headers=HEADERS).status_code == 200
    assert not stale_lease.exists()
