# ENGRAM spawn-flow test harness — clean-Ubuntu sandbox.
#
# Validates V1 §3.3 (Phase 5 spawn-flow real-world validation): runs
# `agentctl spawn` end-to-end inside a container with root, then verifies
# the newborn agent is correctly created, install.sh runs cleanly under
# the newborn uid, and `agentctl finalize-name` renames the substrate.
#
# Mirrors tests/install/Dockerfile's prereq stack; adds sudo + acl + the
# spawn-flow scripts (agent-bootstrap installed to /usr/local/sbin/,
# agentctl to /usr/local/bin/).
#
# Build:
#   docker build -t engram-spawn-test:latest -f tests/spawn/Dockerfile .
#
# Run:
#   docker run --rm -v "$(pwd):/alpha:ro" engram-spawn-test:latest

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Same prereq stack as tests/install/Dockerfile, plus sudo + acl which the
# spawn flow needs (agentctl uses setfacl/getfacl; finalize-name calls
# pause which routes through cron — kept minimal for the test).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 \
        python3-pip \
        git \
        cron \
        ca-certificates \
        curl \
        sqlite3 \
        sudo \
        acl \
        gawk \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install fastmcp sentence-transformers

# Stub claude. install.sh's MCP-add step uses it. Local copy (rather than
# referencing tests/install/) so this harness is independent of PR #49's
# state — when both land on dev, the duplicate can be deduplicated.
COPY tests/spawn/claude-stub.sh /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# Spawn-flow scripts. agent-bootstrap is the production target referenced
# by agentctl spawn; agentctl is the day-to-day driver. v0.1 of agentctl
# isn't yet vendored in alpha (issue #50), so the test harness vendors a
# snapshot via tests/spawn/agentctl-snapshot.
COPY tools/agent-bootstrap /usr/local/sbin/agent-bootstrap
RUN chmod 0755 /usr/local/sbin/agent-bootstrap
COPY tests/spawn/agentctl-snapshot /usr/local/bin/agentctl
RUN chmod 0755 /usr/local/bin/agentctl

# Pre-warm sentence-transformers cache so install.sh's seed-graph step
# doesn't pay the cold-load cost at test time. Same trick as PR #49's
# install harness.
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# /home/agents-shared is referenced by agent-bootstrap (Step 6) and
# finalize-name (ACL sweep). Pre-create as the agents group's territory
# so the spawn flow doesn't have to bootstrap it on every invocation.
RUN groupadd agents \
    && mkdir -p /home/agents-shared \
    && chown root:agents /home/agents-shared \
    && chmod 0775 /home/agents-shared

# The container runs as root (default) — the spawn flow requires it.
# run_spawn_test.sh is the entry point; it switches to the newborn user
# at the appropriate stages.
COPY tests/spawn/run_spawn_test.sh /root/run_spawn_test.sh
RUN chmod +x /root/run_spawn_test.sh

CMD ["/root/run_spawn_test.sh"]
