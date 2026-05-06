"""Pytest configuration for engram-alpha tests.

Adds the repo root to sys.path so test files can `import server`.
This mirrors the `sys.path.insert(0, dirname(__file__))` pattern each
file uses when run as a script — keeping tests runnable both ways.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
