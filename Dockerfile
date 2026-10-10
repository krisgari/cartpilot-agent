FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    CARTPILOT_STATE=/data/state.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY data/seed.json ./data/seed.json

# Run as an unprivileged user; /data is the only writable path (mount a volume there).
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status == 200 else 1)"

# One worker: the agent holds no per-process state, but SQLite is a single-node store.
CMD ["uvicorn", "cartpilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
