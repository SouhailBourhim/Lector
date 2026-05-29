FROM python:3.12-slim

# Only runtime deps — no compiler needed (all Python packages ship pre-built wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# --prefer-binary: always pick wheels over sdists, never compile from source
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt && \
    python -m spacy download en_core_web_sm

COPY . .

RUN mkdir -p /tmp/lector_cache

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s \
  CMD curl -f http://localhost:8000/health || exit 1

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
