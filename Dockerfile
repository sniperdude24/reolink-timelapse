# reolink-timelapse -- generic image (any x86_64 / arm64 Docker host).
#
# Software video decode only: the Debian ffmpeg here has no Raspberry Pi
# hardware-decode patches. On a Raspberry Pi 5 build Dockerfile.pi
# instead, which pulls Raspberry Pi OS's patched ffmpeg for rpivid HEVC
# hardware decode. Everything stateful (config.yaml, stream_users.yaml,
# Timelapses/) lives on the /data volume via REOLINK_TIMELAPSE_HOME.
#
#   docker build -t reolink-timelapse .
#   docker compose up -d            # see docker-compose.yml
#   docker compose exec stream reolink-timelapse users add me --admin
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md ./
COPY reolink_timelapse ./reolink_timelapse
RUN pip install --no-cache-dir . && rm -rf /src

ENV REOLINK_TIMELAPSE_HOME=/data \
    PYTHONUNBUFFERED=1
RUN useradd --uid 1000 --create-home app && mkdir -p /data && chown app:app /data
USER app
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8177

ENTRYPOINT ["reolink-timelapse"]
CMD ["serve-stream"]
