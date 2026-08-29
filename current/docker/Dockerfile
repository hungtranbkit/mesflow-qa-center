ARG QA_BASE_IMAGE=mesflow-qa-base:unresolved
FROM ${QA_BASE_IMAGE}

ARG QA_RUNTIME_VERSION=1.0.0
LABEL org.opencontainers.image.title="MESFlow QA Runtime" \
      org.opencontainers.image.version="$QA_RUNTIME_VERSION"

ENV PYTHONDONTWRITEBYTECODE=1 \
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

# Heavy system/Python/browser dependencies are already baked into the
# fingerprinted QA base image. A normal release build now only copies the
# changing application source into a thin final layer.
COPY . /app

RUN mkdir -p /data/logs /data/reports /data/state

EXPOSE 8095

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8095/api/version >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","/app/agent.py"]
