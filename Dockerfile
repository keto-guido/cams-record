# cams-record - builds with the legacy builder (DOCKER_BUILDKIT=0): no RUN --mount, no heredocs.
FROM python:3.12-slim

# Static ffmpeg/ffprobe binaries (mwader/static-ffmpeg, pinned tag, amd64/arm64). Stream copy only,
# so no GPU or codec libraries are needed and the image stays small.
COPY --from=mwader/static-ffmpeg:7.1.1 /ffmpeg /ffprobe /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    CONFIG_DIR=/config

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY cams_record /app/cams_record
COPY scripts /app/scripts
COPY config/config.example.yaml config/cameras.example.yaml /app/config/

RUN mkdir -p /config /volume1/nvr /tmp/cams-record

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "/app/scripts/healthcheck.py"]

ENTRYPOINT ["python", "-m", "cams_record"]
CMD ["--config-dir", "/config"]
