FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    BACKUP_DIR=/backup \
    HOME=/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

# rclone copies the Garmin backup to Google Drive, OneDrive, a NAS… (Settings → Backup).
RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install . \
    && useradd --uid 1000 --home-dir /data --no-create-home hub \
    && mkdir -p /data /backup \
    && chown hub:hub /data /backup

USER hub

# Garmin and Zwift tokens, settings, SQLite databases and FIT backups.
VOLUME ["/data"]
# The Garmin account backup (Backup page).
VOLUME ["/backup"]

EXPOSE 8127

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8127/healthz', timeout=4)" || exit 1

ENTRYPOINT ["garmin-sync-hub"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8127"]
