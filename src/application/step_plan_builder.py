from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.cmsdriver import RUNTIME_MANAGED_OPTIONS, basename_from_file_spec
from domain.configuration import ConfigLoader, classify_setup_id, step_slug
from infrastructure.artifact_locator import ArtifactLocator
from infrastructure.gridpack_fragment import CustomGridpackFragmentManager
from infrastructure.setup_script_parser import SetupScriptParser


def _truncate_step_to_gen(step_value: str) -> str:
    kept: list[str] = []
    for token in step_value.split(","):
        kept.append(token)
        if token.split(":", 1)[0] == "GEN":
            break
    return ",".join(kept)


def _set_option(options: list[dict], name: str, value: str) -> None:
    for option in options:
        if option["name"] == name:
            option["value"] = value
            return
    options.append({"name": name, "value": value})


def _option_value(options: list[dict], name: str) -> str | None:
    return next((o["value"] for o in options if o["name"] == name), None)


@dataclass(frozen=True)
class _McMContext:
    config_path: Path
    repo_root: Path
    gridpack_uri: str | None
    sample_directories: list[Path]
    overwrite_custom_fragment: bool


class StepPlanBuilder:
    """Turns a sample config plus downloaded McM scripts into the versioned step plan JSON."""

    # v2: per-step `filename`, flat remote layout; v3: top-level `gridpack`; v4: per-command `kind`.
    PLAN_VERSION = 4
    REQUIRED_TOP_LEVEL_FIELDS = {"plan_version", "sample_name", "base_output_path", "total_events", "gridpack", "steps"}
    REQUIRED_STEP_FIELDS = {
        "step_index", "name", "slug", "filename", "events_per_job", "cores_per_job", "memory_per_job",
        "job_count", "predecessor_step", "commands", "final_output_name",
    }
    _CMSSW_FIELDS = {
        "command_index", "scram_arch", "cmssw_release", "fragment_path", "fragment_target",
        "options", "filein", "fileout", "python_filename",
    }
    REQUIRED_COMMAND_FIELDS = {
        "cmssw": _CMSSW_FIELDS,
        "gen": _CMSSW_FIELDS,
        "delphes": {"command_index", "kind", "card", "delphes_executable", "filein", "fileout"},
        "nano": {"command_index", "kind", "filein", "fileout"},
    }
    HEPMC_WRITER_PACKAGE_REPO_PATH = "cmssw/Casino/HepMCWriter"
    HEPMC_WRITER_PACKAGE_DEST = "Casino/HepMCWriter"
    HEPMC_WRITER_CUSTOMISE = "Casino/HepMCWriter/customise.dumpHepMC"
    HEPMC_OUTPUT_NAME = "gen.hepmc"
    DELPHES_EXECUTABLE = "DelphesHepMC2"

    def __init__(
        self,
        config_loader: ConfigLoader,
        artifact_locator: ArtifactLocator,
        script_parser: SetupScriptParser,
        fragment_manager: CustomGridpackFragmentManager,
        gridpack_checker,
    ) -> None:
        self.config_loader = config_loader
        self.artifact_locator = artifact_locator
        self.script_parser = script_parser
        self.fragment_manager = fragment_manager
        self.gridpack_checker = gridpack_checker

    @classmethod
    def validate_plan_payload(cls, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Step plan payload must be a JSON object")
        if payload.get("plan_version") != cls.PLAN_VERSION:
            raise ValueError(
                "Step plan is outdated or incompatible: "
                f"expected plan_version={cls.PLAN_VERSION}, got {payload.get('plan_version')!r}"
            )
        missing = cls.REQUIRED_TOP_LEVEL_FIELDS - payload.keys()
        if missing:
            raise ValueError(f"Step plan is missing required top-level fields: {sorted(missing)}")
        if not isinstance(payload["steps"], list) or not payload["steps"]:
            raise ValueError("Step plan must contain at least one step")
        for index, step in enumerate(payload["steps"]):
            cls._validate_step(index, step)

    @classmethod
    def _validate_step(cls, index: int, step: Any) -> None:
        if not isinstance(step, dict):
            raise ValueError(f"Step plan step {index} must be a JSON object")
        name = step.get("name", f"step-{index}")
        missing = cls.REQUIRED_STEP_FIELDS - step.keys()
        if missing:
            raise ValueError(f"Step plan step {index} is missing required fields: {sorted(missing)}")
        if not isinstance(step["commands"], list) or not step["commands"]:
            raise ValueError(f"Step plan step {name} must contain at least one command")
        for command_index, command in enumerate(step["commands"]):
            label = f"Step plan command {command_index} in step {name}"
            if not isinstance(command, dict):
                raise ValueError(f"{label} must be a JSON object")
            kind = command.get("kind", "cmssw")
            required = cls.REQUIRED_COMMAND_FIELDS.get(kind)
            if required is None:
                raise ValueError(f"{label} has unknown kind {kind!r}; valid values are cmssw, gen, delphes, nano")
            missing = required - command.keys()
            if missing:
                raise ValueError(f"{label} is missing required fields: {sorted(missing)}")
            if kind in ("cmssw", "gen"):
                cls._validate_options(label, command["options"])

    @staticmethod
    def _validate_options(label: str, options: Any) -> None:
        if not isinstance(options, list):
            raise ValueError(f"{label} must define options as a list")
        if not all(isinstance(option, dict) for option in options):
            raise ValueError(f"{label} must define each option as a JSON object")
        forbidden = sorted(o["name"] for o in options if o.get("name") in RUNTIME_MANAGED_OPTIONS)
        if forbidden:
            raise ValueError(f"{label} contains runtime-managed options: {forbidden}")

    def build(
        self,
        config_path: Path,
        repo_root: Path,
        driver_root: Path,
        overwrite_custom_fragment: bool = False,
        check_gridpack: bool = True,
    ) -> dict[str, Any]:
        config = self.config_loader.load(config_path)
        gridpack_uri = config.get("gridpack")
        if gridpack_uri and check_gridpack:
            self.gridpack_checker.ensure_exists(gridpack_uri)
        _, sample_name = self.config_loader.relative_config_location(config_path, repo_root)
        sample_directories = self.artifact_locator.candidate_sample_directories(config_path, repo_root, driver_root)
        if not sample_directories:
            raise FileNotFoundError("Could not locate any matching driver-command directory. Run the download command first.")
        context = _McMContext(config_path, repo_root, gridpack_uri, sample_directories, overwrite_custom_fragment)

        steps: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for step_index, step in enumerate(config["steps"]):
            commands = self._step_commands(step, previous, context)
            planned = self._planned_step(step_index, step, previous, commands, config["total_events"])
            steps.append(planned)
            previous = planned

        return {
            "plan_version": self.PLAN_VERSION,
            "sample_name": sample_name,
            "base_output_path": str(config["base_output_path"]),
            "total_events": config["total_events"],
            "gridpack": gridpack_uri,
            "steps": steps,
        }

    @staticmethod
    def _filename_stem(step: dict[str, Any]) -> str:
        return str(step.get("filename") or step_slug(step["name"]))

    def _planned_step(self, step_index, step, previous, commands, total_events) -> dict[str, Any]:
        if previous is not None and step["events_per_job"] % previous["events_per_job"] != 0:
            raise ValueError(
                f"Step {step['name']} has events_per_job={step['events_per_job']} which is not a whole-number "
                f"multiple of the predecessor step {previous['name']} with events_per_job={previous['events_per_job']}"
            )
        return {
            "step_index": step_index,
            "name": step["name"],
            "slug": step_slug(step["name"]),
            "filename": self._filename_stem(step),
            "events_per_job": step["events_per_job"],
            "cores_per_job": step["cores_per_job"],
            "memory_per_job": step["memory_per_job"],
            "job_count": math.ceil(total_events / step["events_per_job"]),
            "predecessor_step": previous["slug"] if previous else None,
            "setup_ids": step.get("setup_ids", []),
            "commands": commands,
            "final_output_name": basename_from_file_spec(commands[-1]["fileout"]),
        }

    def _step_commands(self, step, previous, context: _McMContext) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        for token in step.get("setup_ids", []):
            kind, card = classify_setup_id(token)
            if kind == "delphes":
                commands.append(self._delphes_command(step, commands, card))
            elif kind == "nano":
                commands.append(self._nano_command(step, previous, len(commands)))
            else:
                commands.extend(self._mcm_commands(token, context, len(commands)))
        if not commands:
            raise ValueError(f"Step {step['name']} did not produce any executable commands")
        return commands

    def _delphes_command(self, step, commands, card) -> dict[str, Any]:
        """Truncate the preceding gen command to LHE,GEN + HepMC dump, then run Delphes on it."""
        if not commands:
            raise ValueError(f"Step {step['name']}: 'delphes' stage has no preceding gen command")
        if not card:
            raise ValueError(f"Step {step['name']}: 'delphes' stage must encode a card path")
        gen = commands[-1]
        options = gen["options"]
        step_value = _option_value(options, "--step")
        if not step_value:
            raise ValueError(f"Step {step['name']}: gen command has no --step to truncate")
        gen["kind"] = "gen"
        _set_option(options, "--step", _truncate_step_to_gen(step_value))
        _set_option(options, "--eventcontent", "RAWSIM")
        _set_option(options, "--datatier", "GEN")
        if not any(o.get("name") == "--customise" and o.get("value") == self.HEPMC_WRITER_CUSTOMISE for o in options):
            options.append({"name": "--customise", "value": self.HEPMC_WRITER_CUSTOMISE})
        gen["hepmc_output"] = self.HEPMC_OUTPUT_NAME
        # Repo-relative; load_step_plan resolves it against the worker repo root.
        gen["extra_cmssw_packages"] = [{"source": self.HEPMC_WRITER_PACKAGE_REPO_PATH, "dest": self.HEPMC_WRITER_PACKAGE_DEST}]
        return {
            "command_index": len(commands),
            "kind": "delphes",
            "card": card,
            "delphes_executable": self.DELPHES_EXECUTABLE,
            "filein": self.HEPMC_OUTPUT_NAME,
            "fileout": f"{self._filename_stem(step)}.root",
        }

    def _nano_command(self, step, previous, command_index: int) -> dict[str, Any]:
        if previous is None:
            raise ValueError(f"Step {step['name']}: 'delphes-nano' requires a predecessor delphes step")
        return {
            "command_index": command_index,
            "kind": "nano",
            "filein": previous["final_output_name"],
            "fileout": f"{self._filename_stem(step)}.root",
        }

    def _mcm_commands(self, setup_id: str, context: _McMContext, first_index: int) -> list[dict[str, Any]]:
        script_path = self.artifact_locator.find_setup_script(setup_id, context.sample_directories)
        fragment_path = self.artifact_locator.find_fragment_file(setup_id, context.sample_directories, script_path=script_path)
        if context.gridpack_uri and fragment_path is not None:
            fragment_path = self.fragment_manager.ensure_custom_fragment(
                source_fragment_path=fragment_path,
                config_path=context.config_path,
                repo_root=context.repo_root,
                gridpack_uri=context.gridpack_uri,
                overwrite=context.overwrite_custom_fragment,
            )
        parsed = self.script_parser.parse_setup_script(script_path, fragment_path)
        commands = []
        for offset, command in enumerate(parsed["commands"]):
            if command["fragment_argument"] and not parsed["fragment_path"]:
                raise FileNotFoundError(
                    f"Could not locate fragment file {command['fragment_argument']!r} required by setup {setup_id}"
                )
            commands.append({
                "kind": "cmssw",
                "scram_arch": parsed["scram_arch"],
                "cmssw_release": parsed["cmssw_release"],
                "fragment_path": parsed["fragment_path"] if command["fragment_argument"] else None,
                "fragment_target": command["fragment_argument"],
                "command_index": first_index + offset,
                "options": command["options"],
                "filein": command["filein"],
                "fileout": command["fileout"],
                "python_filename": command["python_filename"],
            })
        return commands
