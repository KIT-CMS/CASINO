from __future__ import annotations

import math
import os

from .compat import import_law_dependencies
from .task_support import repo_root_from_config

law, luigi = import_law_dependencies()
law.contrib.load("htcondor", "tasks", "wlcg", "git")
logger = law.logger.get_logger(__name__)

DEFAULT_HTCONDOR_REQUIREMENTS = "(TARGET.ProvidesCPU)&&(TARGET.ProvidesIO)"
# The pool hands a 2-core job ~50 GB of scratch at most; larger requests stop matching idle nodes.
DISK_REQUEST_WARN_KB = 60_000_000

# Top-level import so the test fakes (which re-import this module) see a single class per name.
from .bundle_files import BundleCMSSWCache, BundleRepo  # noqa: E402


class CasinoHTCondorWorkflow(law.htcondor.HTCondorWorkflow):
    """HTCondorWorkflow that ships the repo bundle (plan + McM artifacts) so workers can source setup.sh."""

    config = luigi.Parameter(description="Sample config path")
    repo_path = luigi.Parameter(default="", significant=False)
    htcondor_accounting_group = luigi.Parameter(default="cms.production", significant=False)
    htcondor_container_image = luigi.Parameter(
        default="/cvmfs/unpacked.cern.ch/registry.hub.docker.com/cmssw/cms:rhel8-m", significant=False,
    )
    htcondor_walltime = luigi.Parameter(default="86399", significant=False, description="Requested walltime (seconds).")
    htcondor_request_cpus = luigi.IntParameter(
        default=0, significant=False, description="CPU cores per job; 0 derives it from the step config.",
    )
    htcondor_request_memory = luigi.Parameter(
        default="0", significant=False, description="Memory per job (MB); 0 derives it from the step config.",
    )
    htcondor_request_disk = luigi.Parameter(
        default="0", significant=False,
        description="Scratch disk per job (KB); 0 derives htcondor_disk_base + htcondor_disk_per_event * events_per_job.",
    )
    htcondor_disk_base_gb = luigi.Parameter(
        default="3", significant=False,
        description="Fixed part of the derived disk request (GB): CMSSW area, bundles, logs.",
    )
    htcondor_disk_per_event_mb = luigi.Parameter(
        default="2", significant=False,
        description="Event-scaling part of the derived disk request (MB per event). Delphes peaks at ~0.4 MB/event; "
        "the full CMSSW chain is heavier since GEN-SIM, AODSIM and MiniAOD coexist in scratch.",
    )
    htcondor_requirements = luigi.Parameter(
        default=DEFAULT_HTCONDOR_REQUIREMENTS, significant=False, description="Additional target machine requirements.",
    )
    htcondor_remote_job = luigi.Parameter(
        default="True", significant=False, description="ETP flag allowing jobs on remote resources (NEMO, TOPAS).",
    )
    lcg_stack = luigi.Parameter(
        default="/cvmfs/sft.cern.ch/lcg/views/LCG_107/x86_64-el8-gcc11-opt/setup.sh", significant=False,
        description="LCG view sourced on the worker; its OS must match htcondor_container_image (cms:rhel8-m = el8).",
    )
    retries = luigi.IntParameter(
        default=10, significant=False,
        description="Automatic per-branch resubmissions per `law run` (law's default is 5).",
    )

    create_branch_map_before_repr = True

    def htcondor_output_directory_name(self) -> str:
        return self.task_id

    def htcondor_output_directory(self):
        job_dir = law.config.get_expanded("job", "job_file_dir")
        return law.LocalDirectoryTarget(os.path.join(job_dir, self.htcondor_output_directory_name()))

    def htcondor_log_directory(self):
        return law.LocalDirectoryTarget(os.path.join(self.htcondor_output_directory().abspath, "logs"))

    def htcondor_create_job_file_factory(self):
        path = self.htcondor_log_directory().abspath
        factory = super().htcondor_create_job_file_factory(dir=path, mkdtemp="submission_", cleanup=False)
        logger.warn(f"HTCondor job directory is: {path}")
        return factory

    def htcondor_check_job_completeness(self):
        return True  # a corrupt job script can exit 0 without producing the output

    def htcondor_check_job_completeness_delay(self):
        return 0.0

    def _derived_disk_kb(self, events_per_job: int) -> int:
        base_kb = float(self.htcondor_disk_base_gb) * 1_000_000
        per_event_kb = float(self.htcondor_disk_per_event_mb) * 1_000
        request_kb = int(math.ceil(base_kb + per_event_kb * int(events_per_job)))
        if request_kb > DISK_REQUEST_WARN_KB:
            logger.warn(
                f"derived RequestDisk of {request_kb / 1e6:.1f} GB for {events_per_job} events/job is above what the "
                "disk-poorest nodes can hand a 2-core job; consider lowering --htcondor-disk-per-event or events_per_job"
            )
        return request_kb

    def _request_cpus(self):
        return self.htcondor_request_cpus

    def _request_memory(self) -> str:
        return self.htcondor_request_memory

    def _request_disk(self) -> str:
        configured = float(self.htcondor_request_disk)
        return str(int(configured)) if configured > 0 else str(self._derived_disk_kb(0))

    def htcondor_bootstrap_file(self):
        return law.JobInputFile(law.util.rel_path(__file__, "bootstrap.sh"), share=True, render_job=True)

    def htcondor_workflow_requires(self):
        reqs = super().htcondor_workflow_requires()
        reqs["repo"] = BundleRepo.req(
            self,
            repo_path=self.repo_path or str(repo_root_from_config(self.config)),
            config=self.config,
            driver_root=getattr(self, "driver_root", "artifacts/mcm-commands"),
            plan_root=getattr(self, "plan_root", "artifacts/step-plans"),
        )
        return reqs

    def htcondor_job_config(self, config, job_num, branches):
        log_dir = self.htcondor_log_directory().abspath
        config.log = os.path.join(log_dir, "Log.txt")
        config.stdout = os.path.join(log_dir, "Output.txt")
        config.stderr = os.path.join(log_dir, "Error.txt")
        config.universe = "container"
        config.custom_content = [
            ("accounting_group", self.htcondor_accounting_group),
            ("Requirements", self.htcondor_requirements),
            ("+RemoteJob", self.htcondor_remote_job),
            ("container_image", self.htcondor_container_image),
            ("+RequestWalltime", self.htcondor_walltime),
            ("x509userproxy", law.wlcg.get_vomsproxy_file()),
            ("request_cpus", self._request_cpus()),
            ("RequestMemory", self._request_memory()),
            ("RequestDisk", self._request_disk()),
            ("JobBatchName", self.task_id),
        ]
        # law's wlcg shell helpers (law_wlcg_get_file) for the bootstrap
        config.input_files["wlcg_tools"] = law.JobInputFile(
            law.util.law_src_path("contrib/wlcg/scripts/law_wlcg_tools.sh"), share=True, render=False,
        )
        repo_bundle = self.htcondor_workflow_requires()["repo"]
        config.render_variables["repo_uris"] = _bundle_uris(repo_bundle)
        config.render_variables["repo_pattern"] = _bundle_pattern(repo_bundle)
        config.render_variables["user"] = os.environ["USER"]
        config.render_variables["lcg_stack"] = self.lcg_stack
        return config


class CasinoCMSSWHTCondorWorkflow(CasinoHTCondorWorkflow):
    """Additionally ships the pre-created CMSSW release areas a step needs."""

    step = luigi.Parameter(description="Logical step name or slug")
    plan_root = luigi.Parameter(default="artifacts/step-plans", significant=False)

    def htcondor_bootstrap_file(self):
        return law.JobInputFile(law.util.rel_path(__file__, "bootstrap_cmssw_job.sh"), share=True, render_job=True)

    def htcondor_workflow_requires(self):
        reqs = super().htcondor_workflow_requires()
        reqs["cmssw_bundle"] = BundleCMSSWCache.req(self)
        return reqs

    def htcondor_job_config(self, config, job_num, branches):
        config = super().htcondor_job_config(config, job_num, branches)
        cmssw_bundle = self.htcondor_workflow_requires()["cmssw_bundle"]
        config.render_variables["cmssw_uris"] = _bundle_uris(cmssw_bundle)
        config.render_variables["cmssw_pattern"] = _bundle_pattern(cmssw_bundle)
        return config


def _bundle_uris(task) -> str:
    output_dir = task.output().dir
    try:
        uris = output_dir.uri(base_name="filecopy", return_all=True)
    except TypeError:
        uris = output_dir.uri(return_all=True)
    return ",".join(uris)


def _bundle_pattern(task) -> str:
    return os.path.basename(task.get_file_pattern())
