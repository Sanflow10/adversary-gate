# MCP server image (adversary-gate-mcp over stdio), for MCP registries such as
# Glama that start the server and introspect its tools.
FROM python:3.12-slim

# verify_repo builds the baseline with `git archive`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]"

# Policy belongs to whoever runs the container, never to the agent calling it:
#   -e ADVERSARY_GATE_BASE_REF=origin/main -e ADVERSARY_GATE_POLICY="--sandbox bwrap"
# Mount the repository to judge, e.g. -v "$PWD":/work, and pass repo=/work.
ENTRYPOINT ["adversary-gate-mcp"]
