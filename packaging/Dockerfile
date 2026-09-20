# Headless image running the MCP server over Streamable HTTP.
#   docker build -t edb-explorer -f packaging/Dockerfile .
#   docker run --rm -p 8765:8765 -v /path/to/evidence:/evidence:ro edb-explorer
# Then point your MCP client at http://localhost:8765/mcp
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]"

FROM python:3.12-slim
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/edb-explorer /usr/local/bin/edb-explorer
RUN useradd -r -u 10001 edb && mkdir /evidence && chown edb /evidence
USER edb
VOLUME ["/evidence"]
EXPOSE 8765
ENV EDB_EXPLORER_ALLOWED_PATHS=/evidence
ENTRYPOINT ["edb-explorer", "mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
