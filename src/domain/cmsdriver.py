"""Shared vocabulary for replayed cmsDriver.py commands."""
from __future__ import annotations

from pathlib import Path

# Event counts are injected per branch at runtime; McM scripts must not pin them.
RUNTIME_MANAGED_OPTIONS = frozenset({"--number", "--number_out"})
# McM adds this customisation for production monitoring; it only inflates logs.
MONITORING_CUSTOMISE = "Configuration/DataProcessing/Utils.addMonitoring"


def basename_from_file_spec(file_spec: str) -> str:
    return Path(file_spec.removeprefix("file:")).name
