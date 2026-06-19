"""recall_summary_payload — PayloadBuilder for assembling engram_set_recall_summaries calls.

Downstream fairies use this to accumulate validated summary entries + failures
into a structurally-valid JSON payload without hand-editing strings.

Usage::

    from tools.recall_summary_payload import PayloadBuilder

    builder = PayloadBuilder()
    err = builder.add_summary("ob_NNNN", "Short curated summary.", ["kw1", "kw2", "kw3"])
    if err is not None:
        builder.add_failure("ob_NNNN", f"validation rejected: {err['error']}")

    payload_json = builder.to_json()
    # pass payload_json to engram_set_recall_summaries(payload_json=...)
"""

from __future__ import annotations

import json

from tools.recall_summary_validator import validate_summary_entry


class PayloadBuilder:
    """Accumulate recall-summary entries + failures into a batch payload."""

    def __init__(self) -> None:
        self._summaries: list[dict] = []
        self._failures: list[dict] = []

    def add_summary(
        self,
        node_id: str,
        recall_summary: str,
        recall_keywords: list[str],
    ) -> "dict | None":
        """Validate and add a summary entry.

        Returns:
            None if the entry passed validation and was added.
            The validator error dict if validation failed (entry is NOT added).
            Caller may call add_failure(node_id, reason) on rejection.
        """
        entry = {
            "node_id": node_id,
            "recall_summary": recall_summary,
            "recall_keywords": recall_keywords,
        }
        err = validate_summary_entry(entry)
        if err is not None:
            return err
        self._summaries.append(entry)
        return None

    def add_failure(self, node_id: str, reason: str) -> None:
        """Record a fairy-side failure (couldn't generate a summary at all)."""
        self._failures.append({"node_id": node_id, "reason": reason})

    def to_json(self) -> str:
        """Return JSON-encoded payload for engram_set_recall_summaries.

        Shape: {"summaries": [...], "failures": [...]}
        """
        return json.dumps({"summaries": self._summaries, "failures": self._failures})
