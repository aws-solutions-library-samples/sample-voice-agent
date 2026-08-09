# Voice Agent Pipeline Container for AWS ECS Fargate
# Build: docker build -t voice-agent .
#
# This container runs pipecat directly with asyncio.run() - the pattern
# pipecat is designed for. Unlike FastAPI-based approaches, this ensures
# all of pipecat's internal async tasks execute properly.
#
# Multi-stage build: build stage compiles any native extensions,
# runtime stage contains only what's needed to run the application.
# Note: pipecat v0.0.102 uses ONNX Runtime for Silero VAD (not PyTorch),
# so torch/torchaudio are not needed.

# ========================
# Stage 1: Build / Install
# ========================
# Using Amazon Linux 2023 for better AWS integration and security
FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools for any native extensions (gcc, python3-devel)
# Amazon Linux 2023 uses OpenSSL (no gnutls/mbedtls vulnerabilities)
RUN yum install -y \
    gcc \
    python3.13 \
    python3.13-devel \
    python3.13-pip \
    && yum clean all

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .

# Set Python 3.13 as default and install dependencies into a virtual env
RUN alternatives --install /usr/bin/python3 python3 /usr/bin/python3.13 1 \
    && alternatives --set python3 /usr/bin/python3.13 \
    && python3 -m venv /app/venv

ENV PATH="/app/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# ========================
# Stage 2: Runtime
# ========================
# Using Amazon Linux 2023 for better AWS integration and security
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime-only system dependencies (no gcc/python3-devel)
# Amazon Linux 2023 uses OpenSSL (no gnutls/mbedtls vulnerabilities)
RUN yum install -y \
    python3.13 \
    python3.13-libs \
    # Audio processing
    libsndfile \
    # User management tools
    shadow-utils \
    # Networking (health checks) - curl-minimal already installed
    && yum clean all

# Set Python 3.13 as default
RUN alternatives --install /usr/bin/python3 python3 /usr/bin/python3.13 1 \
    && alternatives --set python3 /usr/bin/python3.13

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

# Set working directory
WORKDIR /app

# Copy virtual env with installed packages from builder
COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# Copy application code
COPY app/ ./app/

# Entry point script that handles both service and task modes
# SERVICE_MODE=true: Run HTTP server (always-on, handles multiple calls)
# SERVICE_MODE=false/unset: Run single task (legacy mode, one call per container)
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port 8080 for service mode (HTTP server for receiving call requests)
EXPOSE 8080

CMD ["/app/entrypoint.sh"]
