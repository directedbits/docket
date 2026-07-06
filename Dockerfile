FROM python:3.12-slim

# Links the GHCR package to this repo (provenance + package page).
LABEL org.opencontainers.image.source="https://github.com/directedbits/docket"

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY docket/requirements.txt /srv/docket/requirements.txt
RUN pip install --no-cache-dir -r docket/requirements.txt
COPY docket /srv/docket
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# The server runs as a non-root user; the entrypoint (root) only fixes /data
# ownership on a mounted volume, then drops to appuser via gosu.
RUN useradd -r -u 10001 appuser && mkdir -p /data && chown -R appuser /srv /data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

ENV DB_PATH=/data/docs.db PORT=8765 HOST=0.0.0.0
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import socket,os; socket.create_connection(('127.0.0.1', int(os.environ.get('PORT','8765'))), 2).close()" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "docket.server"]
