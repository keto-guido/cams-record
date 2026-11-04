#!/usr/bin/env python3
"""Docker healthcheck: GET <BASE_PATH>/healthz on the loopback address (stdlib only)."""

import os
import sys
import urllib.request

port = os.environ.get("HTTP_PORT", "8099")
base = (os.environ.get("BASE_PATH", "/cams-record") or "").strip().rstrip("/")
if base and not base.startswith("/"):
    base = "/" + base


def main() -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{base}/healthz", timeout=5) as r:
            return 0 if r.status == 200 else 1
    except Exception as exc:  # noqa: BLE001
        print(f"unhealthy: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
