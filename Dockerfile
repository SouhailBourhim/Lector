FROM python:3.12-slim

# Only runtime deps — no compiler needed (all Python packages ship pre-built wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user for production safety
RUN useradd --create-home --shell /bin/bash lector

WORKDIR /app

COPY requirements.txt .
# --prefer-binary: always pick wheels over sdists, never compile from source
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt && \
    python -m spacy download en_core_web_sm

COPY . .

# Data dir: SQLite DB + all audio files. Mount a persistent volume here.
# Override with DATA_DIR env var (Railway/Render set this via deploy config).
ENV DATA_DIR=/data
RUN mkdir -p /data && chown -R lector:lector /data /app

USER lector

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s \
  CMD curl -f http://localhost:8000/health || exit 1

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
