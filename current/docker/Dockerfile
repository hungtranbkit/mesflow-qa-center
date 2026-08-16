FROM python:3.12-slim-bookworm

ARG QA_RUNTIME_VERSION=1.0.0
LABEL org.opencontainers.image.title="MESFlow QA Runtime" \
      org.opencontainers.image.version="$QA_RUNTIME_VERSION"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MESFLOW_QA_HOST=0.0.0.0 \
    MESFLOW_QA_PORT=8095 \
    MESFLOW_QA_CONFIG_PATH=/data/config.json \
    MESFLOW_QA_LOG_DIR=/data/logs \
    MESFLOW_QA_REPORT_DIR=/data/reports \
    MESFLOW_QA_STATE_DIR=/data/state \
    MESFLOW_QA_RUNTIME_DIR=/data

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl procps iproute2 tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/qa-runtime/requirements.txt
RUN pip install --no-cache-dir --upgrade pip wheel setuptools \
    && pip install --no-cache-dir -r /tmp/qa-runtime/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /root/.cache/pip

# Build Once / Promote Same Artifact: bake the actual QA application source
# into the image so the release built here is immutable and self-contained
# (never needs the workspace bind-mounted at a deployment target). Local
# dev usage via docker/compose.yml's `volumes: - ..:/app:ro` still works
# unchanged -- that bind mount overrides everything copied here.
COPY . /app

RUN mkdir -p /data/logs /data/reports /data/state

EXPOSE 8095

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8095/api/version >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","/app/agent.py"]
