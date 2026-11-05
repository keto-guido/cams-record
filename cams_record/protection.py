"""Detection event windows and a fail-soft, read-only event poller."""
import logging
import threading
import time

log = logging.getLogger(__name__)


def event_window(start, end, cfg):
    lo = start - float(cfg.get("pre_s", 5))
    hi = (end if end is not None else start + float(cfg.get("fallback_s", 10))) + float(cfg.get("post_s", 5))
    return lo, min(hi, lo + float(cfg.get("max_window_s", 120)))


class DetectionProtection:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.watermark = time.time() - float(cfg["lookback_s"])
        self.stop_event = threading.Event()
        self.thread = None

    def run_once(self):
        now = time.time()
        try:
            events = self.db.detection_events(self.watermark - float(self.cfg["lookback_s"]))
            for ev in events:
                start, end = event_window(ev["start_ts"], ev["end_ts"], self.cfg)
                self.db.protect_window(ev["camera_id"], start, end, now + float(self.cfg["days"]) * 86400)
            self.watermark = now
        except Exception:
            log.warning("detection protection: poll failed (check SELECT grant on cams_detect.events)", exc_info=True)

    def _run(self):
        while not self.stop_event.is_set():
            self.run_once()
            self.stop_event.wait(float(self.cfg["interval_s"]))

    def start(self):
        if self.db is not None and self.cfg["enabled"]:
            self.thread = threading.Thread(target=self._run, name="detection-protection", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)
