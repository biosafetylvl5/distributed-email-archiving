# dea-capture with the courier runtime. Build context: this directory.
#
#   docker build -t dea-capture processor/
#   docker run --rm dea-capture dea-capture --version
#
# Runs as uid 1000 with nothing writable but /tmp. The default command runs
# deploy/service.yaml; mount another file over /app/service.yaml to change it.

FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv

FROM python:3.13-slim-trixie@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# dependencies first, so a source change doesn't refetch them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra service --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra service --no-editable

FROM python:3.13-slim-trixie@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2
LABEL org.opencontainers.image.source="https://github.com/biosafetylvl5/distributed-email-archiving" \
      org.opencontainers.image.description="Captures what archived newsletters embed and link to" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN useradd --uid 1000 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin dea
COPY --from=build /app/.venv /app/.venv
COPY deploy/service.yaml /app/service.yaml
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 COLUMNS=240 HOME=/tmp
USER 1000
WORKDIR /tmp
EXPOSE 8000
CMD ["courier", "run", "/app/service.yaml"]
