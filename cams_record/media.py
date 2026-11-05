"""Stream-copy playback and disposable exports."""
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .clips import ClipError, concat_listing, ffmpeg_command
from .retention import UnsafePath, resolve_segment_path


def playback_entries(rows, start, end):
    entries, paths, offset = [], set(), 0
    ordered = sorted(rows, key=lambda r: (r['start_ts'], -r['end_ts']))
    initial = [r for r in ordered if r['start_ts'] <= start < r['end_ts']
               and not r.get('missing') and not r.get('deleted_at')]
    if initial:
        first = max(initial, key=lambda r: (r['end_ts'], r['start_ts']))
        ordered = [first] + [r for r in ordered if r is not first]
    for row in ordered:
        if (row.get('missing') or row.get('deleted_at') or row['path'] in paths
                or row['end_ts'] <= start or row['start_ts'] >= end):
            continue
        if entries and row['start_ts'] < entries[-1]['end']:
            continue
        duration = row['end_ts'] - row['start_ts']
        if duration <= 0:
            continue
        entries.append(dict(id=row['id'], start=row['start_ts'], end=row['end_ts'],
                            offset=offset, duration=duration, codec=row.get('codec')))
        paths.add(row['path'])
        offset += duration
    return entries


def playlist(entries, tolerance):
    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-PLAYLIST-TYPE:VOD',
             f'#EXT-X-TARGETDURATION:{math.ceil(max(e["duration"] for e in entries))}',
             '#EXT-X-MEDIA-SEQUENCE:0']
    previous = None
    for e in entries:
        if previous and (e['start'] - previous['end'] > tolerance
                         or e['codec'] != previous['codec']
                         or int(e['start'] // 86400) != int(previous['start'] // 86400)):
            lines.append('#EXT-X-DISCONTINUITY')
        stamp = datetime.fromtimestamp(e['start'], UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        lines += [f'#EXT-X-PROGRAM-DATE-TIME:{stamp}', f'#EXTINF:{e["duration"]:.3f},', f'seg/{e["id"]}.ts']
        previous = e
    return '\n'.join(lines + ['#EXT-X-ENDLIST', ''])


class Media:
    def __init__(self, clips, cfg):
        self.clips = clips
        self.hls = clips.cache / 'hls'
        self.exports = Path(cfg['recording']['state_dir']) / 'exports'
        self.remuxes = threading.BoundedSemaphore(3)
        self.export_slot = threading.BoundedSemaphore(1)
        self.cache_lock = threading.Lock()
        self.trim_exports()

    @staticmethod
    def trim_old(directory, pattern, age):
        cutoff = time.time() - age
        for path in directory.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass  # A response may have finished while trimming.

    def trim_exports(self):
        self.trim_old(self.exports, '*', 2 * 3600)

    def run(self, command, timeout):
        prefix = []
        if shutil.which('nice'):
            prefix += ['nice', '-n', '10']
        if shutil.which('ionice'):
            prefix += ['ionice', '-c3']
        try:
            return subprocess.run(prefix + command, capture_output=True, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired as exc:
            raise ClipError(504, 'media assembly timed out') from exc
        except OSError as exc:
            raise ClipError(500, 'media assembler unavailable') from exc

    def ts(self, camera, segment_id):
        row = self.clips.db.segment_by_id(segment_id)
        if (not row or row['camera_id'] != camera or row['quality'] != 'sub'
                or row.get('missing') or row.get('deleted_at')):
            raise ClipError(404, 'segment unavailable')
        try:
            source, exists = resolve_segment_path(self.clips.root, row['path'])
            if not exists:
                raise UnsafePath('missing')
        except UnsafePath as exc:
            raise ClipError(404, 'segment unavailable') from exc
        self.hls.mkdir(parents=True, exist_ok=True)
        target = self.hls / f'{segment_id}_{os.stat(source).st_mtime_ns}.ts'
        with self.remuxes:
            with self.cache_lock:
                self.trim_old(self.hls, '*.lease', 3600)
                if target.exists():
                    target.touch()
                    return self.lease(target)
            with tempfile.TemporaryDirectory(dir=self.hls) as tmp:
                output = Path(tmp) / 'out.ts'
                for audio in (True, False):
                    cmd = [self.clips.ffmpeg, '-v', 'error', '-nostdin', '-y', '-i', str(source), '-map', '0:v:0']
                    cmd += ['-map', '0:a?'] if audio else ['-an']
                    cmd += ['-c', 'copy', '-copyinkf:a', '-f', 'mpegts', '-mpegts_copyts', '0',
                            '-output_ts_offset', str(row['start_ts'] % 86400 + 1),
                            '-muxdelay', '0', '-muxpreload', '0', str(output)]
                    if self.run(cmd, self.clips.cfg['timeout_s']):
                        break
                else:
                    raise ClipError(500, 'segment remux failed')
                with self.cache_lock:
                    os.replace(output, target)
                    lease = self.lease(target)
                    files = sorted(self.hls.glob('*.ts'), key=lambda p: p.stat().st_mtime)
                    total = sum(p.stat().st_size for p in files)
                    for p in files:
                        size = p.stat().st_size
                        if total > 512 * 1024**2 or time.time() - p.stat().st_mtime > 21600:
                            p.unlink(missing_ok=True)
                            total -= size
                    return lease

    def lease(self, target):
        lease = self.hls / (uuid.uuid4().hex + '.lease')
        lease.hardlink_to(target)
        return lease

    def estimate(self, camera, quality, start, end):
        state, rows = self.clips.availability(camera, quality, start, end)
        if state != 'available':
            return dict(status=state, bytes=None)
        sizes = {r['path']: r.get('size') for r in self.clips.db.segment_rows(camera, quality, start, end)}
        total, cursor = 0, start
        for row in rows:
            size = sizes.get(row['path'])
            if size is None:
                size = os.stat(resolve_segment_path(self.clips.root, row['path'])[0]).st_size
            overlap = max(0, min(end, row['end_ts']) - max(cursor, row['start_ts']))
            total += size * overlap / (row['end_ts'] - row['start_ts'])
            cursor = row['end_ts']
        return dict(status=state, bytes=math.ceil(total))

    def export(self, camera, quality, start, end, estimate):
        self.trim_exports()
        self.exports.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.exports).free - estimate < 2_000_000_000:
            raise ClipError(507, 'insufficient export space')
        state, rows = self.clips.availability(camera, quality, start, end)
        if state != 'available':
            raise ClipError(425 if state == 'not_ready' else 404, 'footage unavailable')
        with tempfile.TemporaryDirectory(dir=self.exports) as tmp:
            listing, output = Path(tmp) / 'concat.txt', Path(tmp) / 'out.mp4'
            paths = [resolve_segment_path(self.clips.root, r['path'])[0] for r in rows]
            listing.write_text(concat_listing(paths, rows, start, end), encoding='utf-8')
            for audio in (True, False):
                if self.run(ffmpeg_command(self.clips.ffmpeg, listing, output, rows, audio),
                            max(self.clips.cfg['timeout_s'], (end - start) / 3)):
                    break
            else:
                raise ClipError(500, 'export assembly failed')
            target = self.exports / (uuid.uuid4().hex + '.mp4')
            os.replace(output, target)
            return target
