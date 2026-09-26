from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_text(path: Path, content: str, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not path.exists():
        path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any], overwrite: bool = True) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", overwrite)
