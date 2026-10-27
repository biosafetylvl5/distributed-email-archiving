# dea-capture with the courier runtime, on browsertrix-crawler for capture-page.
# Build context: this directory.
#
#   docker build -t dea-capture processor/
#   docker run --rm dea-capture dea-capture --version
#
# Runs as uid 1000 with nothing writable but /tmp. The default command runs
# deploy/service.yaml; mount another file over /app/service.yaml to change it.
# The crawler's own entrypoint (which switches users) is not used: dea-capture
# runs `crawl` itself, behind its guarding proxy.

FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv

FROM webrecorder/browsertrix-crawler:1.15.1@sha256:710b08c749648fa9b2e0f73a3358bc682fbf4ddb83188c929f3b3862d1ae2ecb AS build
COPY --from=uv /uv /usr/local/bin/uv
# the image's Python (Ubuntu's 3.12), so the final stage needs nothing more
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3 UV_PROJECT_ENVIRONMENT=/opt/dea
WORKDIR /src
# dependencies first, so a source change doesn't refetch them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra service --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra service --no-editable

FROM webrecorder/browsertrix-crawler:1.15.1@sha256:710b08c749648fa9b2e0f73a3358bc682fbf4ddb83188c929f3b3862d1ae2ecb
LABEL org.opencontainers.image.source="https://github.com/biosafetylvl5/distributed-email-archiving" \
      org.opencontainers.image.description="Captures what archived newsletters embed and link to" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /opt/dea /opt/dea
COPY deploy/service.yaml /app/service.yaml
ENV PATH=/opt/dea/bin:$PATH PYTHONUNBUFFERED=1 COLUMNS=240 HOME=/tmp
# uid 1000 is the base image's `ubuntu` user
USER 1000
WORKDIR /tmp
EXPOSE 8000
ENTRYPOINT []
CMD ["courier", "run", "/app/service.yaml"]
