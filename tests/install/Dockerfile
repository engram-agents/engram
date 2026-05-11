# ENGRAM install.sh test harness — clean-Ubuntu sandbox.
#
# Builds an image with the minimum prerequisites to run install.sh: Python
# 3.10+, git, pip-installed fastmcp + sentence-transformers, and a stub
# `claude` CLI that satisfies the `claude mcp add` step without needing
# Claude Code itself.
#
# Build:
#   docker build -t engram-install-test:latest -f tests/install/Dockerfile .
#
# Run a one-shot install + verify:
#   docker run --rm -v "$(pwd):/alpha:ro" engram-install-test:latest
#
# (See tests/install/README.md for the full harness workflow.)

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 \
        python3-pip \
        git \
        ca-certificates \
        curl \
        sqlite3 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

# Python deps. Pre-installed at image-build time so install.sh's preflight
# check passes without a network round-trip on every run.
# Note on PEP 668: Ubuntu 22.04 ships pip 22.0.2, which doesn't know
# --break-system-packages, AND doesn't ship the EXTERNALLY-MANAGED marker
# file (that arrived in Ubuntu 23.04). Plain `pip3 install` works. If the
# base image is bumped to 23.04+, add --break-system-packages back.
RUN pip3 install fastmcp sentence-transformers

# Stub `claude` CLI. install.sh requires `claude` on PATH (line 79), and
# step 4 calls `claude mcp add engram -s user -- ...`. The stub returns
# success for `mcp add`, `mcp remove`, `--version`, and `mcp list`. Real
# MCP registration isn't testable in Docker (Claude Code is a native
# binary not pip-installable); the stub lets install.sh complete its
# remaining steps so the rest of the install path is exercised.
COPY tests/install/claude-stub.sh /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# Pre-create test-user home (matches install.sh's $HOME assumption). Using
# a non-root user catches permission-related bugs that root would mask.
RUN useradd -m -s /bin/bash testuser
USER testuser
WORKDIR /home/testuser

# Git identity for the testuser — install.sh step 7 runs `git init && git
# add -A && git commit` inside $ENGRAM_HOME, which fails fatally without
# user.name + user.email configured. Setting at image-build time means
# every container instance has the identity ready before install.sh runs.
RUN git config --global user.email "engram-test@localhost" && \
    git config --global user.name "ENGRAM Test Harness"

# Pre-warm the sentence-transformers cache so install.sh's first run
# doesn't pay the ~80MB MiniLM model download on every test. The model
# loads lazily on first use, so triggering one import here caches it.
# Cache lives under $HOME/.cache/torch/sentence_transformers/, which
# subsequent install.sh + server.py invocations under the same testuser
# will hit transparently.
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# Mount alpha tree at /alpha (read-only) and run install.sh against
# fresh $HOME/.engram + $HOME/.claude. Verification script asserts the
# expected post-install state.
COPY --chown=testuser:testuser tests/install/run_install_test.sh /home/testuser/run_install_test.sh
RUN chmod +x /home/testuser/run_install_test.sh

CMD ["/home/testuser/run_install_test.sh"]
