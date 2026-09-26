from __future__ import annotations

import json
import subprocess
from typing import Callable

from domain.chain import classify_prepid

GENERATOR_KINDS = frozenset({"wmLHEGS", "GS"})
_PROXY_HINT = "A valid grid proxy is required — run `voms-proxy-init -voms cms`."
_PROXY_ERROR_MARKERS = ("proxy", "x509", "voms", "certificate", "unauthorized", "not authorized", "authentication", "credential")
# dasgoclient prints these diagnostics to stdout with exit 0; they are not results.
_DAS_NOISE_MARKERS = ("jsonparser failure", "select sub keys", "Key path not found")


class DASError(RuntimeError):
    pass


def _default_runner(args: list[str]) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = f"dasgoclient failed ({' '.join(args)}): {detail}"
        if any(marker in detail.lower() for marker in _PROXY_ERROR_MARKERS):
            message = f"{message}. {_PROXY_HINT}"
        raise DASError(message)
    return result.stdout


def parse_wmlhegs_root(workflow_name: str) -> str | None:
    """cmsunified_task_<prepid>__v1_... -> <prepid>"""
    if "task_" not in workflow_name:
        return None
    prepid = workflow_name.split("task_", 1)[1].split("__", 1)[0].strip()
    return prepid or None


class DASClient:
    def __init__(self, runner: Callable[[list[str]], str] | None = None, datatier: str = "NANOAODSIM") -> None:
        self.runner = runner or _default_runner
        self.datatier = datatier

    def _query(self, query: str) -> str:
        return self.runner(["dasgoclient", "--query", query])

    def _lines(self, query: str) -> list[str]:
        return [
            line.strip() for line in self._query(query).splitlines()
            if line.strip() and not any(marker in line for marker in _DAS_NOISE_MARKERS)
        ]

    def _first(self, query: str) -> str | None:
        lines = self._lines(query)
        return lines[0] if lines else None

    def _normalize_pattern(self, text: str) -> str:
        """Turn free text or a partial dataset path into a wildcard DAS pattern ending in the datatier.

        A complete /primary/processed/TIER path is used verbatim. Otherwise the last typed
        component becomes a prefix wildcard, because dasgoclient echoes exact, non-existent
        paths back as if they existed.
        """
        text = text.strip().rstrip("/")
        if not text.startswith("/"):
            return f"/*{text}*/*/{self.datatier}"
        components = [c for c in text.split("/") if c]
        if len(components) >= 3:
            return "/" + "/".join(components)
        def star(component: str) -> str:
            return component if component.endswith("*") else component + "*"

        if len(components) == 1:
            return f"/{star(components[0])}/*/{self.datatier}"
        return f"/{components[0]}/{star(components[1])}/{self.datatier}"

    def search_datasets(self, text: str) -> list[str]:
        if not text.strip().strip("/"):
            return []  # would become a namespace-wide search
        return sorted(set(self._lines(f"dataset dataset={self._normalize_pattern(text)}")))

    def summary(self, dataset: str) -> dict:
        out = self._query(f"summary dataset={dataset}").strip()
        if not out:
            return {}
        data = json.loads(out)
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def parent(self, dataset: str) -> str | None:
        return self._first(f"parent dataset={dataset}")

    def mcm_prepid(self, dataset: str) -> str | None:
        return self._first(f"mcm dataset={dataset}")

    def config_generator(self, dataset: str) -> str | None:
        """The generator-root request (wmLHEGS or GS) named by one of the dataset's workflows."""
        for line in self._lines(f"config dataset={dataset}"):
            prepid = parse_wmlhegs_root(line)
            if prepid and classify_prepid(prepid) in GENERATOR_KINDS:
                return prepid
        return None

    def find_generator(self, dataset: str, max_depth: int = 2) -> str | None:
        """Walk parent datasets (nano -> mini -> DRPremix AOD) until a workflow names the generator root."""
        current, seen = dataset, set()
        for _ in range(max_depth + 1):
            if not current or current in seen:
                break
            seen.add(current)
            prepid = self.config_generator(current)
            if prepid:
                return prepid
            current = self.parent(current)
        return None
