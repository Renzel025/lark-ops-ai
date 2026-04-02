"""
Grep-friendly timing logs: lines contain ``PERF`` so you can filter CloudWatch/SLS.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("lark-ops-ai")


def perf_log(label: str, t0: float) -> None:
    """Log elapsed ms since ``t0`` (from ``time.perf_counter()``)."""
    ms = (time.perf_counter() - t0) * 1000.0
    log.info("PERF %s ms=%.1f", label, ms)
