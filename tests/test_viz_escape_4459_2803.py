"""Escape node data in the graph tooltip (#4459) and agent names in the
health-workbench agent <select> (#2803).

Static checks read the shipped JS; the node checks execute the real
``escHtml`` / option-builder source extracted from the files.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "engram" / "viz" / "static"
GRAPH = (STATIC / "graph.js").read_text(encoding="utf-8")
WB = (STATIC / "health-workbench.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
PAYLOADS = ['<img src=x onerror=alert(1)>', '"><script>alert(1)</script>', "a'b&c"]


def _func_body(src: str, header: str) -> str:
    start = src.index(header)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("unbalanced braces for " + header)


def _tooltip() -> str:
    return _func_body(GRAPH, "function showTooltip(")


def _option_builder() -> str:
    start = WB.index("state.agents.map(function (a) {\n          return '<option")
    return WB[start : WB.index('.join("")', start)]


# ---- static -------------------------------------------------------------

def test_tooltip_interpolations_escaped():
    body = _tooltip()
    interps = re.findall(r"\$\{([^}]*)\}", body)
    sensitive = [e for e in interps
                 if re.search(r"d\.id|d\.status|NODE_STYLE|d\.type|text\b|truncate\(", e)
                 and "tierColor" not in e and "tierLabel" not in e]
    assert sensitive, "expected sensitive interpolations in showTooltip"
    for e in sensitive:
        if "d.status ===" in e and "retracted" in e and "escHtml" not in e:
            continue  # colour ternary yields fixed literals
        assert "escHtml(" in e, f"unescaped interpolation: ${{{e}}}"
    assert "escHtml(truncate(text, 80))" in body.replace("\n", "")
    assert "escHtml(d.id)" in body
    assert "escHtml(d.status)" in body
    assert "escHtml(NODE_STYLE[d.type]?.label || d.type)" in body


def test_tooltip_pushes_are_literals_or_escaped():
    """Every lines.push(...) argument is a string literal/template or an escHtml(...) call,
    so a non-${} leak such as lines.push(d.id) is caught too."""
    body = _tooltip()
    # initial array literal element counts as a push too
    pushes = re.findall(r"lines\.push\((.*)\);", body) + re.findall(r"let lines = \[(.*)\];", body)
    assert len(pushes) >= 6, pushes
    for arg in pushes:
        arg = arg.strip()
        assert arg.startswith(("'", '"', "`", "escHtml(")), f"lines.push argument is neither a literal nor escaped: {arg}"


def test_edge_chip_template_escaped():
    body = _func_body(GRAPH, "function renderChips(")
    start = body.index("chip.innerHTML =")
    tpl = body[start : body.index("chip.onclick", start)]
    interps = re.findall(r"\$\{([^}]*)\}", tpl)
    assert any("otherId" in e for e in interps) and any("otherType" in e for e in interps)
    fixed = {"color", "otherColor", "isOutgoing ? '→' : '←'"}  # fixed-literal lookups
    for e in interps:
        if e.strip() in fixed:
            continue
        if e.strip().startswith("otherClaim ?"):
            assert "escHtml(otherClaim)" in e
            continue
        assert e.strip().startswith("escHtml("), f"unescaped chip interpolation: ${{{e}}}"
    assert "escHtml(otherId)" in tpl and "escHtml(otherType)" in tpl


def test_escHtml_coerces_and_escapes_quotes():
    body = _func_body(GRAPH, "function escHtml(")
    assert "String(s)" in body
    assert "&quot;" in body and "&#39;" in body


def test_workbench_option_builder_escapes():
    b = _option_builder()
    # value attribute: name escaped; text content: (label || name) escaped
    assert '<option value="\' + escAttr(a.name) + \'"' in b, b
    assert 'escAttr(a.label || a.name)' in b, b
    assert "a.name +" not in b.replace("escAttr(a.name)", ""), b
    assert "a.name === state.agent" in b


# ---- executed -----------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not installed; static checks still run")
def test_escHtml_executed():
    code = _func_body(GRAPH, "function escHtml(") + \
        f"\nconst P={json.dumps(PAYLOADS)};" \
        "console.log(JSON.stringify([P.map(escHtml), escHtml(5), escHtml(null), escHtml('ob_0001')]));"
    out = json.loads(subprocess.run([NODE, "-e", code], capture_output=True, text=True, check=True).stdout)
    for s in out[0]:
        assert "<" not in s and ">" not in s and '"' not in s and "'" not in s
    assert out[0][0] == "&lt;img src=x onerror=alert(1)&gt;"
    assert out[0][2] == "a&#39;b&amp;c"
    assert out[1] == "5" and out[3] == "ob_0001"


@pytest.mark.skipif(NODE is None, reason="node not installed; static checks still run")
def test_workbench_options_executed():
    code = (
        _func_body(WB, "function escAttr(")
        + f"\nconst state={{agent:'ariadne',agents:{json.dumps([{'name': p, 'label': p} for p in PAYLOADS] + [{'name': 'ariadne', 'label': 'Ariadne (Claude)'}])}}};\n"
        + "console.log(" + _option_builder() + '.join(""));'
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, check=True).stdout
    assert "<img" not in out and "<script" not in out
    assert "&lt;img" in out and "&lt;script" in out and "&quot;" in out
    assert '<option value="ariadne" selected>Ariadne (Claude)</option>' in out
