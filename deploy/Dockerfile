ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361
FROM ${PYTHON_IMAGE}
ARG UV_VERSION=0.7.13

LABEL org.opencontainers.image.title="Asset Factory Blueprint" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.source="https://github.com/hcltech-robotics/asset-factory-blueprint" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    PATH="/workspace/.venv/bin:${PATH}" \
    AFB_APPLIANCE_ROOT=/workspace \
    AFB_TRUSTED_TOOL_SERVER_NETWORK=1

COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md CITATION.cff codemeta.json ./
COPY src ./src
COPY configs ./configs
COPY schemas ./schemas
COPY library ./library
COPY examples ./examples
COPY scripts ./scripts
COPY skills ./skills

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv sync --frozen --no-dev --no-cache --no-editable --extra validation --extra mesh --extra vision \
    && groupadd --system afb \
    && useradd --system --gid afb --home-dir /workspace --shell /usr/sbin/nologin afb \
    && mkdir -p projects artifacts .cache library/downloads \
    && chown -R afb:afb projects artifacts .cache library/downloads

USER afb

EXPOSE 8181
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8181/healthz', timeout=3).read()"]
STOPSIGNAL SIGTERM
CMD ["afb", "tool-server", "--transport", "http", "--host", "0.0.0.0", "--port", "8181", "--job-store", "artifacts/tool-server-state", "--audit-log", "artifacts/tool-server-audit.jsonl"]
