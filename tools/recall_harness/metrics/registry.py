"""Metric registry — register and dispatch metric functions by name.

Per HARNESS-DESIGN.md "Metric plugin" section. Each metric is a python function
with signature `metric(test: RRU, golden: RRU, **kwargs) -> dict`. Functions
register via `@register_metric(name)` decorator and are dispatched by name
from the CLI / harness orchestrator.
"""
from __future__ import annotations

from typing import Callable

from ..rru import RankingResultUnit

MetricFn = Callable[..., dict]

_REGISTRY: dict[str, MetricFn] = {}


def register_metric(name: str):
    """Decorator: register a metric function under a public name."""
    def deco(fn: MetricFn) -> MetricFn:
        if name in _REGISTRY:
            raise ValueError(f"metric {name!r} already registered")
        _REGISTRY[name] = fn
        return fn
    return deco


def get_metric(name: str) -> MetricFn:
    if name not in _REGISTRY:
        raise KeyError(f"metric {name!r} not registered. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_metrics() -> list[str]:
    return sorted(_REGISTRY)


def apply_metric(
    name: str,
    test: RankingResultUnit,
    golden: RankingResultUnit,
    **kwargs,
) -> dict:
    """Apply a registered metric to a (test, golden) pair. Returns the
    metric's result dict, augmented with `metric_name` for traceability.
    """
    fn = get_metric(name)
    result = fn(test, golden, **kwargs)
    result["metric_name"] = name
    return result
