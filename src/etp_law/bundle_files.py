from __future__ import annotations

import hashlib
import importlib
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import storage
from .compat import import_law_dependencies
from .runtime import find_step
from .task_support import load_step_plan, sample_download_root, step_plan_path

law, luigi = import_law_dependencies()
law.contrib.load("tasks", "wlcg", "git")

# LCG views ship neither law nor luigi, so the worker gets them from vendor/site-packages in the bundle.
WORKER_RUNTIME_MODULES = ("law", "luigi", "lockfile", "dateutil", "six", "tenacity", "typing_extensions")
OPTIONAL_WORKER_RUNTIME_MODULES = ("tornado", "yaml", "daemon", "docutils")

_SiblingFileCollection = getattr(law, "SiblingFileCollection", None) or law.target.collection.SiblingFileCollection


def _transfer_output(output, local_target) -> None:
    local_path = Path(getattr(local_target, "path", local_target))
    targets = getattr(output, "targets", None)
    if isinstance(targets, dict):
        targets = targets.values()
    elif not isinstance(targets, (list, tuple)):
        targets = [output]
    for target in targets:
        storage.stage_out(target, local_path, retries=storage.STAGE_OUT_RETRIES, retry_delay=storage.STAGE_OUT_RETRY_DELAY)


class CasinoSiblingFileCollection(_SiblingFileCollection):
    def _iter_state(self, existing=True, keys=False, unpack=True, exists_func=None, **kwargs):
        del kwargs
        exists_func = exists_func or storage.target_exists
        for key, targets in self._iter_flat():
            group = list(targets) if isinstance(targets, (list, tuple)) else [targets]
            if all(exists_func(target) for target in group) is bool(existing):
                if unpack:
                    targets = self.targets[key]
                yield (key, targets) if keys else targets


class _BundleTask:
    """Shared output/completeness plumbing for the two TransferLocalFile bundles."""

    def _replicated_output(self):
        output = self.single_output()
        if self.replicas <= 0:
            return output
        return CasinoSiblingFileCollection([
            output.parent.child(self.get_replicated_path(output.basename, i), "f") for i in range(self.replicas)
        ])

    def get_file_pattern(self):
        path = os.path.expandvars(os.path.expanduser(self.single_output().abspath))
        return self.get_replicated_path(path, i=None if self.replicas <= 0 else "*")

    def output(self):
        return self._replicated_output()

    def complete(self):
        try:
            return storage.target_exists(self.output())
        except Exception:
            return False


class BundleRepo(_BundleTask, law.git.BundleGitRepository, law.tasks.TransferLocalFile):
    """Git-bundle the repo plus the step plan, McM artifacts and vendored law/luigi; upload to WLCG storage."""

    repo_path = luigi.Parameter(default="", significant=False, description="Repository path to bundle; defaults to $BASE_DIR.")
    config = luigi.Parameter(default="", significant=False, description="Sample config whose runtime artifacts join the bundle.")
    driver_root = luigi.Parameter(default="artifacts/mcm-commands", significant=False)
    plan_root = luigi.Parameter(default="artifacts/step-plans", significant=False)
    replicas = luigi.IntParameter(default=1, description="Number of replicas to generate; default: 1")

    exclude_files = ["tmp", "*~", "*.pyc", ".vscode/", ".law_tmp/"]
    version = None
    task_namespace = None

    def get_repo_path(self):
        return self.repo_path or os.environ["BASE_DIR"]

    def requires(self):
        if not self.config:
            return None
        from .tasks import BuildStepPlan

        return BuildStepPlan(config=self.config, driver_root=self.driver_root, plan_root=self.plan_root)

    def _runtime_artifact_sources(self) -> list[Path]:
        if not self.config:
            return []
        sources = [step_plan_path(self.config, self.plan_root), sample_download_root(self.config, self.driver_root)]
        missing = [source for source in sources if not source.exists()]
        if missing:
            raise FileNotFoundError(
                "required runtime artifacts missing from repo bundle source: " + ", ".join(map(str, missing))
            )
        return sources

    def _bundle_checksum(self) -> str:
        """Git checksum mixed with the runtime artifacts so a plan rebuild forces a re-bundle."""
        parts = [self.checksum]
        repo_root = Path(self.get_repo_path()).resolve()
        for source in self._runtime_artifact_sources():
            files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
            parts += [f"{f.resolve().relative_to(repo_root)}:{hashlib.sha1(f.read_bytes()).hexdigest()}" for f in files]
        return law.util.create_hash("|".join(parts))

    def single_output(self):
        repo_base = os.path.basename(self.get_repo_path())
        return law.wlcg.WLCGFileTarget(os.path.join("bundles", f"{repo_base}.{self._bundle_checksum()}.tgz"))

    def _stage_runtime_artifacts(self, stage_dir: Path) -> None:
        repo_root = Path(self.get_repo_path()).resolve()
        for source in self._runtime_artifact_sources():
            target = stage_dir / source.resolve().relative_to(repo_root)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    @staticmethod
    def _stage_runtime_modules(vendor_root: Path) -> None:
        site_packages = vendor_root / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        for module_name in WORKER_RUNTIME_MODULES + OPTIONAL_WORKER_RUNTIME_MODULES:
            try:
                module_path = Path(importlib.import_module(module_name).__file__).resolve()
            except ModuleNotFoundError:
                if module_name in WORKER_RUNTIME_MODULES:
                    raise
                continue
            if module_path.name == "__init__.py":
                shutil.copytree(module_path.parent, site_packages / module_path.parent.name, dirs_exist_ok=True)
            else:
                shutil.copy2(module_path, site_packages / module_path.name)
        wrapper = vendor_root / "bin" / "law"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text('#!/usr/bin/env bash\nexec python3 -m law "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)

    @law.decorator.safe_output
    def run(self):
        base_bundle = law.LocalFileTarget(is_tmp="tgz")
        self.bundle(base_bundle)
        final_bundle = law.LocalFileTarget(is_tmp="tgz")
        with tempfile.TemporaryDirectory(prefix="casino-bundle-") as tmpdir:
            stage_dir = Path(tmpdir) / "bundle-root"
            stage_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(base_bundle.path, "r:gz") as archive:
                archive.extractall(stage_dir)
            self._stage_runtime_artifacts(stage_dir)
            self._stage_runtime_modules(stage_dir / "vendor")
            with tarfile.open(final_bundle.path, "w:gz") as archive:
                for path in sorted(stage_dir.iterdir()):
                    archive.add(path, arcname=path.name)
        self.publish_message("bundled repository archive, size is " + law.util.human_bytes(final_bundle.stat().st_size, fmt=True))
        _transfer_output(self.output(), final_bundle)


class BundleCMSSWCache(_BundleTask, law.tasks.TransferLocalFile):
    """Pre-create the CMSSW areas a step needs and ship them so workers skip `scram p CMSSW`."""

    config = luigi.Parameter(description="Sample config path")
    step = luigi.Parameter(description="Logical step name or slug")
    plan_root = luigi.Parameter(default="artifacts/step-plans", significant=False)
    replicas = luigi.IntParameter(default=1, description="Number of replicas to generate; default: 1")

    version = None
    task_namespace = None

    def requires(self):
        from .tasks import BuildStepPlan

        return BuildStepPlan(config=self.config, plan_root=self.plan_root)

    def _step(self) -> dict:
        return find_step(load_step_plan(self.config, self.plan_root), self.step)

    def _release_specs(self) -> list[tuple[str, str]]:
        """Distinct (release, scram_arch) pairs of the CMSSW commands; delphes/nano commands have none."""
        specs = [(c.get("cmssw_release"), c.get("scram_arch")) for c in self._step()["commands"]]
        return list(dict.fromkeys(spec for spec in specs if all(spec)))

    def single_output(self):
        checksum = law.util.create_hash("-".join(f"{release}:{arch}" for release, arch in self._release_specs()))
        return law.wlcg.WLCGFileTarget(os.path.join("bundles", f"cmssw-cache-{self._step()['slug']}.{checksum}.tgz"))

    @law.decorator.safe_output
    def run(self):
        with tempfile.TemporaryDirectory(prefix="casino-cmssw-cache-") as tmpdir:
            cache_root = Path(tmpdir) / "cmssw-cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            for release, scram_arch in self._release_specs():
                script = "\n".join([
                    "set -euo pipefail",
                    # Same env guard as build_cmssw_command_script: keep LCG's Python/Geant4 away from scram.
                    "unset PYTHONHOME",
                    "unset PYTHONPATH",
                    "unset LD_LIBRARY_PATH",
                    "export LANG=${LANG:-C.UTF-8}",
                    "export LC_ALL=${LC_ALL:-C.UTF-8}",
                    f"export SCRAM_ARCH={shlex.quote(scram_arch)}",
                    "source /cvmfs/cms.cern.ch/cmsset_default.sh",
                    f"cd {shlex.quote(str(cache_root))}",
                    f"if [ ! -d {shlex.quote(release + '/src')} ]; then",
                    f"  scram p CMSSW {shlex.quote(release)}",
                    "fi",
                ])
                subprocess.run(["/bin/bash", "-lc", script], check=True)
            bundle = law.LocalFileTarget(is_tmp="tgz")
            with tarfile.open(bundle.path, "w:gz") as archive:
                archive.add(cache_root, arcname="cmssw-cache")
            _transfer_output(self.output(), bundle)
