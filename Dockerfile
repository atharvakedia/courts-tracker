# One process serves the dashboard and runs the collector: `serve
# --with-collector` starts the poll loop on a background thread inside the
# web process, so a single always-on machine with one volume is the whole
# deployment. Nothing here is Fly-specific; the same image runs anywhere that
# can mount a directory at /data.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PADEL_TRACKER_CONFIG=/app/config.yaml \
    PADEL_TRACKER_STORAGE_URL=sqlite:////data/padel.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tracker ./tracker
COPY config.yaml ./config.yaml
RUN pip install --no-cache-dir . && mkdir -p /data

EXPOSE 8080
CMD ["python", "-m", "tracker", "serve", "--log-format", "json", "--host", "0.0.0.0", "--port", "8080", "--with-collector"]
