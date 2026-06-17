FROM python:3.11-slim-bookworm

WORKDIR /app

# ── Layer 1: Python deps (cached until pyproject.toml changes) ────────────────
# Empty agent package so find_packages() finds 'agent' during non-editable install.
# Real source is copied in Layer 3 below.
COPY pyproject.toml .
RUN mkdir -p agent && touch agent/__init__.py \
    && pip install --no-cache-dir ".[cloud]" \
    && rm -rf agent

# ── Layer 2: Chromium (cached until playwright version changes) ───────────────
RUN playwright install chromium --with-deps \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 3: Source (invalidated on every code change — lightweight) ──────────
COPY agent/ ./agent/
COPY api.py main.py ./

# Non-root user for container security
RUN groupadd -r agent && useradd --no-log-init -r -g agent -u 1000 agent \
    && mkdir -p /tmp/browser-agent \
    && chown -R agent:agent /app /tmp/browser-agent
USER agent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS=true \
    DATA_DIR=/tmp/browser-agent \
    LOG_LEVEL=INFO \
    MAX_CONCURRENT_SESSIONS=2

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
