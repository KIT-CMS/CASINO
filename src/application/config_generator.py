from __future__ import annotations

import re
from pathlib import Path

from domain.chain import ResolvedChain
from domain.defaults import Defaults

# Longest prefix first so RunIII is not mistaken for RunII.
_ERA_PREFIXES = [("RunIII", "run3"), ("RunIV", "run4"), ("RunII", "run2"), ("Run4", "run4")]


def render_law_command(config_ref: str, workflow: str, htcondor_extra_args: str = "") -> str:
    command = f'poetry run law run RunSample --config "{config_ref}" --workflow {workflow}'
    if workflow == "htcondor" and htcondor_extra_args:
        command += f" {htcondor_extra_args}"
    return command


def parse_campaign_location(campaign: str) -> tuple[str | None, str | None]:
    """(era, year) directory components of a McM campaign name; either may be None."""
    era = next((name for prefix, name in _ERA_PREFIXES if campaign.startswith(prefix)), None)
    year = re.search(r"\d{4}", campaign)
    return era, (year.group(0) if year else None)


def build_config(chain: ResolvedChain, *, mode, total_events, cores, memory,
                 events_per_job_mini, events_per_job_nano, defaults: Defaults,
                 card=None, output_root=None, gridpack=None, include_delphes_nano: bool = True) -> dict:
    def prep(kind: str) -> str:
        step = chain.step(kind)
        if step is None:
            raise ValueError(f"chain missing required {kind} step")
        return step.prep_id

    def generator_prep() -> str:
        step = chain.step("wmLHEGS") or chain.step("GS")
        if step is None:
            raise ValueError("chain missing required generator step (wmLHEGS or GS)")
        return step.prep_id

    def step(name: str, filename: str, setup_ids: list[str], events_per_job: int) -> dict:
        return {"name": name, "filename": filename, "setup_ids": setup_ids,
                "events_per_job": events_per_job, "cores_per_job": cores, "memory_per_job": memory}

    base = output_root or defaults.base_output_path
    if mode == "delphes":
        if not card:
            raise ValueError("Delphes mode requires a card path")
        base = f"{base.rstrip('/')}/{defaults.delphes_output_subdir}"
        steps = [step("delphes-tree", "delphes-tree", [generator_prep(), f"delphes:{card}"], events_per_job_mini)]
        if include_delphes_nano:
            steps.append(step("delphes-nano", "delphes-nano", ["delphes-nano"], events_per_job_nano))
    elif mode == "full-chain":
        steps = [
            step("MiniAODv6", "miniAODv6", [generator_prep(), prep("DRPremix"), prep("MiniAOD")], events_per_job_mini),
            step("NanoAODv15", "nanoAODv15", [prep("NanoAOD")], events_per_job_nano),
        ]
    else:
        raise ValueError(f"Unknown mode {mode!r}")

    config: dict = {"total_events": total_events, "base_output_path": base}
    if gridpack:
        config["gridpack"] = gridpack
    config["steps"] = steps
    return config


def dump_config(config: dict) -> str:
    lines = [f"total_events: {config['total_events']}", f"base_output_path: {config['base_output_path']}"]
    if config.get("gridpack"):
        lines.append(f"gridpack: {config['gridpack']}")
    lines.append("steps:")
    for step in config["steps"]:
        lines += [f"- name: {step['name']}", f"  filename: {step['filename']}", "  setup_ids:"]
        lines += [f"  - {setup_id}" for setup_id in step["setup_ids"]]
        lines += [
            f"  events_per_job: {step['events_per_job']}",
            f"  cores_per_job: {step['cores_per_job']}",
            f"  memory_per_job: {step['memory_per_job']} # in GB",
        ]
    return "\n".join(lines) + "\n"


class ConfigWriter:
    def write(self, config: dict, path, *, force: bool = False) -> Path:
        path = Path(path)
        if path.exists() and not force:
            raise FileExistsError(f"Config already exists: {path} (use force/--force to overwrite)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_config(config), encoding="utf-8")
        return path
