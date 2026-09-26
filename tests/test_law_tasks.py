import hashlib
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from tests.support import REPO_ROOT, CasinoTestCase

ETP_LAW_MODULES = ["etp_law.tasks", "etp_law.htcondor", "etp_law.bundle_files", "etp_law.storage", "etp_law.compat"]
KIT_ROOT = "root://cmsdcache-kit-disk.gridka.de:1094//store/user/sdaigler/mc_production"
KIT_DAVS = "davs://cmsdcache-kit-disk.gridka.de:2880/pnfs/gridka.de/cms/disk-only/store/user/sdaigler/mc_production"
DELPHES_CONFIG = (
    "total_events: 100\nbase_output_path: /ceph/x\nsteps:\n"
    "- name: delphes\n  filename: delphes\n  setup_ids:\n  - HIG-RunIII2024Summer24wmLHEGS-01175\n"
    "  - delphes:cards/delphes/delphes_card_CMS.tcl\n  events_per_job: 100\n  cores_per_job: 2\n  memory_per_job: 4\n"
)


def install_fake_law_and_luigi():
    class Parameter:
        def __init__(self, default=None, significant=True, description=""):
            self.default, self.significant, self.description, self.name = default, significant, description, ""

        def __set_name__(self, owner, name):
            self.name = name

        def __get__(self, instance, owner):
            return self if instance is None else instance.__dict__.get(self.name, self.default)

        def __set__(self, instance, value):
            instance.__dict__[self.name] = value

    class FakeTarget:
        def __init__(self, path=None, is_tmp=None, **kwargs):
            if path is None:
                handle, path = tempfile.mkstemp(suffix=f".{is_tmp}" if is_tmp else "")
                os.close(handle)
            self.path = self.abspath = str(path)
            self.kwargs = kwargs

        @property
        def parent(self):
            return FakeDirectoryTarget(str(Path(self.path).parent))

        dir = parent

        @property
        def basename(self):
            return Path(self.path).name

        def child(self, basename, type=None):
            return self.__class__(str(Path(self.path) / basename))

        def uri(self, return_all=False):
            return [self.path] if return_all else self.path

        def exists(self):
            return Path(self.path).exists()

        def copy_from_local(self, src, **kwargs):
            self.copied_from = src

    class FakeDirectoryTarget(FakeTarget):
        pass

    class FakeTargetCollection:
        def __init__(self, targets, threshold=1.0, **kwargs):
            self.targets, self.threshold = targets, threshold

        def _iter_flat(self):
            yield from (self.targets.items() if isinstance(self.targets, dict) else enumerate(self.targets))

        def _iter_state(self, existing=True, keys=False, exists_func=None, **kwargs):
            exists_func = exists_func or (lambda target: target.exists())
            for key, target in enumerate(self.targets):
                if bool(exists_func(target)) is bool(existing):
                    yield (key, target) if keys else target

        def exists(self, **kwargs):
            return self.count(**kwargs) >= len(self.targets) * self.threshold

        def count(self, **kwargs):
            keys = kwargs.get("keys", False)
            kwargs["keys"] = True
            found = [key for key, _ in self._iter_state(**kwargs)]
            return (len(found), found) if keys else len(found)

    class FakeSiblingFileCollection(FakeTargetCollection):
        def __init__(self, targets, threshold=1.0, **kwargs):
            super().__init__(targets, threshold=threshold, **kwargs)
            self.dir = targets[0].parent

        def _iter_state(self, *args, **kwargs):
            if self.dir.exists():
                yield from super()._iter_state(*args, **kwargs)

    class FakeTask:
        def __init__(self, **kwargs):
            for cls in reversed(type(self).mro()):
                for name, value in cls.__dict__.items():
                    if isinstance(value, Parameter) and name not in self.__dict__:
                        setattr(self, name, kwargs.pop(name, value.default))
            self.branch = kwargs.pop("branch", getattr(self, "branch", -1))
            for name, value in kwargs.items():
                setattr(self, name, value)
            self.task_id = f"{type(self).__name__}_{getattr(self, 'step', 'task')}"

        @classmethod
        def req(cls, instance=None, **overrides):
            params = {}
            if instance is not None and not isinstance(instance, type):
                for base in cls.mro():
                    for name, value in base.__dict__.items():
                        if isinstance(value, Parameter):
                            params[name] = getattr(instance, name, value.default)
                params["branch"] = getattr(instance, "branch", -1)
            params.update(overrides)
            return cls(**params)

    class LocalWorkflow:
        def workflow_complete(self):
            return NotImplemented

    class HTCondorWorkflow:
        def htcondor_workflow_requires(self):
            return {}

        def htcondor_job_config(self, config, job_num, branches):
            return config

        def htcondor_create_job_file_factory(self, **kwargs):
            return kwargs

    class TransferLocalFile(FakeTask):
        def get_replicated_path(self, path, i=None):
            return path if i is None else path.replace("*", str(i))

    class BundleGitRepository(FakeTask):
        exclude_files = []
        checksum = "checksum"

        def bundle(self, bundle):
            self.bundled_to = bundle

        def get_replicated_path(self, path, i=None):
            return path if i is None else path.replace("*", str(i))

    class Logger:
        def warn(self, *args, **kwargs):
            return None

        info = error = warn

    luigi_module = types.ModuleType("luigi")
    luigi_module.Parameter = Parameter
    luigi_module.IntParameter = type("IntParameter", (Parameter,), {})
    luigi_module.FloatParameter = type("FloatParameter", (Parameter,), {})
    luigi_module.BoolParameter = type("BoolParameter", (Parameter,), {})

    law_module = types.ModuleType("law")
    law_module.Task = FakeTask
    law_module.WrapperTask = type("WrapperTask", (FakeTask,), {})
    law_module.LocalWorkflow = LocalWorkflow
    law_module.LocalFileTarget = FakeTarget
    law_module.LocalDirectoryTarget = FakeDirectoryTarget
    law_module.JobInputFile = FakeTarget
    law_module.contrib = types.SimpleNamespace(load=lambda *args, **kwargs: None)
    law_module.logger = types.SimpleNamespace(get_logger=lambda name: Logger())
    law_module.config = types.SimpleNamespace(get_expanded=lambda section, key: "/tmp/law-jobs")
    law_module.decorator = types.SimpleNamespace(safe_output=lambda func: func)
    law_module.util = types.SimpleNamespace(
        rel_path=lambda origin, name: os.path.join(os.path.dirname(origin), name),
        law_src_path=lambda path: path,
        create_hash=lambda text: hashlib.md5(text.encode("utf-8")).hexdigest()[:8],
    )
    law_module.tasks = types.SimpleNamespace(TransferLocalFile=TransferLocalFile)
    law_module.git = types.SimpleNamespace(BundleGitRepository=BundleGitRepository)
    law_module.htcondor = types.SimpleNamespace(HTCondorWorkflow=HTCondorWorkflow)
    law_module.target = types.SimpleNamespace(collection=types.SimpleNamespace(
        TargetCollection=FakeTargetCollection, SiblingFileCollection=FakeSiblingFileCollection,
    ))
    law_module.wlcg = types.SimpleNamespace(
        WLCGFileTarget=type("WLCGFileTarget", (FakeTarget,), {}),
        WLCGDirectoryTarget=FakeDirectoryTarget,
        get_vomsproxy_file=lambda: "/tmp/x509up",
    )
    return luigi_module, law_module


def _reset_etp_law_modules():
    """Drop the law-bound modules (and the package attributes `from . import x` reuses) for a re-import."""
    package = sys.modules.get("etp_law")
    for module_name in ETP_LAW_MODULES:
        sys.modules.pop(module_name, None)
        if package is not None:
            package.__dict__.pop(module_name.rsplit(".", 1)[1], None)


def _job_config():
    return types.SimpleNamespace(custom_content=[], input_files={}, render_variables={})


def _uri_by_base(root_uri: str, **by_base_name):
    def uri(**kwargs):
        return by_base_name.get(kwargs.get("base_name"), root_uri)
    return mock.Mock(side_effect=uri)


class LawTasksTests(CasinoTestCase):
    def import_fake_etp_modules(self):
        luigi_module, law_module = install_fake_law_and_luigi()
        with mock.patch.dict(sys.modules, {"luigi": luigi_module, "law": law_module}):
            _reset_etp_law_modules()
            tasks_module = importlib.import_module("etp_law.tasks")
            htcondor_module = importlib.import_module("etp_law.htcondor")
            bundle_module = sys.modules["etp_law.bundle_files"]
        return tasks_module, htcondor_module, bundle_module

    def import_tasks_module(self):
        return self.import_fake_etp_modules()[0]

    def _wlcg_task(self, tasks_module):
        config_path = self.write_default_config()
        config_path.write_text(config_path.read_text(encoding="utf-8").replace("base_output_path: /ceph/sdaigler", f"base_output_path: {KIT_ROOT}"), encoding="utf-8")
        app = self.create_application()
        app.main(["download", str(config_path)])
        app.main(["build-step-runners", str(config_path)])
        return tasks_module.RunSampleStep(config=str(config_path), step="MiniAODv6", workflow="htcondor")

    def _expected_branch_basenames(self, tasks_module, task) -> set:
        plan = task.load_plan()
        step = tasks_module.find_step(plan, "MiniAODv6")
        output_root = tasks_module.resolve_output_root(plan, task.output_root)
        backend = tasks_module.resolve_output_backend(output_root, task.output_backend)
        return {os.path.basename(tasks_module.build_output_target(plan, step, b, output_root, backend).path) for b in task.create_branch_map()}

    def test_fetch_artifacts_complete_requires_all_expected_files(self):
        config_path = self.write_default_config()
        tasks_module = self.import_tasks_module()
        fetch_task = tasks_module.FetchMcMArtifacts(config=str(config_path))
        Path(fetch_task.output().path).mkdir(parents=True)
        self.assertFalse(fetch_task.complete())
        self.create_application().main(["download", str(config_path)])
        self.assertTrue(fetch_task.complete())

    def test_run_sample_step_requirements(self):
        config_path = self.write_default_config()
        tasks_module = self.import_tasks_module()
        task = tasks_module.RunSampleStep(config=str(config_path), step="NanoAODv15", skip_check_gridpack=True)

        reqs = task.requires()
        self.assertIsInstance(reqs["plan"], tasks_module.BuildStepPlan)
        self.assertTrue(reqs["plan"].skip_check_gridpack)
        self.assertEqual(reqs["upstream"].step, "MiniAODv6")
        self.assertEqual(task.workflow_requires()["upstream"].step, "MiniAODv6")
        with mock.patch.dict(os.environ, {"CASINO_CMSSW_CACHE_DIR": "/tmp/cmssw-cache"}):
            self.assertNotIn("upstream", task.requires())  # the submitter already verified upstream completeness

    def test_build_step_plan_forwards_the_gridpack_check(self):
        config_path = self.write_default_config()
        tasks_module = self.import_tasks_module()
        for skip in (False, True):
            task = tasks_module.BuildStepPlan(config=str(config_path), skip_check_gridpack=skip)
            app = mock.Mock()
            with mock.patch.object(task, "app", return_value=app):
                task.run()
            self.assertEqual(app.build_step_runners_for_config.call_args.kwargs["check_gridpack"], not skip)

    def test_run_sample_wraps_the_last_step_and_forwards_parameters(self):
        config_path = self.write_default_config()
        tasks_module = self.import_tasks_module()
        requirement = tasks_module.RunSample(config=str(config_path)).requires()
        self.assertIsInstance(requirement, tasks_module.RunSampleStep)
        self.assertEqual((requirement.step, requirement.workflow, requirement.poll_interval), ("NanoAODv15", "local", 10))

        requirements = '(TARGET.ProvidesCPU)&&(TARGET.Machine =!= "f03.gridka.de")'
        wrapper = tasks_module.RunSample(config=str(config_path), workflow="htcondor", poll_interval="30sec", htcondor_requirements=requirements)
        requirement = wrapper.requires()
        self.assertEqual((requirement.workflow, requirement.poll_interval, requirement.htcondor_requirements), ("htcondor", "30sec", requirements))

    def test_bundles_require_the_step_plan_and_use_storage_helpers(self):
        config_path = self.write_default_config()
        _, htcondor_module, bundle_module = self.import_fake_etp_modules()
        cmssw_bundle = htcondor_module.BundleCMSSWCache(config=str(config_path), step="MiniAODv6")
        self.assertEqual(type(cmssw_bundle.requires()).__name__, "BuildStepPlan")
        self.assertEqual(cmssw_bundle.requires().plan_root, cmssw_bundle.plan_root)
        self.assertFalse(cmssw_bundle.complete())

        target = mock.Mock()
        target.exists = mock.Mock(side_effect=RuntimeError("gfal2 is not installed"))
        with mock.patch.object(cmssw_bundle, "output", return_value=target), \
             mock.patch.object(bundle_module.storage, "target_exists", return_value=True) as exists:
            self.assertTrue(cmssw_bundle.complete())
        exists.assert_called_once_with(target)

        repo_bundle = bundle_module.BundleRepo(config=str(config_path), repo_path=str(self.repo), driver_root="artifacts/mcm-commands", plan_root="artifacts/step-plans")
        requirement = repo_bundle.requires()
        self.assertEqual((type(requirement).__name__, requirement.driver_root, requirement.plan_root), ("BuildStepPlan", "artifacts/mcm-commands", "artifacts/step-plans"))
        with mock.patch.object(repo_bundle, "output", return_value=target):
            self.assertFalse(repo_bundle.complete())

    def test_transfer_output_stages_every_replica(self):
        _, _, bundle_module = self.import_fake_etp_modules()
        local_bundle = bundle_module.law.LocalFileTarget("/tmp/casino-bundle.tgz")
        target_a, target_b, single = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(bundle_module.storage, "stage_out") as stage_out:
            bundle_module._transfer_output(types.SimpleNamespace(targets=[target_a, target_b]), local_bundle)
            bundle_module._transfer_output(single, local_bundle)
        self.assertEqual(stage_out.call_args_list, [
            mock.call(t, Path(local_bundle.path), retries=10, retry_delay=300) for t in (target_a, target_b, single)
        ])

    def test_cmssw_cache_release_specs_skip_non_cmssw_commands(self):
        config_path = self.write_default_config()
        _, _, bundle_module = self.import_fake_etp_modules()
        plan = {"steps": [{"name": "delphes", "slug": "delphes", "commands": [
            {"kind": "gen", "cmssw_release": "CMSSW_14_0_21", "scram_arch": "el8_amd64_gcc12"},
            {"kind": "delphes", "card": "/c.tcl"},
        ]}]}
        task = bundle_module.BundleCMSSWCache(config=str(config_path), step="delphes")
        with mock.patch.object(bundle_module, "load_step_plan", return_value=plan):
            self.assertEqual(task._release_specs(), [("CMSSW_14_0_21", "el8_amd64_gcc12")])

    def test_completeness_tolerates_transient_storage_errors(self):
        config_path = self.write_default_config()
        tasks_module = self.import_tasks_module()
        failing = mock.Mock(exists=mock.Mock(side_effect=RuntimeError("transient dCache error")))
        branch = tasks_module.RunSampleStep(config=str(config_path), step="MiniAODv6", workflow="htcondor", branch=0)
        with mock.patch.dict(os.environ, {"CASINO_CMSSW_CACHE_DIR": "/tmp/cmssw-cache"}), \
             mock.patch.object(branch, "output", return_value=failing):
            self.assertFalse(branch.complete())
        workflow = tasks_module.RunSampleStep(config=str(config_path), step="MiniAODv6", workflow="htcondor")
        with mock.patch.object(workflow, "target_from_spec", return_value=failing):
            self.assertFalse(workflow.workflow_complete())

    def test_target_collection_uses_one_directory_listing(self):
        tasks_module = self.import_tasks_module()
        storage = tasks_module.storage
        targets = [storage.law.wlcg.WLCGFileTarget(f"GluGluHH/MiniAODv6-43dfb1fe/miniAODv6_{i}.root") for i in range(1, 6)]
        collection = tasks_module.CasinoTargetCollection(targets)
        basenames = {os.path.basename(t.path) for t in targets}

        with mock.patch.object(storage, "dir_listing", return_value=basenames) as listing, \
             mock.patch.object(storage, "target_exists") as per_file:
            self.assertEqual(collection.count(existing=True), 5)
        listing.assert_called_once()
        per_file.assert_not_called()
        with mock.patch.object(storage, "dir_listing", return_value=basenames - {"miniAODv6_5.root"}):
            self.assertEqual(collection.count(existing=True), 4)

        plain = mock.Mock()
        plain.exists = mock.Mock(side_effect=RuntimeError("gfal2 is not installed"))
        with mock.patch.object(storage, "target_exists", return_value=True) as exists:
            self.assertEqual(tasks_module.CasinoTargetCollection([plain]).count(existing=True, keys=True), (1, [0]))
        exists.assert_called_once_with(plain)

    def test_branch_outputs_complete_uses_the_listing_then_falls_back(self):
        tasks_module = self.import_tasks_module()
        task = self._wlcg_task(tasks_module)
        expected = self._expected_branch_basenames(tasks_module, task)
        storage = tasks_module.storage

        with mock.patch.object(storage, "dir_listing", return_value=set(expected)) as listing, \
             mock.patch.object(storage, "target_exists") as per_branch:
            self.assertTrue(task.workflow_complete())
        listing.assert_called_once()
        per_branch.assert_not_called()
        with mock.patch.object(storage, "dir_listing", return_value=set(list(expected)[1:])):
            self.assertFalse(task.workflow_complete())
        with mock.patch.object(storage, "dir_listing", return_value=None), \
             mock.patch.object(storage, "target_exists", return_value=True) as per_branch:
            self.assertTrue(task.workflow_complete())
        self.assertTrue(per_branch.called)

    def test_target_exists_probes_gfal_stat_then_xrdfs(self):
        storage = self.import_tasks_module().storage
        target = storage.law.wlcg.WLCGFileTarget("GluGluHH/MiniAODv6/miniAODv6_1.root")
        target.exists = mock.Mock(return_value=False)
        ok = subprocess.CompletedProcess(args=[], returncode=0)
        quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        target.uri = _uri_by_base(f"{KIT_ROOT}/mc.root")
        with mock.patch.object(storage.shutil, "which", return_value="/usr/bin/xrdfs"), \
             mock.patch.object(storage.subprocess, "run", return_value=ok) as run:
            self.assertTrue(storage.target_exists(target))
        run.assert_called_once_with(["/usr/bin/xrdfs", "cmsdcache-kit-disk.gridka.de:1094", "stat", "/store/user/sdaigler/mc_production/mc.root"], **quiet)

        target.uri = _uri_by_base(f"{KIT_ROOT}/mc.root", stat=f"{KIT_DAVS}/mc.root")
        with mock.patch.object(storage.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(storage.subprocess, "run", return_value=ok) as run:
            self.assertTrue(storage.target_exists(target))
        run.assert_called_once_with(["/usr/bin/gfal-stat", f"{KIT_DAVS}/mc.root"], **quiet)
        target.exists.assert_not_called()

    def test_dir_listing_handles_success_transient_errors_and_missing_directories(self):
        storage = self.import_tasks_module().storage
        target = storage.law.wlcg.WLCGFileTarget("sample/MiniAODv6/miniAODv6_1.root")
        target.uri = _uri_by_base(f"{KIT_ROOT}/sample/MiniAODv6-43dfb1fe/miniAODv6_1.root", listdir=f"{KIT_DAVS}/sample/MiniAODv6-43dfb1fe/miniAODv6_1.root")
        which = lambda name: "/usr/bin/gfal-ls" if name == "gfal-ls" else None  # noqa: E731

        def listing(completed):
            with mock.patch.object(storage.shutil, "which", side_effect=which), \
                 mock.patch.object(storage.subprocess, "run", return_value=completed) as run:
                result = storage.dir_listing(target)
            return result, run

        result, run = listing(subprocess.CompletedProcess(args=[], returncode=0, stdout="miniAODv6_1.root\nminiAODv6_2.root\n"))
        self.assertEqual(result, {"miniAODv6_1.root", "miniAODv6_2.root"})
        run.assert_called_once_with(["/usr/bin/gfal-ls", f"{KIT_DAVS}/sample/MiniAODv6-43dfb1fe"], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False, text=True, timeout=storage.DIR_LISTING_TIMEOUT)
        # A transient failure must yield None (fallback), never an empty set.
        self.assertIsNone(listing(subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="gfal-ls error: 70 (Communication error)"))[0])
        self.assertEqual(listing(subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="gfal-ls error: 2 (No such file or directory)"))[0], set())

    def test_stage_out_uses_xrdcp_even_when_gfal_is_available(self):
        # The worker's gfal CLIs are broken (no /usr/bin/python), so stage-out must never use them.
        storage = self.import_tasks_module().storage
        target = storage.law.wlcg.WLCGFileTarget("sample/MiniAODv6/file.root")
        target.uri = _uri_by_base("root://kit:1094//path/file.root", filecopy="davs://kit:2880/path/file.root")
        with mock.patch.object(storage.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(storage.subprocess, "run") as run:
            storage.stage_out(target, Path("/tmp/file.root"), retries=0, retry_delay=0)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["xrdfs", "xrdcp"])
        self.assertEqual(run.call_args_list[1].args[0][-1], "root://kit:1094//path/file.root")

    def test_wlcg_fs_for_pins_bases_per_output_root(self):
        storage = self.import_tasks_module().storage

        class FakeWLCGFileSystem:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        storage.law.wlcg.WLCGFileSystem = FakeWLCGFileSystem
        for root in (KIT_ROOT, KIT_ROOT.replace(":1094//", ":1094/")):
            storage.WLCG_FS_CACHE.clear()
            fs = storage.wlcg_fs_for(root)
            self.assertEqual(fs.kwargs, {"base": root, "bases": {name: KIT_DAVS for name in storage.WEBDAV_BASE_NAMES}})
        custom_root = "root://other-se.example.com:1094//store/user/me/out"
        self.assertEqual(storage.wlcg_fs_for(custom_root).kwargs["bases"], {name: custom_root for name in storage.WEBDAV_BASE_NAMES})

    def test_htcondor_job_config_derives_requests_from_the_step(self):
        config_path = self.write_default_config()
        text = config_path.read_text(encoding="utf-8").replace("cores_per_job: 4", "cores_per_job: 2").replace("memory_per_job: 4", "memory_per_job: 6")
        config_path.write_text(text, encoding="utf-8")
        tasks_module = self.import_tasks_module()

        def requests(step, **params):
            task = tasks_module.RunSampleStep(config=str(config_path), step=step, workflow="htcondor", **params)
            return dict(task.htcondor_job_config(_job_config(), 0, [0]).custom_content)

        no_plan_yet = tasks_module.RunSampleStep(config=str(config_path), step="MiniAODv6", workflow="htcondor")
        self.assertEqual(no_plan_yet._request_disk(), str(3_000_000 + 2 * 1_000 * 100))  # derived from the config
        app = self.create_application()
        app.main(["download", str(config_path)])
        app.main(["build-step-runners", str(config_path)])
        mini = requests("MiniAODv6")
        self.assertEqual((mini["request_cpus"], mini["RequestMemory"], mini["RequestDisk"]), (2, "6000", str(3_000_000 + 2 * 1_000 * 100)))
        self.assertEqual(requests("NanoAODv15")["RequestDisk"], str(3_000_000 + 2 * 1_000 * 1000))
        self.assertEqual(requests("NanoAODv15", htcondor_disk_base_gb="4", htcondor_disk_per_event_mb="0.5")["RequestDisk"], str(4_000_000 + 500 * 1000))
        self.assertEqual(requests("NanoAODv15", htcondor_request_disk="6000000")["RequestDisk"], "6000000")

    def test_htcondor_job_config_renders_bundle_inputs(self):
        config_path = self.prepare_plan()
        tasks_module = self.import_tasks_module()
        task = tasks_module.RunSampleStep(config=str(config_path), step="MiniAODv6", workflow="htcondor",
                                          htcondor_accounting_group="analysis.test", lcg_stack="/cvmfs/example.cern.ch/lcg.sh")

        rendered = task.htcondor_job_config(_job_config(), 0, [0])

        self.assertEqual(rendered.universe, "container")
        self.assertIn(("accounting_group", "analysis.test"), rendered.custom_content)
        self.assertEqual(rendered.render_variables["user"], "casino-test-user")
        self.assertEqual(rendered.render_variables["lcg_stack"], "/cvmfs/example.cern.ch/lcg.sh")
        self.assertTrue({"repo_uris", "repo_pattern", "cmssw_uris", "cmssw_pattern"} <= rendered.render_variables.keys())
        self.assertIn("wlcg_tools", rendered.input_files)

    def test_htcondor_output_directory_is_readable(self):
        config_path = self.write_default_config()
        tasks_module = self.import_tasks_module()
        task = tasks_module.RunSampleStep(config=str(config_path), step="MiniAODv6", workflow="htcondor")
        task.task_id = "RunSampleStep__1__False_0bfe850fbb"
        expected = f"/tmp/law-jobs/RunSampleStep_MiniAODv6_{config_path.stem}_0bfe850fbb"
        self.assertEqual(task.htcondor_output_directory().path, expected)
        factory = task.htcondor_create_job_file_factory()
        self.assertEqual((factory["dir"], factory["mkdtemp"], factory["cleanup"]), (f"{expected}/logs", "submission_", False))

    def test_law_cfg_keeps_job_grouping_and_webdav_bases(self):
        content = (REPO_ROOT / "law.cfg").read_text(encoding="utf-8")
        self.assertNotIn("htcondor_job_grouping_submit = False", content)  # batched submission ships law_job.sh unrendered
        webdav = "davs://cmsdcache-kit-disk.gridka.de:2880/pnfs/gridka.de/cms/disk-only/store/user/${USER}/mc_production"
        for option in ["stat", "exists", "chmod", "unlink", "rmdir", "mkdir", "listdir", "filecopy"]:
            self.assertIn(f"base_{option}: {webdav}", content)

    def test_setup_script_exports_paths_and_links_dasgoclient(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conda_prefix = Path(temp_dir)
            (conda_prefix / "bin").mkdir()
            dasgoclient = conda_prefix / "cvmfs" / "dasgoclient"
            dasgoclient.parent.mkdir(parents=True)
            dasgoclient.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            dasgoclient.chmod(0o755)
            env = dict(os.environ, CONDA_PREFIX=str(conda_prefix), CASINO_DASGOCLIENT_SOURCE=str(dasgoclient))
            command = f'source {REPO_ROOT / "setup.sh"}; printf "%s\\n%s\\n%s\\n" "$BASE_DIR" "$LAW_CONFIG_FILE" "$PYTHONPATH"'
            result = subprocess.run(["bash", "-lc", command], check=True, capture_output=True, text=True, env=env)
            base_dir, law_config_file, pythonpath = result.stdout.strip().splitlines()
            self.assertEqual((base_dir, law_config_file), (str(REPO_ROOT), str(REPO_ROOT / "law.cfg")))
            self.assertIn(str(REPO_ROOT / "src"), pythonpath.split(":"))
            self.assertEqual((conda_prefix / "bin" / "dasgoclient").resolve(), dasgoclient)

    def test_bootstrap_scripts_fetch_bundles_before_sourcing_lcg(self):
        for name in ("bootstrap.sh", "bootstrap_cmssw_job.sh"):
            content = (REPO_ROOT / "src" / "etp_law" / name).read_text(encoding="utf-8")
            for line in ['export USER="{{user}}"', 'export BASE_DIR="${PWD}/repo"', 'source "${JOB_DIR}/{{wlcg_tools}}"',
                         "law_wlcg_get_file \"{{repo_uris}}\" '{{repo_pattern}}'", 'source "{{lcg_stack}}"', 'source "${BASE_DIR}/setup.sh"']:
                self.assertIn(line, content, f"{name}: {line}")
            # The OSG env must be dropped before LCG is sourced.
            self.assertLess(content.find("unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH GFAL_PLUGIN_DIR GFAL_CONFIG_DIR"), content.find('source "{{lcg_stack}}"'))
        cmssw = (REPO_ROOT / "src" / "etp_law" / "bootstrap_cmssw_job.sh").read_text(encoding="utf-8")
        self.assertIn('export CASINO_CMSSW_CACHE_DIR="${PWD}/cmssw-cache"', cmssw)
        self.assertIn("law_wlcg_get_file \"{{cmssw_uris}}\" '{{cmssw_pattern}}'", cmssw)

    def _remote_copy(self) -> Path:
        remote_repo = self.repo / "remote-copy"
        shutil.copytree(self.repo / "configs", remote_repo / "configs", dirs_exist_ok=True)
        shutil.copytree(self.repo / "artifacts", remote_repo / "artifacts", dirs_exist_ok=True)
        return remote_repo

    def test_load_step_plan_remaps_paths_onto_the_worker_repo(self):
        config_path = self.prepare_plan()
        remote_repo = self._remote_copy()
        from etp_law.task_support import load_step_plan, resolve_config_path

        host_config = Path("/work/sdaigler/casino/configs/run3/2024") / config_path.name
        with mock.patch.dict(os.environ, {"BASE_DIR": str(remote_repo)}):
            self.assertEqual(resolve_config_path(str(host_config)), (remote_repo / "configs" / "run3" / "2024" / config_path.name).resolve())
            plan = load_step_plan(str(host_config))

        fragment_path = plan["steps"][0]["commands"][0]["fragment_path"]
        self.assertTrue(fragment_path.startswith(str(remote_repo.resolve())))
        self.assertIn("/artifacts/mcm-commands/", fragment_path)

    def test_load_step_plan_remaps_delphes_assets(self):
        config_path = self.write_default_config()
        config_path.write_text(DELPHES_CONFIG, encoding="utf-8")
        (self.repo / "cards" / "delphes").mkdir(parents=True)
        (self.repo / "cards" / "delphes" / "delphes_card_CMS.tcl").write_text("# card\n", encoding="utf-8")
        app = self.create_application()
        app.main(["download", str(config_path)])
        app.main(["build-step-runners", str(config_path)])
        remote_repo = self._remote_copy()
        from etp_law.task_support import load_step_plan

        with mock.patch.dict(os.environ, {"BASE_DIR": str(remote_repo)}):
            plan = load_step_plan(str(Path("/work/sdaigler/casino/configs/run3/2024") / config_path.name))

        gen, delphes = plan["steps"][0]["commands"]
        self.assertEqual(delphes["card"], str(remote_repo.resolve() / "cards" / "delphes" / "delphes_card_CMS.tcl"))
        self.assertEqual(gen["extra_cmssw_packages"][0]["source"], str(remote_repo.resolve() / "cmssw" / "Casino" / "HepMCWriter"))

    def test_run_dispatches_commands_by_kind(self):
        config_path = self.write_default_config()
        tasks_module = self.import_tasks_module()
        base = dict(fragment_path=None, fragment_target=None, cmsrun_input_files=(), cmsdriver_argv=(), cmsrun_argv=())
        gen = tasks_module.PreparedCommand(command_index=0, scram_arch="el8_amd64_gcc12", cmssw_release="CMSSW_14_0_21", python_filename="g.py",
                                           filein=None, fileout="file:g.root", local_output_path="/s/g.root", kind="gen", **base)
        delphes = tasks_module.PreparedCommand(command_index=1, scram_arch="", cmssw_release="", python_filename="", filein="/s/gen.hepmc",
                                               fileout="delphes.root", local_output_path="/s/delphes.root", kind="delphes", card="/repo/c.tcl", **base)
        task = tasks_module.RunSampleStep(config=str(config_path), step="delphes", branch=0)
        execution = types.SimpleNamespace(commands=(gen, delphes), external_inputs=(), scratch_dir="/s")
        plan = {"steps": [{"name": "delphes", "slug": "delphes", "cores_per_job": 2}], "total_events": 100, "base_output_path": "/tmp/output"}
        calls = []

        with mock.patch.dict(os.environ, {"CASINO_CMSSW_CACHE_DIR": "/cache"}), \
             mock.patch.object(task, "load_plan", return_value=plan), \
             mock.patch.object(tasks_module, "find_step", return_value=plan["steps"][0]), \
             mock.patch.object(tasks_module, "build_branch_execution_plan", return_value=execution), \
             mock.patch.object(tasks_module, "build_cmssw_command_script", side_effect=lambda **k: calls.append("cmssw") or "cmssw"), \
             mock.patch.object(tasks_module, "build_lcg_command_script", side_effect=lambda **k: calls.append("lcg") or "lcg"), \
             mock.patch.object(tasks_module.subprocess, "run") as run, \
             mock.patch.object(task, "output", return_value=mock.Mock()), \
             mock.patch.object(tasks_module.storage, "stage_out") as stage_out:
            task.run()

        self.assertEqual(calls, ["cmssw", "lcg"])
        self.assertEqual([call.args[0] for call in run.call_args_list], [["/bin/bash", "-lc", "cmssw"], ["/bin/bash", "-lc", "lcg"]])
        stage_out.assert_called_once()
