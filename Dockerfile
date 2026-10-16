FROM node:24-bookworm-slim AS mcp
WORKDIR /mcp
COPY vendor/torob-mcp ./
RUN npm ci --ignore-scripts && npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 TOROB_MCP_ENTRY=/opt/torob-mcp/dist/stdio.js
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=mcp /usr/local/bin/node /usr/local/bin/node
COPY --from=mcp /mcp /opt/torob-mcp
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home radar
USER radar
WORKDIR /work
ENTRYPOINT ["dealradar"]
CMD ["watch", "--config", "/work/config.toml"]
