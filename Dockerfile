# MCP server image (adversary-gate-mcp over stdio), for MCP registries such as
# Glama that start the server and introspect its tools.
FROM python:3.12-slim

# verify_repo writes the baseline commit's tree with git (read-tree + checkout-index).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]"

# The tests the gate runs are the patch's code: they run as an unprivileged
# user, never as root. A repository mounted from the host belongs to another
# uid, which git refuses as "dubious ownership" -- inside this throwaway
# container that check guards nothing, so it is switched off for git only.
RUN useradd --system --uid 10001 --home-dir /home/gate --create-home gate \
    && git config --system --add safe.directory '*'
USER 10001
ENV HOME=/home/gate

# Policy belongs to whoever runs the container, never to the agent calling it:
#   -e ADVERSARY_GATE_BASE_REF=origin/main -e ADVERSARY_GATE_POLICY="--coverage-floor 0.9"
# This image has no bubblewrap, so the tests run unsandboxed inside it, and git
# trusts any repository mounted into it: mount a throwaway clone, never your
# working tree (e.g. -v "$PWD/throwaway":/work), and pass repo=/work.
ENTRYPOINT ["adversary-gate-mcp"]
