"""Claude's ordinary user-message view of already-selected context.

This renderer changes presentation, not selection, authority, or stored history.
Fences preserve arbitrary UTF-8 content without letting a quoted delimiter end
its document. They are a textual boundary, not a model-obedience guarantee.
"""

from __future__ import annotations

import json
import re
from typing import Any


CLAUDE_DOCUMENT_FORMAT = "lattice-context-documents-v11"
DOCUMENT_TITLES = {
    "instruction": "Task instructions",
    "knowledge": "Reference",
    "state": "State",
    "ancestry": "Earlier response",
    "current_prompt": "Current request",
}


def claude_document_bytes(segment: dict[str, Any], content: str, *, sole_request: bool) -> bytes:
    # The common one-message case is exactly the user's text, without a protocol
    # preamble, custom system instruction, or redundant schema instruction.
    if segment["plane"] == "current_prompt" and sole_request:
        return content.encode("utf-8")
    # One longer than every backtick run also handles adversarial closing fences
    # indented or embedded in source text. JSON quoting keeps labels on one line.
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", content)), default=0))
    title = DOCUMENT_TITLES[segment["plane"]]
    source = json.dumps(segment["label"], ensure_ascii=True)
    lineage = " (derived selection)" if segment["derived_from_block_ids"] else ""
    return (
        f"### {title}{lineage}\nSource: {source}\n"
        f"Status: {segment['status']}; selection: {segment['disposition']}\n\n"
        f"{fence}\n{content}\n{fence}\n\n"
    ).encode("utf-8")
