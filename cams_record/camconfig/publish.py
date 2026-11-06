"""Write App Hub's cams.json from cams_config (camera_source: db with access.publish_registry)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from ..camera_access import parse_registry
from .model import CameraObject
from .views import registry

log = logging.getLogger(__name__)


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write by rename in the same directory: readers watching the inode see old or new, never half."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def registry_text(cams: dict[str, CameraObject]) -> str:
    """cams.json text. Raises ValueError rather than publish a file that would drop any camera."""
    text = json.dumps(registry(cams), indent=2) + "\n"
    kept = parse_registry(text)
    if set(kept) != set(cams):
        missing = sorted(set(cams) - set(kept))
        raise ValueError(f"the registry would drop {', '.join(missing)}")
    return text


def publish_registry(path: str | Path, cams: dict[str, CameraObject]) -> bool:
    """Write cams.json when its content changes. True if written. The old file stays on any error."""
    path = Path(path)
    text = registry_text(cams)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    atomic_write(path, text)
    log.info("published %s: %d cameras", path, len(cams))
    return True
