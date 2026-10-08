# F1 dashboard backend in a container (alternative to server/launch.sh + systemd).
# Build context = the repository root:  docker compose -f server/docker/docker-compose.yml up -d --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    F1DASH_DATA_DIR=/data F1DASH_CONFIG_OVERLAY=/app/server/config/server.toml
WORKDIR /app/main

COPY main/requirements.txt /app/main/requirements.txt
RUN pip install --no-cache-dir -r /app/main/requirements.txt

COPY main /app/main
COPY server/config /app/server/config
COPY server/disk /app/server/disk
COPY server/tv /app/server/tv

RUN useradd --create-home --uid 1000 f1 && mkdir -p /data && chown -R f1:f1 /app /data
USER f1
VOLUME ["/data"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "main.py"]
