# Packrat as an always-on server, e.g. on a NAS. See compose.yml for how to run it.
FROM python:3.12-slim

# Archive tools for .rar and .7z packs: 7-Zip, with bsdtar as a fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends 7zip libarchive-tools \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE /tmp/src/
COPY packrat /tmp/src/packrat
RUN pip install --no-cache-dir /tmp/src && rm -rf /tmp/src

# Settings (and the password) in /config, database and previews in /data, itch.io downloads in /downloads.
# It listens on the network, so other computers sign in; the setup code for the first password is in the log.
ENV XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data HOME=/data \
    PACKRAT_HOST=0.0.0.0 PACKRAT_PORT=8765 PACKRAT_DOWNLOADS=/downloads PYTHONUNBUFFERED=1
RUN mkdir -p /config /data /downloads /library && chown 1000:1000 /config /data /downloads
USER 1000:1000
WORKDIR /data
VOLUME ["/config", "/data"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PACKRAT_PORT\"]}/api/ping', timeout=4)"
CMD ["packrat", "serve"]
