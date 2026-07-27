# No `# syntax=` directive on purpose: pinning the BuildKit frontend forces a
# registry round trip on every build and breaks offline builds entirely.
# Nothing below needs a newer frontend than the built-in one.
#
# The API key is never baked in. Pass it at run time via env_file or a secret.
#
# ---------------------------------------------------------------------------
# Why the runtime starts from bare Alpine rather than python:3.12-alpine
#
# Deleting a file that arrived in a lower layer reclaims nothing — it writes a
# whiteout and the original bytes stay in the image. Pruning the interpreter in
# a `FROM python:...` runtime stage measurably made things *worse*: 336kB of
# whiteouts on top of a 48.1MB CPython layer that still held every byte.
#
# So the pruning happens in the builder, which is discarded, and the runtime
# copies only what survived. Together with dropping Debian for Alpine and
# fastapi for starlette, that took the image from 220MB to 62.4MB.
#
# The cost is a coupling: ALPINE_VERSION below must match the Alpine that
# python:3.12-alpine is built on, or the copied interpreter meets a different
# musl. Check with:
#   docker run --rm python:3.12-alpine cat /etc/alpine-release
# ---------------------------------------------------------------------------

ARG PYTHON_IMAGE=python:3.12-alpine
ARG ALPINE_VERSION=3.24

# ---------------------------------------------------------------- builder ---
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY ontime ./ontime

# `--only-binary` means a dependency without a musllinux wheel fails the build
# loudly instead of quietly compiling Rust. Nothing here has a compiled
# dependency since fastapi (and so pydantic-core) was dropped.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --only-binary=:all: . \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel

# Everything below is deleted in a stage that gets thrown away, so the runtime
# COPY never carries it. A package installer in a running production container
# turns code execution into arbitrary dependency installation, which is why pip
# goes as well as the interactive and build-time machinery.
RUN rm -rf /usr/local/include \
           /usr/local/lib/pkgconfig \
           /usr/local/lib/python3.12/idlelib \
           /usr/local/lib/python3.12/ensurepip \
           /usr/local/lib/python3.12/turtledemo \
           /usr/local/lib/python3.12/tkinter \
           /usr/local/lib/python3.12/lib2to3 \
           /usr/local/lib/python3.12/test \
           /usr/local/lib/python3.12/config-3.12-* \
           /usr/local/lib/python3.12/site-packages/pip* \
           /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/site-packages/wheel* \
           /usr/local/bin/pip* /usr/local/bin/idle* \
 && rm -f /usr/local/lib/python3.12/lib-dynload/_test*.so \
 && find /usr/local /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /usr/local /opt/venv -name '*.pyc' -delete \
 && find /usr/local -name '*.a' -delete

# ---------------------------------------------------------------- runtime ---
FROM alpine:${ALPINE_VERSION} AS runtime

# The shared libraries CPython links against, which the interpreter copied
# below cannot supply for itself. tzdata is load-bearing rather than optional:
# every service-day calculation goes through ZoneInfo("Europe/London"), so
# naming it here means a base without it fails the build, not the arithmetic.
RUN apk add --no-cache \
      ca-certificates \
      libcrypto3 libssl3 \
      sqlite-libs \
      libffi zlib libbz2 xz-libs gdbm libintl \
      readline ncurses-libs \
      tzdata \
 && adduser -S -D -H -h /app ontime \
 && mkdir -p /data && chown ontime /data

COPY --from=builder --chown=root:root /usr/local /usr/local
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
