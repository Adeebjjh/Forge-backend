# Forge Agent runner — deployable on Railway (or anywhere with Docker).
# The runner is stdlib-only Python: no pip packages required.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY runner ./runner
COPY web ./web

# Railway injects $PORT. FORGE_TOKEN (24+ chars) must be set as a variable.
# FORGE_WORKSPACE defaults to /data — mount a Railway volume there so your
# workspace survives restarts and redeploys.
CMD ["sh", "-c", "python -m runner.server --workspace ${FORGE_WORKSPACE:-/data} --host 0.0.0.0 --port ${PORT:-8787}"]
