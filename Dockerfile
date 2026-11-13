FROM python:3.11-slim

LABEL io.modelcontextprotocol.server.name="io.github.cgajs/cityengine-mcp"
LABEL org.opencontainers.image.source="https://github.com/cgajs/cityengine-mcp"

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY cityengine_mcp ./cityengine_mcp
RUN pip install --no-cache-dir .

ENTRYPOINT ["cityengine-mcp"]
