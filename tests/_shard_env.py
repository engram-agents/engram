"""One helper for every test that spawns the shard runner or the id-set check (part of #4420).

tests.yml's suite step sets NIGHTLY_SHARD_COUNT, NIGHTLY_SHARD_TIMEOUT_MIN, NIGHTLY_MARKER,
NIGHTLY_COVERAGE, NIGHTLY_COLLECT_FILE and NIGHTLY_IDSET_REFERENCE in the environment of the
pytest shards it runs. A test that then runs ``nightly_run_shards.sh`` or
``shard_idset_check.py`` in a subprocess must not inherit any of them: they are the runner's
own settings, and the real id-set check would run against the test's fake files. So such a test
builds its subprocess environment with ``hermetic_env()`` and adds its own settings on top.
"""

import os

# The prefixes of every setting the runner and the id-set check read for themselves.
RUNNER_ENV_PREFIXES = ("NIGHTLY_", "SHARD_IDSET_")


def hermetic_env(base=None):
    """A copy of ``base`` (default ``os.environ``) without any runner setting."""
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if not k.startswith(RUNNER_ENV_PREFIXES)}
