# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 AS uv
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /opt/tooling
COPY pyproject.toml uv.lock LICENSE THIRD_PARTY.md README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Tooling publication targets linux/amd64, matching the supported cluster minor.
ADD --chmod=0755 --checksum=sha256:8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a \
    https://dl.k8s.io/release/v1.36.4/bin/linux/amd64/kubectl /usr/local/bin/kubectl
ARG VERSION
ARG REVISION
LABEL org.opencontainers.image.source="https://github.com/neurwerk/k8s_stack_addon_pipecat" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENV PATH="/opt/tooling/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
COPY --from=builder /opt/tooling/.venv /opt/tooling/.venv
USER 65532:65532
WORKDIR /tmp
CMD ["pipecat-validate", "--help"]
