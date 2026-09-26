import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from etp_law.runtime import (
    DAS_FILE_LIST_FILTER_AWK,
    RANDOM_SEED_MAX_VALUE,
    PreparedCommand,
    _module_seed,
    _resolve_runtime_placeholders,
    build_branch_execution_plan,
    build_cmssw_command_script,
    build_lcg_command_script,
    build_output_target,
    find_step,
    predecessor_branches_for_branch,
    required_bind_paths,
    resolve_output_backend,
    step_seed_salt,
)
from tests.support import SAMPLE_NAME, CasinoTestCase

LCG_SETUP = "/cvmfs/sft.cern.ch/lcg/views/LCG_107/x86_64-el8-gcc11-opt/setup.sh"


def _customise_commands(execution, index: int = 0) -> str:
    argv = execution.commands[index].cmsdriver_argv
    return argv[argv.index("--customise_commands") + 1]


def _execution(plan, step, branch=0, **kwargs):
    with tempfile.TemporaryDirectory() as tmpdir:
        return build_branch_execution_plan(plan=plan, step_name_or_slug=step, branch=branch, scratch_dir=Path(tmpdir), **kwargs)


def _source_plan(total_events: int, events_per_job: int = 10) -> dict:
    return {
        "sample_name": "sample", "base_output_path": "/tmp/out", "total_events": total_events,
        "steps": [{
            "name": "Source", "slug": "Source", "events_per_job": events_per_job, "cores_per_job": 1,
            "job_count": (total_events + events_per_job - 1) // events_per_job, "predecessor_step": None,
            "commands": [{
                "command_index": 7, "scram_arch": "el8_amd64_gcc12", "cmssw_release": "CMSSW_14_0_0",
                "fragment_path": None, "fragment_target": "fragment.py", "python_filename": "source_cfg.py",
                "filein": None, "fileout": "file:source.root",
                "options": [
                    {"name": "--customise_commands",
                     "value": "process.RandomNumberGeneratorService.externalLHEProducer.initialSeed=int(${SEED})"},
                    {"name": "--step", "value": "LHE,GEN,SIM"},
                ],
            }],
        }],
    }


def _delphes_plan() -> dict:
    gen = {
        "command_index": 0, "kind": "gen", "scram_arch": "el8_amd64_gcc12", "cmssw_release": "CMSSW_14_0_21",
        "fragment_path": "/repo/frag.py", "fragment_target": "Configuration/GenProduction/python/frag.py",
        "options": [{"name": "--step", "value": "LHE,GEN"}, {"name": "--fileout", "value": "file:HIG.root"},
                    {"name": "--python_filename", "value": "gen_cfg.py"}, {"name": "--no_exec", "value": None},
                    {"name": "--customise", "value": "Casino/HepMCWriter/customise.dumpHepMC"}],
        "filein": None, "fileout": "file:HIG.root", "python_filename": "gen_cfg.py", "hepmc_output": "gen.hepmc",
        "extra_cmssw_packages": [{"source": "/repo/cmssw/Casino/HepMCWriter", "dest": "Casino/HepMCWriter"}],
    }
    delphes = {"command_index": 1, "kind": "delphes", "card": "/repo/cards/delphes/delphes_card_CMS.tcl",
               "delphes_executable": "DelphesHepMC2", "filein": "gen.hepmc", "fileout": "delphes.root"}
    nano = {"command_index": 0, "kind": "nano", "filein": "delphes.root", "fileout": "nanoAODv15.root"}
    return {
        "plan_version": 4, "sample_name": "s", "base_output_path": "/tmp/out", "total_events": 200, "gridpack": None,
        "steps": [
            {"step_index": 0, "name": "delphes", "slug": "delphes", "filename": "delphes", "events_per_job": 100,
             "cores_per_job": 2, "memory_per_job": 4, "job_count": 2, "predecessor_step": None,
             "commands": [gen, delphes], "final_output_name": "delphes.root"},
            {"step_index": 1, "name": "nanoAODv15", "slug": "nanoAODv15", "filename": "nanoAODv15", "events_per_job": 200,
             "cores_per_job": 1, "memory_per_job": 2, "job_count": 1, "predecessor_step": "delphes",
             "commands": [nano], "final_output_name": "nanoAODv15.root"},
        ],
    }


class BranchExecutionPlanTests(CasinoTestCase):
    def setUp(self):
        super().setUp()
        self.prepare_plan()
        self.plan = self.load_plan()

    def test_source_step_rewrites_inputs_and_injects_runtime_options(self):
        execution = _execution(self.plan, "MiniAODv6")

        self.assertEqual((execution.event_count, len(execution.commands)), (100, 4))
        self.assertIsNone(execution.commands[0].filein)
        self.assertTrue(execution.commands[1].filein.endswith("/HIG-RunIII2024Summer24wmLHEGS-01175.root"))
        self.assertTrue(execution.commands[3].filein.endswith("/HIG-RunIII2024Summer24DRPremix-00530.root"))
        argv = execution.commands[0].cmsdriver_argv
        self.assertEqual(argv[argv.index("--number") + 1], "100")
        self.assertIn("--number_out", argv)
        self.assertNotIn("Configuration/DataProcessing/Utils.addMonitoring", argv)

        salt = step_seed_salt(find_step(self.plan, "MiniAODv6"), self.plan["total_events"])
        customise = _customise_commands(execution)
        self.assertIn(f"externalLHEProducer.initialSeed=int({_module_seed(0, 'externalLHEProducer', salt)})", customise)
        self.assertIn(f"generator.initialSeed=int({_module_seed(0, 'generator', salt)})", customise)
        self.assertIn("process.source.firstLuminosityBlock=cms.untracked.uint32(1)", customise)
        self.assertIn("process.MessageLogger.cerr.FwkReport.reportEvery=1000", customise)
        customise_branch_1 = _customise_commands(_execution(self.plan, "MiniAODv6", branch=1))
        self.assertIn("firstLuminosityBlock=cms.untracked.uint32(2)", customise_branch_1)
        self.assertNotEqual(customise, customise_branch_1)

    def test_downstream_step_reads_predecessor_outputs(self):
        execution = _execution(self.plan, "NanoAODv15", output_root_override="/tmp/casino-outputs")

        self.assertEqual((execution.event_count, len(execution.external_inputs)), (1000, 10))
        self.assertEqual(execution.external_inputs[0], f"file:/tmp/casino-outputs/{SAMPLE_NAME}/MiniAODv6-b6f0ca9e/MiniAODv6_1.root")
        self.assertEqual(execution.commands[0].filein, execution.external_inputs[0])
        self.assertEqual(execution.commands[0].cmsrun_input_files, execution.external_inputs)
        self.assertEqual(execution.output.path, f"/tmp/casino-outputs/{SAMPLE_NAME}/NanoAODv15-9ba262f9/NanoAODv15_1.root")
        argv = execution.commands[0].cmsdriver_argv
        self.assertEqual(argv[argv.index("--eventcontent") + 1], "NANOAODSIM1")

        script = build_cmssw_command_script(execution.commands[0], Path("/scratch"), Path("/cache"), compile_cores=4)
        self.assertIn("process.source.fileNames = cms.untracked.vstring(", script)
        self.assertIn(execution.external_inputs[-1], script)
        self.assertIn("cmsRun -n 4 HIG-RunIII2024Summer24NanoAODv15-00308_1_cfg.py", script)

    def test_output_targets_and_predecessor_mapping(self):
        self.assertEqual(resolve_output_backend("/ceph/casino", "auto"), "posix")
        self.assertEqual(resolve_output_backend("root://grid.example//store/user/casino", "auto"), "wlcg")
        target = build_output_target(self.plan, self.plan["steps"][1], 0, "root://grid.example//store/user/casino", "wlcg")
        self.assertEqual(target.path, f"{SAMPLE_NAME}/NanoAODv15-9ba262f9/NanoAODv15_1.root")
        self.assertEqual(target.uri, f"root://grid.example//store/user/casino/{target.path}")

        plan = {"sample_name": "s", "base_output_path": "/tmp/out", "total_events": 250, "steps": [
            {"name": "A", "slug": "a", "events_per_job": 100, "job_count": 3, "predecessor_step": None},
            {"name": "B", "slug": "b", "events_per_job": 200, "job_count": 2, "predecessor_step": "a"},
        ]}
        self.assertEqual(predecessor_branches_for_branch(plan, plan["steps"][1], 0), [0, 1])
        self.assertEqual(predecessor_branches_for_branch(plan, plan["steps"][1], 1), [2])


class SeedTests(unittest.TestCase):
    def test_seeds_are_golden_and_match_the_placeholder_path(self):
        # salt = int(sha1(b"10:20:Source")[:8], 16) = 3717002350; branch 1 base = 3/4
        customise = _customise_commands(_execution(_source_plan(20), "Source", branch=1))
        self.assertIn("externalLHEProducer.initialSeed=int(117002354)", customise)
        self.assertIn("generator.initialSeed=int(117002355)", customise)
        salt = 123_456_789
        for branch in (0, 1, 7):
            expected = _module_seed(branch, "externalLHEProducer", salt)
            self.assertEqual(_resolve_runtime_placeholders("initialSeed=int(${SEED})", branch, salt), f"initialSeed=int({expected})")

    def test_seeds_are_collision_free_in_range_and_decorrelated_by_total_events(self):
        def seeds(plan, branch):
            return [int(m.group(1)) for m in re.finditer(r"initialSeed=int\((\d+)\)", _customise_commands(_execution(plan, "Source", branch)))]

        plan = _source_plan(50, events_per_job=10)
        all_seeds = [seed for branch in range(plan["steps"][0]["job_count"]) for seed in seeds(plan, branch)]
        self.assertEqual(len(set(all_seeds)), 10)
        self.assertTrue(all(1 <= seed <= RANDOM_SEED_MAX_VALUE for seed in all_seeds))
        self.assertNotEqual(seeds(_source_plan(20), 1), seeds(_source_plan(200), 1))


class CommandScriptTests(CasinoTestCase):
    def setUp(self):
        super().setUp()
        self.prepare_plan()
        self.scratch = Path(tempfile.mkdtemp())
        execution = build_branch_execution_plan(self.load_plan(), "MiniAODv6", 0, self.scratch)
        self.script = build_cmssw_command_script(execution.commands[0], self.scratch, self.scratch / "cmssw-cache", compile_cores=4)

    def test_cmssw_script_sets_up_the_release_and_runs_cmsrun(self):
        for line in [
            "set -euo pipefail", "unset PYTHONHOME", "unset PYTHONPATH", "unset LD_LIBRARY_PATH",
            'for _v in $(compgen -e 2>/dev/null | grep -E "^(G4|GEANT4)" || true); do unset "${_v}"; done',
            "export SCRAM_ARCH=el8_amd64_gcc12", "source /cvmfs/cms.cern.ch/cmsset_default.sh",
            "scram p CMSSW CMSSW_14_0_21", "scram b ProjectRename", 'eval "$(scram runtime -sh)"',
            "cmsDriver.py Configuration/GenProduction/python/HIG-RunIII2024Summer24wmLHEGS-01175-fragment.py",
            "scram b -j 4", "| awk", 'if [ ! -r "${G4ENSDFSTATEDATA}/ENSDFSTATE.dat" ]; then',
            "cmsRun -n 4 HIG-RunIII2024Summer24wmLHEGS-01175_1_cfg.py",
            "TIMEFORMAT='cmsRun command 0 timing: wall=%3R s user=%3U s sys=%3S s cpu=%P'", "tail -n 500",
        ]:
            self.assertIn(line, self.script, line)

    def test_cmssw_script_filters_the_das_file_list(self):
        def filtered(text):
            return subprocess.run(["awk", DAS_FILE_LIST_FILTER_AWK], input=text, capture_output=True, text=True, check=True).stdout.splitlines()

        single_line = ", ".join(f"'/store/mc/file_{i}.root'" for i in range(12345))
        lines = filtered(f"DAS succeeded after 1 attempts 0\nfound files: [{single_line}]\nnext line")
        self.assertEqual(lines, ["DAS succeeded after 1 attempts 0", "found files: 12345 files (list suppressed)", "next line"])
        multi_line = "\n".join(f"/store/mc/file_{i}.root" for i in range(42))
        lines = filtered(f"found files:\n{multi_line}\nafter the list")
        self.assertEqual(lines, ["found files: 42 files (list suppressed)", "after the list"])

    def test_cmssw_script_propagates_the_cmsrun_exit_code(self):
        # Regression: `if ! cmsRun` made $? read 0 on a real failure.
        cmsrun_block = self.script[self.script.index("log_file="):]
        stub = "\n".join(["set -euo pipefail", "cmsRun() { echo 'boom' >&2; return 42; }", cmsrun_block])
        completed = subprocess.run(["/bin/bash", "-c", stub], capture_output=True, text=True, cwd=self.scratch)
        self.assertEqual(completed.returncode, 42, msg=completed.stderr)
        self.assertIn("cmsRun failed (rc=42)", completed.stderr)

    def test_required_bind_paths_creates_missing_directories(self):
        output_root = self.scratch / "output-root"
        bind_paths = required_bind_paths(
            repo_root=self.repo, scratch_dir=self.scratch / "s", cmssw_cache_dir=self.scratch / "c",
            output_root=str(output_root), output_backend="posix", input_specs=["file:/data/in.root"], fragment_paths=[],
        )
        self.assertTrue(output_root.exists())
        self.assertTrue({self.scratch / "s", self.scratch / "c", output_root.resolve(), Path("/cvmfs"), Path("/data")} <= set(bind_paths))


class DelphesRuntimeTests(unittest.TestCase):
    def test_delphes_step_commands_and_scripts(self):
        execution = _execution(_delphes_plan(), "delphes", output_backend_request="posix")
        gen, delphes = execution.commands

        self.assertEqual((gen.kind, gen.hepmc_output), ("gen", "gen.hepmc"))
        self.assertEqual(gen.extra_cmssw_packages, (("/repo/cmssw/Casino/HepMCWriter", "Casino/HepMCWriter"),))
        self.assertIn("Casino/HepMCWriter/customise.dumpHepMC", gen.cmsdriver_argv)
        self.assertEqual(delphes.delphes_argv[:2], ("DelphesHepMC2", "/repo/cards/delphes/delphes_card_CMS.tcl"))
        self.assertTrue(delphes.delphes_argv[2].endswith("/delphes.root"))
        self.assertTrue(delphes.delphes_argv[3].endswith("/gen.hepmc"))

        cmssw_script = build_cmssw_command_script(gen, Path("/scratch"), Path("/cache"), compile_cores=2)
        self.assertIn("cp -r /repo/cmssw/Casino/HepMCWriter Casino/HepMCWriter", cmssw_script)
        self.assertIn("scram b -j 2", cmssw_script)

        lcg_script = build_lcg_command_script(delphes, Path("/s"), LCG_SETUP, Path("/repo/src"))
        for line in ["set -eo pipefail", "unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH", f"source '{LCG_SETUP}'", "set -u",
                     "export PYTHONPATH='/repo/src'", "PileUpFile", "ln -sf", "rm -f '", "DelphesHepMC2", "tail -n 500"]:
            self.assertIn(line, lcg_script, line)
        self.assertNotIn("scram", lcg_script)
        self.assertNotIn("set -euo pipefail", lcg_script)

    def test_nano_step_merges_predecessor_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution = build_branch_execution_plan(_delphes_plan(), "nanoAODv15", 0, Path(tmp), output_root_override=tmp, output_backend_request="posix")
        (nano,) = execution.commands

        self.assertEqual(nano.kind, "nano")
        self.assertEqual(nano.ntuplize_argv[:3], ("python3", "-m", "delphes_nano"))
        inputs = nano.ntuplize_argv[3:-1]
        self.assertEqual(len(inputs), 2)
        self.assertTrue(all("delphes_" in path for path in inputs))
        self.assertTrue(nano.ntuplize_argv[-1].endswith("/nanoAODv15.root"))
        script = build_lcg_command_script(nano, Path("/s"), LCG_SETUP, Path("/repo/src"))
        self.assertIn("python3 -m delphes_nano", script)
        self.assertNotIn("rm -f", script)
