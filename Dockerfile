# AREDN Fleet Manager, headless web UI, for Linux containers (amd64 and arm64).
# Runs on Docker on Linux, on Raspberry Pi, and on Docker Desktop for Windows/Mac
# (Linux containers, the default). See README.md, "Docker".
FROM python:3.12-slim

LABEL org.opencontainers.image.title="AREDN Fleet Manager" \
      org.opencontainers.image.description="Safe, bulk management of AREDN mesh nodes (headless web UI)" \
      org.opencontainers.image.source="https://github.com/km6slf/FleetManager" \
      org.opencontainers.image.licenses="GPL-3.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Everything Fleet Manager keeps (settings, pinned host keys, backups, firmware cache,
    # job logs) lives in /data: mount a volume there and upgrades keep it.
    AREDN_FLEET_HOME=/data \
    # Saved node and Wi-Fi passwords: an encrypted file in /data/secrets, unlocked by
    # FLEET_SECRETS_PASSWORD (there's no desktop credential store in a container).
    FLEET_SECRETS_FILE=/data/secrets/passwords.cfg \
    FLEET_PORT=8787

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[container]" \
 && useradd --create-home --uid 10001 fleet \
 && mkdir -p /data && chown fleet:fleet /data \
 && rm -rf /src

USER fleet
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('FLEET_PORT','8787'), timeout=4)"

ENTRYPOINT ["sh", "-c", "exec aredn-fleet serve --host 0.0.0.0 --port \"$FLEET_PORT\""]
