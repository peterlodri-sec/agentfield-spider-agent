"""Tiny structured-log helper.

Same shape as rag-stack's ``log()`` so log lines from the spider agent
are diff-friendly with the existing crawler when both are deployed.
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("spider")


def log(action: str, **fields: Any) -> None:
    """Emit ``action k1=v1 k2=v2`` style log lines.

    String values longer than 80 chars are truncated with a trailing
    ellipsis. Strings containing whitespace are quoted. Newlines are
    flattened to spaces.
    """
    parts = []
    for k, v in fields.items():
        if isinstance(v, str):
            v = v.replace("\n", " ")
            if len(v) > 80:
                v = v[:77] + "..."
            if " " in v or "\t" in v:
                v = f'"{v}"'
        parts.append(f"{k}={v}")
    _logger.info(f"{action:14s} {' '.join(parts)}")
