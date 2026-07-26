# syntax=docker/dockerfile:1
#
# Two stages. The builder compiles a wheel and resolves dependencies; the
# runtime layer receives the resulting virtualenv and nothing else. No
# compiler, no pip, no test suite, no source tree, no dev extras — the
# `[project.optional-dependencies].dev` group is never requested, so pytest,
# ruff and mypy cannot reach the final image.
#
# The API key is never baked in. Pass it at run time via env_file or a secret.

# ---------------------------------------------------------------- builder ---
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY ontime ./ontime

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install . \
 # Strip the build tooling back out: it is not needed to run the application.
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel \
 && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name '*.pyc' -delete

# ---------------------------------------------------------------- runtime ---
FROM python:3.12-slim AS runtime

RUN adduser --system --group --home /app ontime \
 && mkdir -p /data \
 && chown ontime:ontime /data

COPY --from=builder --chown=root:root /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ONTIME_DATA_DIR=/data \
    ONTIME_HOST=0.0.0.0 \
    ONTIME_PORT=8000

WORKDIR /app
USER ontime
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "ontime.web"]
