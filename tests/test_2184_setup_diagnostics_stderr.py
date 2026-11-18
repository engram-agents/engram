"""Regression coverage for #2184's MCP stdout-protocol guard.

``_run_db_one_time_setup`` runs while the MCP server is starting.  Its
diagnostics must not be emitted on stdout, which is reserved for MCP protocol
messages.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_CORE_PATH = Path(__file__).resolve().parent.parent / "src" / "engram" / "engram_core.py"


def _setup_print_calls() -> list[ast.Call]:
    """Return every direct ``print`` call in the one-time DB setup function."""
    module = ast.parse(_CORE_PATH.read_text())
    setup = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_db_one_time_setup"
    )
    return sorted(
        (
            call
            for call in ast.walk(setup)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "print"
        ),
        key=lambda call: call.lineno,
    )


def _is_sys_stderr(keyword: ast.keyword) -> bool:
    return (
        keyword.arg == "file"
        and isinstance(keyword.value, ast.Attribute)
        and isinstance(keyword.value.value, ast.Name)
        and keyword.value.value.id == "sys"
        and keyword.value.attr == "stderr"
    )


@pytest.mark.parametrize(
    "call",
    _setup_print_calls(),
    ids=lambda call: f"line_{call.lineno}",
)
def test_every_one_time_setup_diagnostic_print_targets_stderr(call: ast.Call) -> None:
    """MCP startup diagnostics must never corrupt the stdout protocol stream."""
    assert any(_is_sys_stderr(keyword) for keyword in call.keywords), (
        f"print at engram_core.py:{call.lineno} must pass file=sys.stderr"
    )
