from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from domain.configuration import ConfigLoader
from infrastructure.artifact_locator import ArtifactLocator
from infrastructure.console import eprint
from infrastructure.downloader import DownloadAllResult, DownloadManager
from infrastructure.filesystem import write_json
from infrastructure.gridpack_checker import GfalGridpackChecker
from infrastructure.gridpack_fragment import CustomGridpackFragmentManager
from infrastructure.mcm import fetch_text
from infrastructure.setup_script_parser import SetupScriptParser
from .step_plan_builder import StepPlanBuilder


ARTIFACTS_ROOT = Path("artifacts")
DEFAULT_DRIVER_ROOT = ARTIFACTS_ROOT / "mcm-commands"
DEFAULT_PLANS_ROOT = ARTIFACTS_ROOT / "step-plans"


@dataclass(slots=True)
class BuildStepPlansResult:
    built: int = 0
    skipped: int = 0
    failed: int = 0


def _log_gridpack(gridpack_uri: str | None, check_gridpack: bool, existing_plan: bool = False) -> None:
    if not gridpack_uri:
        return
    if existing_plan:
        eprint(f"Custom gridpack found in existing step plan: {gridpack_uri}")
    elif check_gridpack:
        eprint(f"Custom gridpack found and accessible: {gridpack_uri}")
    else:
        eprint(f"Custom gridpack found in config: {gridpack_uri}")


class CasinoApplication:
    """Single wiring point shared by the CLI and the law tasks."""

    def __init__(self, fetcher: Callable[[str, int, bool], str] = fetch_text, gridpack_checker=None) -> None:
        self.config_loader = ConfigLoader()
        self.artifact_locator = ArtifactLocator(self.config_loader)
        self.script_parser = SetupScriptParser()
        self.fragment_manager = CustomGridpackFragmentManager(self.config_loader)
        self.gridpack_checker = gridpack_checker or GfalGridpackChecker()
        self.plan_builder = StepPlanBuilder(
            self.config_loader, self.artifact_locator, self.script_parser, self.fragment_manager, self.gridpack_checker,
        )
        self.downloader = DownloadManager(self.config_loader, fetcher=fetcher)

    def download(self, config_path: Path, driver_root: Path, timeout: int, overwrite: bool, verify_certs: bool) -> Path:
        return self.downloader.download(config_path, driver_root, timeout, overwrite, verify_certs)

    def download_all(
        self, repo_root: Path, driver_root: Path, timeout: int, overwrite: bool, verify_certs: bool,
    ) -> DownloadAllResult:
        return self.downloader.download_all(repo_root, driver_root, timeout, overwrite, verify_certs)

    def build_step_plans_all(self, repo_root: Path, driver_root: Path, overwrite: bool) -> BuildStepPlansResult:
        result = BuildStepPlansResult()
        for config_path in self.config_loader.config_paths(repo_root):
            try:
                wrote_plan = self.build_step_runners_for_config(config_path, repo_root, driver_root, overwrite)
            except Exception as exc:
                result.failed += 1
                eprint(f"Failed {config_path}: {exc}")
                continue
            if wrote_plan:
                result.built += 1
            else:
                result.skipped += 1
        eprint(f"Build-step-runners summary: built={result.built} skipped={result.skipped} failed={result.failed}")
        return result

    def build_step_runners_for_config(
        self, config_path: Path, repo_root: Path, driver_root: Path, overwrite: bool, check_gridpack: bool = True,
    ) -> bool:
        """Write the step plan for one config; returns False when a valid plan already exists."""
        relative_config_dir, sample_name = self.config_loader.relative_config_location(config_path, repo_root)
        plan_path = repo_root / DEFAULT_PLANS_ROOT / relative_config_dir / f"{sample_name}.json"
        if plan_path.exists() and not overwrite:
            try:
                existing_plan = json.loads(plan_path.read_text(encoding="utf-8"))
                StepPlanBuilder.validate_plan_payload(existing_plan)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                eprint(f"Rebuilding incompatible step plan at {plan_path}: {exc}")
            else:
                _log_gridpack(existing_plan.get("gridpack"), check_gridpack, existing_plan=True)
                eprint(f"Skipped existing step plan at {plan_path}")
                return False

        plan = self.plan_builder.build(
            config_path, repo_root, driver_root, overwrite_custom_fragment=overwrite, check_gridpack=check_gridpack,
        )
        write_json(plan_path, plan, overwrite=True)
        _log_gridpack(plan.get("gridpack"), check_gridpack)
        eprint(f"Wrote step plan to {plan_path}")
        return True

    def main(self, argv: list[str] | None = None) -> int:
        from cli import run_cli

        return run_cli(argv, app=self)
