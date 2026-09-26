from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from application import DEFAULT_DRIVER_ROOT, DEFAULT_PLANS_ROOT
from casino import create_application
from domain.configuration import step_slug

from . import storage
from .compat import force_explicit_bool_parsing, import_law_dependencies
from .htcondor import DEFAULT_HTCONDOR_REQUIREMENTS, CasinoCMSSWHTCondorWorkflow
from .runtime import (
    DEFAULT_CONTAINER_RUNTIME,
    DEFAULT_LOCAL_CONTAINER_IMAGE,
    PreparedCommand,
    build_branch_execution_plan,
    build_cmssw_command_script,
    build_lcg_command_script,
    build_local_container_command,
    build_output_target,
    find_step,
    required_bind_paths,
    resolve_output_backend,
    resolve_output_root,
    step_output_dir,
)
from .storage import CasinoTargetCollection
from .task_support import (
    load_step_plan,
    repo_root_from_config,
    resolve_config_path,
    resolve_repo_relative_path,
    sample_download_root,
    step_plan_path,
)

__all__ = ["CasinoTask", "FetchMcMArtifacts", "BuildStepPlan", "RunSampleStep", "RunSample", "PreparedCommand"]

law, luigi = import_law_dependencies()
law.contrib.load("tasks", "wlcg")
logger = law.logger.get_logger(__name__)
_LAW_TASK_ID_HASH_RE = re.compile(r"_([0-9a-f]{8,})$")


def _duration_parameter(**kwargs):
    cls = getattr(law, "DurationParameter", None)
    if cls is None and hasattr(law, "__path__"):
        try:
            from law.parameter import DurationParameter as cls
        except ImportError:
            cls = None
    if cls is None:
        kwargs.pop("unit", None)
        return luigi.Parameter(**kwargs)
    return cls(**kwargs)


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def _task_id_hash(task_id: str) -> str:
    match = _LAW_TASK_ID_HASH_RE.search(task_id)
    return match.group(1) if match else law.util.create_hash(task_id)


class CasinoTask(law.Task):
    config = luigi.Parameter(description="Path to the sample config YAML file")
    driver_root = luigi.Parameter(default=str(DEFAULT_DRIVER_ROOT), significant=False)
    plan_root = luigi.Parameter(default=str(DEFAULT_PLANS_ROOT), significant=False)
    output_root = luigi.Parameter(default="", significant=False)
    output_backend = luigi.Parameter(default="auto", significant=False)
    overwrite = luigi.BoolParameter(default=False, significant=False)
    skip_check_gridpack = luigi.BoolParameter(default=False, significant=False)

    def app(self):
        return create_application()

    def resolved_config_path(self) -> Path:
        return resolve_config_path(self.config)

    def resolved_repo_root(self) -> Path:
        return repo_root_from_config(self.config)

    def resolved_driver_root(self) -> Path:
        return resolve_repo_relative_path(self.config, self.driver_root, DEFAULT_DRIVER_ROOT)

    def load_plan(self) -> dict:
        return load_step_plan(self.config, self.plan_root)

    def target_from_spec(self, path: str, backend: str, output_root: str | None = None):
        """law target for an OutputTargetSpec path; WLCG targets are pinned to output_root's filesystem."""
        if backend == "posix":
            return law.LocalFileTarget(path)
        fs = storage.wlcg_fs_for(output_root)
        return law.wlcg.WLCGFileTarget(path) if fs is None else law.wlcg.WLCGFileTarget(path, fs=fs)


class FetchMcMArtifacts(CasinoTask):
    timeout = luigi.IntParameter(default=30, significant=False)
    verify_certs = luigi.BoolParameter(default=False, significant=False)

    def complete(self):
        try:
            return self.app().downloader.has_download_entry(
                self.resolved_config_path(), self.resolved_repo_root(), self.resolved_driver_root(),
            )
        except (FileNotFoundError, ValueError):
            return False

    def output(self):
        return law.LocalDirectoryTarget(str(sample_download_root(self.config, self.driver_root)))

    def run(self):
        self.app().download(
            self.resolved_config_path(), self.resolved_driver_root(), self.timeout, self.overwrite, self.verify_certs,
        )


class BuildStepPlan(CasinoTask):
    def requires(self):
        return FetchMcMArtifacts.req(self)

    def complete(self):
        try:
            return self.output().exists() and bool(self.load_plan())
        except (FileNotFoundError, ValueError):
            return False

    def output(self):
        return law.LocalFileTarget(str(step_plan_path(self.config, self.plan_root)))

    def run(self):
        self.app().build_step_runners_for_config(
            config_path=self.resolved_config_path(),
            repo_root=self.resolved_repo_root(),
            driver_root=self.resolved_driver_root(),
            overwrite=self.overwrite,
            check_gridpack=not self.skip_check_gridpack,
        )


class RunSampleStep(law.LocalWorkflow, CasinoCMSSWHTCondorWorkflow, CasinoTask):
    """One branch per CMSSW job of a logical step; chained to its predecessor step."""

    output_collection_cls = CasinoTargetCollection

    step = luigi.Parameter(description="Logical step name or slug")
    container_runtime = luigi.Parameter(default=DEFAULT_CONTAINER_RUNTIME, significant=False)
    local_container_image = luigi.Parameter(default=DEFAULT_LOCAL_CONTAINER_IMAGE, significant=False)

    def _plan_from_config(self) -> dict:
        """Plan-shaped view of the sample config, used before build-step-runners has produced the plan."""
        config = self.app().config_loader.load(self.resolved_config_path())
        steps: list[dict] = []
        for cfg in config["steps"]:
            slug = step_slug(cfg["name"])
            steps.append({
                "name": cfg["name"],
                "slug": slug,
                "filename": cfg.get("filename") or slug,
                "events_per_job": cfg["events_per_job"],
                "cores_per_job": cfg["cores_per_job"],
                "memory_per_job": cfg["memory_per_job"],
                "job_count": math.ceil(config["total_events"] / cfg["events_per_job"]),
                "predecessor_step": steps[-1]["slug"] if steps else None,
            })
        return {
            "sample_name": self.resolved_config_path().stem,
            "base_output_path": str(config["base_output_path"]),
            "total_events": config["total_events"],
            "steps": steps,
        }

    def _plan_or_config(self) -> dict:
        try:
            return self.load_plan()
        except (FileNotFoundError, ValueError):
            return self._plan_from_config()

    def _step(self) -> dict:
        return find_step(self._plan_or_config(), self.step)

    def _layout(self) -> tuple[dict, dict, str, str]:
        """(plan, step, output_root, output_backend)"""
        plan = self._plan_or_config()
        output_root = resolve_output_root(plan, self.output_root)
        return plan, find_step(plan, self.step), output_root, resolve_output_backend(output_root, self.output_backend)

    def htcondor_output_directory_name(self) -> str:
        try:
            step_name = self._step()["name"]
        except (FileNotFoundError, KeyError, ValueError):
            step_name = str(self.step)
        parts = [
            "RunSampleStep",
            _safe_component(step_name),
            _safe_component(self.resolved_config_path().stem),
            _task_id_hash(self.task_id),
        ]
        return "_".join(part for part in parts if part)

    def _request_cpus(self) -> int:
        configured = int(self.htcondor_request_cpus)
        return configured if configured > 0 else int(self._step()["cores_per_job"])

    def _request_memory(self) -> str:
        configured = float(self.htcondor_request_memory)
        if configured > 0:
            return str(int(configured))
        return str(int(float(self._step()["memory_per_job"]) * 1000))

    def _request_disk(self) -> str:
        configured = float(self.htcondor_request_disk)
        if configured > 0:
            return str(int(configured))
        return str(self._derived_disk_kb(int(self._step()["events_per_job"])))

    def create_branch_map(self):
        return {branch: branch for branch in range(int(self._step()["job_count"]))}

    def workflow_requires(self):
        reqs = {"plan": BuildStepPlan.req(self)}
        predecessor = self._step().get("predecessor_step")
        if predecessor:
            reqs["upstream"] = RunSampleStep.req(self, step=predecessor)
        return reqs

    def requires(self):
        reqs = {"plan": BuildStepPlan.req(self)}
        # The submitter verified upstream completeness; re-walking it on the worker only costs WLCG stats.
        if not self._is_remote_job():
            predecessor = self._step().get("predecessor_step")
            if predecessor:
                reqs["upstream"] = RunSampleStep.req(self, step=predecessor, branch=-1)
        return reqs

    def complete(self):
        if getattr(self, "branch", -1) >= 0:
            return storage.target_exists(self.output())
        return self._branch_outputs_complete()

    def workflow_complete(self):
        return self._branch_outputs_complete()

    def _branch_outputs_complete(self) -> bool:
        try:
            plan, step, output_root, backend = self._layout()
        except (FileNotFoundError, ValueError):
            return False
        branches = self.get_branch_map() if hasattr(self, "get_branch_map") else range(int(step["job_count"]))
        specs = [build_output_target(plan, step, branch, output_root, backend) for branch in branches]
        if backend != "posix" and specs:
            # All branch outputs share one directory: one listing instead of one stat per branch.
            listing = storage.dir_listing(self.target_from_spec(specs[0].path, specs[0].backend, output_root))
            if listing is not None:
                return all(os.path.basename(spec.path) in listing for spec in specs)
        return all(storage.target_exists(self.target_from_spec(spec.path, spec.backend, output_root)) for spec in specs)

    def output(self):
        plan, step, output_root, backend = self._layout()
        if getattr(self, "branch", -1) < 0:
            relative_dir = f"{plan['sample_name']}/{step_output_dir(step, plan['total_events'])}"
            if backend == "posix":
                return law.LocalDirectoryTarget(str(Path(output_root) / relative_dir))
            directory_cls = getattr(law.wlcg, "WLCGDirectoryTarget", law.wlcg.WLCGFileTarget)
            fs = storage.wlcg_fs_for(output_root)
            return directory_cls(relative_dir) if fs is None else directory_cls(relative_dir, fs=fs)
        spec = build_output_target(plan, step, self.branch, output_root, backend)
        return self.target_from_spec(spec.path, spec.backend, output_root)

    @staticmethod
    def _is_remote_job() -> bool:
        return bool(os.environ.get("CASINO_CMSSW_CACHE_DIR"))  # exported by bootstrap_cmssw_job.sh

    def run(self):
        plan = self.load_plan()
        step = find_step(plan, self.step)
        output_root = resolve_output_root(plan, self.output_root)
        output_backend = resolve_output_backend(output_root, self.output_backend)
        is_remote = self._is_remote_job()

        with tempfile.TemporaryDirectory(prefix=f"casino-{step['slug']}-{self.branch}-") as tmpdir:
            scratch_dir = Path(tmpdir)
            execution = build_branch_execution_plan(
                plan=plan,
                step_name_or_slug=self.step,
                branch=self.branch,
                scratch_dir=scratch_dir,
                output_root_override=self.output_root,
                output_backend_request=self.output_backend,
            )
            if is_remote:
                cmssw_cache_dir = Path(os.environ["CASINO_CMSSW_CACHE_DIR"]).resolve()
            else:
                cmssw_cache_dir = scratch_dir / "cmssw-cache"
                bind_paths = required_bind_paths(
                    repo_root=self.resolved_repo_root(),
                    scratch_dir=scratch_dir,
                    cmssw_cache_dir=cmssw_cache_dir,
                    output_root=output_root,
                    output_backend=output_backend,
                    input_specs=list(execution.external_inputs),
                    fragment_paths=[c.fragment_path for c in execution.commands if c.fragment_path],
                )

            for command in execution.commands:
                if command.kind in ("delphes", "nano"):
                    script = build_lcg_command_script(
                        command=command, scratch_dir=scratch_dir, lcg_setup=str(self.lcg_stack),
                        repo_src=self.resolved_repo_root() / "src",
                    )
                else:
                    script = build_cmssw_command_script(
                        command=command, scratch_dir=scratch_dir, cmssw_cache_dir=cmssw_cache_dir,
                        compile_cores=step["cores_per_job"],
                    )
                if is_remote:
                    argv = ["/bin/bash", "-lc", script]  # already inside the worker container
                else:
                    argv = build_local_container_command(
                        runtime=self.container_runtime, image=self.local_container_image,
                        bind_paths=bind_paths, shell_script=script,
                    )
                subprocess.run(argv, check=True, cwd=scratch_dir)

            storage.stage_out(self.output(), Path(execution.commands[-1].local_output_path))


class RunSample(law.WrapperTask, CasinoTask):
    workflow = luigi.Parameter(default="local", significant=False)
    poll_interval = _duration_parameter(
        default=10, unit="m", significant=False,
        description="Interval between RunSampleStep job status polls. Default unit is minutes.",
    )
    htcondor_requirements = luigi.Parameter(
        default=DEFAULT_HTCONDOR_REQUIREMENTS, significant=False,
        description="Additional target machine requirements forwarded to RunSampleStep.",
    )

    def requires(self):
        config = self.app().config_loader.load(self.resolved_config_path())
        return RunSampleStep.req(
            self,
            step=config["steps"][-1]["name"],
            workflow=self.workflow,
            poll_interval=self.poll_interval,
            htcondor_requirements=self.htcondor_requirements,
        )


# Our own BoolParameters are defined after compat ran, so flip them to EXPLICIT_PARSING too.
force_explicit_bool_parsing(law, luigi)
