"""Path resolution shared by the law tasks.

On an HTCondor worker the repo lives at $BASE_DIR (= $PWD/repo) while configs and step plans
may carry absolute paths from the submitter; these helpers remap them onto the worker checkout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from application import DEFAULT_DRIVER_ROOT, DEFAULT_PLANS_ROOT, StepPlanBuilder
from domain.configuration import ConfigLoader, step_slug

_loader = ConfigLoader()
_REPO_MARKERS = {"configs", "artifacts", "src"}


def _base_dir() -> Path | None:
    base_dir = os.environ.get("BASE_DIR")
    return Path(base_dir).expanduser().resolve() if base_dir else None


def _remap_to_base_dir(path: Path) -> Path | None:
    """Rewrite a path below a configs/-anchored repo onto $BASE_DIR, or None if not applicable."""
    base_dir = _base_dir()
    if base_dir is None:
        return None
    try:
        repo_root = _loader.repo_root_from_path(path)
    except ValueError:
        return None
    return (base_dir / path.resolve().relative_to(repo_root.resolve())).resolve()


def _inferred_repo_root(path: Path) -> Path | None:
    """Repo root of a path via a configs/, artifacts/ or src/ ancestor."""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name in _REPO_MARKERS:
            return candidate.parent.resolve()
    try:
        return _loader.repo_root_from_path(path)
    except ValueError:
        return None


def _remap_to_repo(path: Path, target_root: Path, source_root: Path | None = None) -> Path | None:
    """Rewrite path from source_root (or its inferred repo root) to the same location under target_root."""
    resolved, target_root = path.resolve(), target_root.resolve()
    roots = [root.resolve() for root in (source_root, _inferred_repo_root(path)) if root is not None]
    for root in dict.fromkeys(roots):
        if root == target_root:
            continue
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        return (target_root / relative).resolve()
    return None


def resolve_config_path(config: str) -> Path:
    candidate = Path(config).expanduser()
    remapped = _remap_to_base_dir(candidate)
    if remapped is not None and candidate.is_absolute():
        return remapped
    if candidate.exists():
        return candidate.resolve()
    return remapped if remapped is not None else candidate.resolve()


def repo_root_from_config(config: str) -> Path:
    return _loader.repo_root_from_path(resolve_config_path(config))


def resolve_repo_relative_path(config: str, value: str, default: Path) -> Path:
    candidate = Path(value).expanduser() if value else Path(default)
    if candidate.is_absolute():
        return _remap_to_base_dir(candidate) or candidate.resolve()
    return (repo_root_from_config(config) / candidate).resolve()


def _config_location(config: str) -> tuple[Path, str]:
    config_path = resolve_config_path(config)
    return _loader.relative_config_location(config_path, _loader.repo_root_from_path(config_path))


def step_plan_path(config: str, plan_root: str = str(DEFAULT_PLANS_ROOT)) -> Path:
    relative_config_dir, sample_name = _config_location(config)
    return resolve_repo_relative_path(config, plan_root, DEFAULT_PLANS_ROOT) / relative_config_dir / f"{sample_name}.json"


def sample_download_root(config: str, driver_root: str = str(DEFAULT_DRIVER_ROOT)) -> Path:
    relative_config_dir, sample_name = _config_location(config)
    return resolve_repo_relative_path(config, driver_root, DEFAULT_DRIVER_ROOT) / relative_config_dir / sample_name


def _submitter_repo_root(payload: dict[str, Any], config: str) -> Path | None:
    """Repo root the plan was built in, anchored on any absolute fragment path it carries."""
    for step in payload.get("steps", []):
        for command in step.get("commands", []):
            if command.get("fragment_path"):
                root = _inferred_repo_root(Path(command["fragment_path"]).expanduser())
                if root is not None:
                    return root
    return _inferred_repo_root(Path(config).expanduser())


def _resolve_plan_path(value: str, remote_root: Path, submitter_root: Path | None) -> str:
    """Repo-relative values join the worker repo; absolute ones are remapped when possible."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        return str((remote_root / path).resolve())
    return str(_remap_to_repo(path, remote_root, submitter_root) or path)


def load_step_plan(config: str, plan_root: str = str(DEFAULT_PLANS_ROOT)) -> dict[str, Any]:
    payload = json.loads(step_plan_path(config, plan_root).read_text(encoding="utf-8"))
    submitter_root = _submitter_repo_root(payload, config)
    remote_root = repo_root_from_config(config)
    for step in payload.get("steps", []):
        for command in step.get("commands", []):
            if command.get("fragment_path"):
                remapped = _remap_to_repo(Path(command["fragment_path"]).expanduser(), remote_root, submitter_root)
                if remapped is not None:
                    command["fragment_path"] = str(remapped)
            if command.get("card"):
                command["card"] = _resolve_plan_path(command["card"], remote_root, submitter_root)
            for package in command.get("extra_cmssw_packages", []):
                if package.get("source"):
                    package["source"] = _resolve_plan_path(package["source"], remote_root, submitter_root)
    StepPlanBuilder.validate_plan_payload(payload)
    return payload


def config_step(config: str, step_name_or_slug: str) -> dict[str, Any]:
    for step in _loader.load(resolve_config_path(config))["steps"]:
        if step_name_or_slug in (step["name"], step_slug(step["name"])):
            return step
    raise KeyError(f"Unknown config step {step_name_or_slug!r}")
