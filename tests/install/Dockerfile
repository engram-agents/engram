# ENGRAM install.sh test harness — clean-Python sandbox.
#
# Builds an image with the minimum prerequisites to run install.sh: Python
# 3.12 final, git, pip-installed deps from requirements-lock.txt, and a
# stub `claude` CLI that satisfies `claude mcp add` without needing Claude
# Code itself.
#
# Base image is python:3.12-slim (Debian-based, stable Python 3.12). We
# previously used ubuntu:22.04 + apt python3.11, but Ubuntu 22.04 ships
# `python3.11` as `3.11.0rc1` (release candidate, pre-final) which lacks
# `sys.get_int_max_str_digits` that current `transformers` needs at import
# time. python:3.12-slim has a stable Python 3.12 and a working pip.
#
# Build:
#   docker build -t engram-install-test:latest -f tests/install/Dockerfile .
#
# Run a one-shot install + verify:
#   docker run --rm -v "$(pwd):/alpha:ro" engram-install-test:latest
#
# (See tests/install/README.md for the full harness workflow.)

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Python deps. Pre-installed at image-build time from the alpha tree's
# locked snapshot so install.sh's preflight check passes deterministically
# without a network round-trip on every run. Using requirements-lock.txt
# (not requirements.txt) gives a reproducible CI environment — same
# transitive versions every build, no pip-resolution-drift between today's
# build and a week-later debugging build.
COPY requirements-lock.txt /tmp/requirements-lock.txt
# --extra-index-url is required because the lockfile pins `torch==2.12.0+cpu`,
# whose `+cpu` local-version tag only resolves against PyTorch's CPU index.
# Matches the regeneration command in requirements-lock.txt's header.
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/requirements-lock.txt \
    && rm /tmp/requirements-lock.txt

# Stub `claude` CLI. install.sh requires `claude` on PATH (preflight), and
# Step 4 calls `claude mcp add engram -s user -- ...`. The stub returns
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
# Cache lives under $HOME/.cache/huggingface/, which subsequent install.sh
# + server.py invocations under the same testuser will hit transparently.
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# Mount alpha tree at /alpha (read-only) and run install.sh against
# fresh $HOME/.engram + $HOME/.claude. Verification script asserts the
# expected post-install state.
COPY --chown=testuser:testuser tests/install/run_install_test.sh /home/testuser/run_install_test.sh
RUN chmod +x /home/testuser/run_install_test.sh

CMD ["/home/testuser/run_install_test.sh"]
