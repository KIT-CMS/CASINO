from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from application.config_generator import render_law_command
from domain.configuration import ConfigLoader, is_pseudo_stage
from domain.defaults import load_defaults


@dataclass(frozen=True)
class ConfigSummary:
    name: str
    abs_path: Path
    mode: str
    total_events: int
    n_steps: int
    law_command: str


def available_years(repo_root) -> list[tuple[str, int]]:
    """Sorted (configs/<rel-dir>, count) pairs for every directory holding configs."""
    repo_root = Path(repo_root)
    loader = ConfigLoader()
    counts: dict[str, int] = {}
    for path in loader.config_paths(repo_root):
        rel_dir, _ = loader.relative_config_location(path, repo_root)
        counts[rel_dir.as_posix()] = counts.get(rel_dir.as_posix(), 0) + 1
    return sorted(counts.items())


def configs_for_year(repo_root, rel_dir) -> list[ConfigSummary]:
    repo_root = Path(repo_root)
    loader = ConfigLoader()
    extra_args = load_defaults(repo_root).htcondor_extra_args
    summaries = []
    for path in sorted((repo_root / "configs" / rel_dir).glob("*.yaml")):
        config = loader.load(path)
        is_delphes = any(is_pseudo_stage(token) for step in config["steps"] for token in step["setup_ids"])
        summaries.append(ConfigSummary(
            name=path.stem,
            abs_path=path,
            mode="delphes" if is_delphes else "full-chain",
            total_events=config["total_events"],
            n_steps=len(config["steps"]),
            law_command=render_law_command(f"$PWD/{path.resolve().relative_to(repo_root.resolve())}", "htcondor", extra_args),
        ))
    return summaries
