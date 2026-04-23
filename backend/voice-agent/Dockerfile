# Voice Agent runtime image (Fargate).
#
# Thin image that layers application code + runtime apt packages on top of a
# pre-baked base image containing the full Python venv (see Dockerfile.base).
#
# The BASE_IMAGE build arg MUST be set — either to an ECR URI tagged with a
# specific requirements.txt hash, or (for from-scratch local rebuilds) you
# can pass BASE_IMAGE=$(docker build -q -f Dockerfile.base .)
#
# A typical deploy does:
#   BASE=$(./infrastructure/scripts/build-base-image.sh --uri-only)
#   docker build --build-arg BASE_IMAGE="$BASE" -t voice-agent .
#
# Code-only changes produce a ~30-second build (vs ~10 min for the full
# pip install). Changes to requirements.txt require rebuilding the base
# image too — build-base-image.sh will detect the new hash and do that.

ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS builder
# No-op stage — we only need the builder's /app/venv layer.

# ========================
# Runtime
# ========================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime-only apt packages. Kept OUTSIDE Dockerfile.base so we can patch
# libsndfile/ffmpeg security updates without re-rolling the heavy pip install.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Pre-built venv from Dockerfile.base — the heavy pipecat+deps install is
# already done, we just copy it over.
#
# IMPORTANT: use --chown on every COPY instead of a single `RUN chown -R
# appuser /app` at the end. A trailing recursive chown touches every byte
# of the 590 MB venv on EVERY build and invalidates the layer whenever app
# code changes, adding ~80s per code-only build. --chown sets ownership at
# copy time, which means the venv's large layer only chown-walks once (when
# this line itself is invalidated by a base image change), and subsequent
# code-only changes skip it entirely.
COPY --chown=appuser:appuser --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER appuser

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
