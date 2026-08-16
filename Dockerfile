# cangjie-knowledge-mcp MCP server image
#
# Build:  docker build -t cangjie-knowledge-mcp .
# Run:    docker run -i --rm cangjie-knowledge-mcp
#         (speaks MCP JSON-RPC over stdio; use with opencode.json's mcp config)
#
# The knowledge base JSONL sources and the Python package are baked into the
# image. The binary BM25 index (.pkl) is regenerated automatically on first run.

FROM python:3.11-slim

WORKDIR /app

# Dependencies (PyYAML only; the search core is stdlib-only)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source + committed knowledge base
COPY src/ ./src/
COPY data/ ./data/
COPY config.yaml .

ENV PYTHONPATH=/app/src

# Run the MCP server over stdio
ENTRYPOINT ["python", "-m", "cjkb.mcp_server"]
