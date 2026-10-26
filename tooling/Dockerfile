# syntax=docker/dockerfile:1
# Pipecat-specific operations; same locked package/image structure as shared Tooling.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/

LABEL org.opencontainers.image.source="https://github.com/neurwerk/k8s_stack_addon_pipecat"
LABEL org.opencontainers.image.description="Pipecat add-on selection and credential preparation."
LABEL org.opencontainers.image.licenses="MIT"

COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY.md /app/
COPY src/ /app/src/

WORKDIR /app
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"

RUN groupadd --system app && useradd --system --gid app appuser
USER appuser

# No entrypoint; callers select the reviewed packaged command.
CMD []
