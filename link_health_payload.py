#!/usr/bin/env python3
"""Minimal GCS JSON payload helper for the dashboard."""
from __future__ import annotations

from typing import Any


STATUS_UP = "UP"
STATUS_DOWN = "DOWN"
STATUS_NOT_INCLUDED = "NOT_INCLUDED"


def build_gcs_forward_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Return the minimal read-only GCS payload.

    Status meanings:
    - UP: Link is included in MAVProxy and current telemetry is arriving.
    - DOWN: Link is included in MAVProxy, but current telemetry is not arriving.
    - NOT_INCLUDED: Link is not present in the MAVProxy master list.
    """
    links = []
    for link in state.get("links", []):
        included = bool(link.get("enabled", False))
        up = included and bool(link.get("up", False))

        if not included:
            status = STATUS_NOT_INCLUDED
        elif up:
            status = STATUS_UP
        else:
            status = STATUS_DOWN

        links.append(
            {
                "name": str(link.get("name") or link.get("id") or "UNKNOWN"),
                "status": status,
                "quality_pct": round(
                    float(link.get("quality_pct", 0.0) or 0.0), 1
                ),
                "active_tx": bool(link.get("active_tx", False)) and included,
            }
        )

    return {"links": links}
