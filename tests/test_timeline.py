import json
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
    assert [e['id'] for e in p['segments']] == [1, 3]
    assert p['segments'][1]['offset'] == 60
    assert p['playlist'].startswith('hls/cam/')
    r = client.get('/api/hls/cam/index.m3u8?start=0&end=200', headers=HEADERS)
    assert r.status_code == 200
    assert '#EXTINF:60.000,\nseg/1.ts' in r.text
    assert '#EXT-X-DISCONTINUITY' in r.text
    assert r.text.endswith('#EXT-X-ENDLIST\n')
    assert r.headers['cache-control'] == 'no-store'
    db.segment_rows.return_value = []
    assert client.get('/api/hls/cam/index.m3u8?start=0&end=200', headers=HEADERS).status_code == 404


def test_day_codec_discontinuity():
    from cams_record.media import playlist
    entries = playback_entries([seg(86340, 86400) | {'id': 1}, seg(86400, 86460) | {'id': 2},
                                seg(86460, 86520) | {'id': 3, 'codec': 'h264'}], 86340, 86520)
    assert playlist(entries, 2).count('#EXT-X-DISCONTINUITY') == 2


@needs_ffmpeg
def test_ts_cache_and_audio(setup, monkeypatch):
    cfg, db, client = setup
    source = Path(cfg['recording']['root']) / 'a.mp4'
    make_clip(source, 3)
    row = rows()[0] | {'codec': 'h264', 'end_ts': 3}
    db.segment_by_id.return_value = row
    assert client.get('/api/hls/wrong/seg/1.ts', headers=HEADERS).status_code == 404
    db.segment_by_id.return_value = row | {'quality': 'main'}
    assert client.get('/api/hls/cam/seg/1.ts', headers=HEADERS).status_code == 404
    db.segment_by_id.return_value = row
    response = client.get('/api/hls/cam/seg/1.ts', headers=HEADERS)
    assert response.status_code == 200
    output = source.parent / 'probe.ts'
    output.write_bytes(response.content)
    info = json.loads(subprocess.run(
        ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output)],
        capture_output=True, check=True).stdout)
    assert {s['codec_type'] for s in info['streams']} == {'video', 'audio'}
    monkeypatch.setattr(Media, 'run', lambda *a: pytest.fail('cache missed'))
    assert client.get('/api/hls/cam/seg/1.ts', headers=HEADERS).content == response.content


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
        assert client.get('/api/hls/cam/seg/1.ts', headers=HEADERS).status_code == 404


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

    asyncio.run(disconnect_request(client.app, '/api/hls/cam/seg/1.ts', during_send))
    assert not list(media.hls.glob('*.lease'))
    assert list(media.hls.glob('*.ts'))
    assert client.get('/api/hls/cam/seg/1.ts', headers=HEADERS).status_code == 200
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
    assert client.get('/api/hls/cam/seg/1.ts', headers=HEADERS).status_code == 200
    assert not stale_lease.exists()
    assert fresh_lease.exists()
    stale_lease.write_bytes(b'lease')
    os.utime(stale_lease, (time.time() - 3601,) * 2)
    monkeypatch.setattr(Media, 'run', lambda *a: pytest.fail('cache missed'))
    assert client.get('/api/hls/cam/seg/1.ts', headers=HEADERS).status_code == 200
    assert not stale_lease.exists()
