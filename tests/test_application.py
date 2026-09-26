import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from application.chain_resolver import (
    AmbiguousChain,
    AuthenticatedChainResolver,
    DrpremixUnresolved,
    NoChainFound,
    ProxyChainResolver,
    WmlhegsUnresolved,
    resolve_chain,
    select_chain,
)
from application.config_generator import ConfigWriter, build_config, dump_config, parse_campaign_location, render_law_command
from application.config_lister import available_years, configs_for_year
from application.step_plan_builder import StepPlanBuilder
from domain.chain import ResolvedChain, ResolvedStep
from domain.configuration import ConfigLoader
from domain.defaults import Defaults
from infrastructure.mcm_auth import McMAuthUnavailable
from tests.support import MCM_ROOT, PLANS_ROOT, SAMPLE_NAME, CasinoTestCase

CUSTOM_GRIDPACK = "root://grid.example//store/user/casino/gridpacks/ggHH_kl_m1.tgz"
DELPHES_CONFIG = (
    "total_events: 200\nbase_output_path: /ceph/sdaigler\nsteps:\n"
    "- name: delphes\n  filename: delphes\n  setup_ids:\n"
    "  - HIG-RunIII2024Summer24wmLHEGS-01175\n  - delphes:cards/delphes/delphes_card_CMS.tcl\n"
    "  events_per_job: 100\n  cores_per_job: 2\n  memory_per_job: 4\n"
    "- name: delphes-nano\n  filename: nanoAODv15\n  setup_ids:\n  - delphes-nano\n"
    "  events_per_job: {nano_epj}\n  cores_per_job: 1\n  memory_per_job: 2\n"
)
NANO = ("/GluGluHHto2B2Tau_Par-c2-0p00-kl-1p00-kt-1p00_TuneCP5_13p6TeV_powheg-pythia8"
        "/RunIII2024Summer24NanoAODv15-PowhegBugFix_150X_mcRun3_2024_realistic_v2-v2/NANOAODSIM")
MINI = NANO.replace("NanoAODv15", "MiniAODv6").replace("NANOAODSIM", "MINIAODSIM")
CHAIN = ("HIG-chain_RunIII2024Summer24wmLHEGS_flowRunIII2024Summer24DRPremixNoOutput_"
         "flowRunIII2024Summer24MiniAODv6_flowRunIII2024Summer24NanoAODv15-00118")
GS_CHAIN = ("GEN-chain_RunIII2024Summer24GS_flowRunIII2024Summer24DRPremix_"
            "flowRunIII2024Summer24MiniAODv6_flowRunIII2024Summer24NanoAODv15-00084")
DRPREMIX = "HIG-RunIII2024Summer24DRPremix-00530"


class RecordingGridpackChecker:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.checked_uris: list[str] = []

    def ensure_exists(self, gridpack_uri: str) -> None:
        self.checked_uris.append(gridpack_uri)
        if self.error is not None:
            raise self.error


class StepPlanBuilderTests(CasinoTestCase):
    def _add_gridpack(self, config_path: Path) -> None:
        text = config_path.read_text(encoding="utf-8").replace(
            "base_output_path: /ceph/sdaigler", f"base_output_path: /ceph/sdaigler\ngridpack: {CUSTOM_GRIDPACK}", 1,
        )
        config_path.write_text(text, encoding="utf-8")

    def test_build_step_runners_creates_plan(self):
        config_path = self.write_default_config()
        app = self.create_application()
        app.main(["download", str(config_path)])

        self.assertEqual(app.main(["build-step-runners", str(config_path)]), 0)

        plan = self.load_plan()
        StepPlanBuilder.validate_plan_payload(plan)
        self.assertEqual(plan["plan_version"], StepPlanBuilder.PLAN_VERSION)
        self.assertIsNone(plan["gridpack"])
        mini, nano = plan["steps"]
        self.assertEqual((mini["filename"], mini["job_count"], mini["predecessor_step"]), ("MiniAODv6", 10, None))
        self.assertEqual((nano["filename"], nano["job_count"], nano["predecessor_step"]), ("NanoAODv15", 1, "MiniAODv6"))
        self.assertEqual(mini["final_output_name"], "HIG-RunIII2024Summer24MiniAODv6-00530.root")
        self.assertEqual([c["command_index"] for c in mini["commands"]], [0, 1, 2, 3])
        first = mini["commands"][0]
        self.assertIsNone(first["filein"])
        self.assertEqual(first["fileout"], "file:HIG-RunIII2024Summer24wmLHEGS-01175.root")
        self.assertEqual(first["python_filename"], "HIG-RunIII2024Summer24wmLHEGS-01175_1_cfg.py")
        option_names = [option["name"] for option in first["options"]]
        self.assertNotIn("--number", option_names)
        self.assertNotIn("Configuration/DataProcessing/Utils.addMonitoring", [o["value"] for o in first["options"]])
        eventcontent = next(o["value"] for o in nano["commands"][0]["options"] if o["name"] == "--eventcontent")
        self.assertEqual(eventcontent, "NANOAODSIM1")

    def test_accepts_json_encoded_mcm_payloads(self):
        config_path = self.write_default_config()
        app = self.create_application(fetcher=self.fake_fetch_text_json)
        app.main(["download", str(config_path)])
        self.assertEqual(app.main(["build-step-runners", str(config_path)]), 0)
        sample_root = self.repo / MCM_ROOT / "run3" / "2024" / SAMPLE_NAME
        script = (sample_root / "HIG-RunIII2024Summer24" / "HIG-RunIII2024Summer24wmLHEGS-01175.sh").read_text(encoding="utf-8")
        fragment = (sample_root / "fragments" / "HIG-RunIII2024Summer24wmLHEGS-01175-fragment.py").read_text(encoding="utf-8")
        self.assertIn("cat <<'EndOfTestFile'", script)
        self.assertTrue(fragment.startswith("import FWCore"))

    def test_gridpack_config_creates_and_preserves_the_custom_fragment(self):
        config_path = self.write_default_config()
        self._add_gridpack(config_path)
        checker = RecordingGridpackChecker()
        app = self.create_application(gridpack_checker=checker)
        app.main(["download", str(config_path)])
        custom_fragment = self.repo / "configs" / "run3" / "2024" / "fragments" / f"{SAMPLE_NAME}-fragment.py"
        downloaded_fragment = self.repo / MCM_ROOT / "run3" / "2024" / SAMPLE_NAME / "fragments" / "HIG-RunIII2024Summer24wmLHEGS-01175-fragment.py"

        self.assertEqual(app.main(["build-step-runners", str(config_path)]), 0)

        self.assertEqual(checker.checked_uris, [CUSTOM_GRIDPACK])
        rendered = custom_fragment.read_text(encoding="utf-8")
        self.assertIn(CUSTOM_GRIDPACK, rendered)
        self.assertIn("run_generic_tarball_xrootd.sh", rendered)
        self.assertNotIn(CUSTOM_GRIDPACK, downloaded_fragment.read_text(encoding="utf-8"))
        plan = self.load_plan()
        self.assertEqual(plan["gridpack"], CUSTOM_GRIDPACK)
        self.assertEqual(plan["steps"][0]["commands"][0]["fragment_path"], str(custom_fragment.resolve()))

        custom_fragment.write_text("# user edited fragment\n", encoding="utf-8")
        app.main(["build-step-runners", str(config_path)])
        self.assertEqual(custom_fragment.read_text(encoding="utf-8"), "# user edited fragment\n")
        app.main(["build-step-runners", str(config_path), "--overwrite"])
        self.assertIn(CUSTOM_GRIDPACK, custom_fragment.read_text(encoding="utf-8"))

    def test_missing_gridpack_aborts_before_writing_the_fragment(self):
        config_path = self.write_default_config()
        self._add_gridpack(config_path)
        checker = RecordingGridpackChecker(FileNotFoundError(f"Gridpack does not exist or is not accessible: {CUSTOM_GRIDPACK}"))
        app = self.create_application(gridpack_checker=checker)
        app.main(["download", str(config_path)])

        with self.assertRaisesRegex(FileNotFoundError, "Gridpack does not exist or is not accessible"):
            app.main(["build-step-runners", str(config_path)])

        self.assertEqual(checker.checked_uris, [CUSTOM_GRIDPACK])
        self.assertFalse((self.repo / "configs" / "run3" / "2024" / "fragments" / f"{SAMPLE_NAME}-fragment.py").exists())

    def test_rejects_non_divisible_fan_in(self):
        config_path = self.write_default_config()
        config_path.write_text(config_path.read_text(encoding="utf-8").replace("events_per_job: 1000", "events_per_job: 150"), encoding="utf-8")
        app = self.create_application()
        app.main(["download", str(config_path)])
        with self.assertRaisesRegex(ValueError, r"events_per_job=150.*not a whole-number multiple.*events_per_job=100"):
            app.main(["build-step-runners", str(config_path)])

    def test_build_all_reports_a_summary_and_rebuilds_incompatible_plans(self):
        config_path = self.write_default_config()
        config_dir = self.repo / "configs" / "run3" / "2024"
        (config_dir / f"{SAMPLE_NAME}_copy.yaml").write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        (config_dir / "broken_sample.yaml").write_text("total_events: 10\nbase_output_path: /tmp\n", encoding="utf-8")
        app = self.create_application()

        def build_all(*extra):
            stderr = StringIO()
            with mock.patch.object(Path, "cwd", return_value=self.repo), redirect_stderr(stderr):
                result = app.main(["build-step-runners", "--all", *extra])
            return result, stderr.getvalue()

        with mock.patch.object(Path, "cwd", return_value=self.repo):
            app.main(["download", "--all"])
        result, output = build_all()
        self.assertEqual(result, 1)
        self.assertIn("Build-step-runners summary: built=2 skipped=0 failed=1", output)
        self.assertIn("Build-step-runners summary: built=0 skipped=2 failed=1", build_all()[1])
        self.assertIn("built=2 skipped=0 failed=1", build_all("--overwrite")[1])

        plan_path = self.repo / PLANS_ROOT / "run3" / "2024" / f"{SAMPLE_NAME}.json"
        for corrupt in ("[]\n", json.dumps({key: value for key, value in self.load_plan().items() if key != "plan_version"})):
            with self.subTest(corrupt=corrupt[:20]):
                plan_path.write_text(corrupt, encoding="utf-8")
                stderr = StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(app.main(["build-step-runners", str(config_path)]), 0)
                self.assertIn("Rebuilding incompatible step plan", stderr.getvalue())
                self.assertEqual(self.load_plan()["plan_version"], StepPlanBuilder.PLAN_VERSION)

    def test_validate_plan_payload_checks_command_kinds(self):
        def step(commands):
            return {
                "step_index": 0, "name": "s", "slug": "s", "filename": "s", "events_per_job": 100, "cores_per_job": 1,
                "memory_per_job": 2, "job_count": 1, "predecessor_step": None, "commands": commands,
                "final_output_name": commands[-1]["fileout"],
            }

        def payload(commands):
            return {"plan_version": StepPlanBuilder.PLAN_VERSION, "sample_name": "s", "base_output_path": "/tmp/out",
                    "total_events": 100, "gridpack": None, "steps": [step(commands)]}

        gen = {"command_index": 0, "kind": "gen", "scram_arch": "el8_amd64_gcc12", "cmssw_release": "CMSSW_14_0_21",
               "fragment_path": None, "fragment_target": None, "options": [], "filein": None, "fileout": "file:gen.root",
               "python_filename": "cfg.py"}
        delphes = {"command_index": 1, "kind": "delphes", "card": "/repo/c.tcl", "delphes_executable": "DelphesHepMC2",
                   "filein": "gen.hepmc", "fileout": "delphes.root"}
        nano = {"command_index": 0, "kind": "nano", "filein": "delphes.root", "fileout": "nano.root"}
        StepPlanBuilder.validate_plan_payload(payload([gen, delphes]))
        StepPlanBuilder.validate_plan_payload(payload([nano]))
        for command, message in [
            ({k: v for k, v in delphes.items() if k != "card"}, "card"),
            ({k: v for k, v in nano.items() if k != "filein"}, "filein"),
            ({"command_index": 0, "kind": "bogus", "filein": None, "fileout": "out.root"}, "unknown kind"),
            (dict(gen, options=[{"name": "--number", "value": "5"}]), "runtime-managed"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                StepPlanBuilder.validate_plan_payload(payload([command]))

    def test_delphes_config_builds_gen_delphes_and_nano_commands(self):
        self.config_path.write_text(DELPHES_CONFIG.format(nano_epj=200), encoding="utf-8")
        app = self.create_application()
        app.main(["download", str(self.config_path)])
        app.main(["build-step-runners", str(self.config_path)])

        plan = self.load_plan()
        StepPlanBuilder.validate_plan_payload(plan)
        delphes_step, nano_step = plan["steps"]
        gen, delphes = delphes_step["commands"]
        self.assertEqual((gen["kind"], delphes["kind"]), ("gen", "delphes"))
        options = {o["name"]: o["value"] for o in gen["options"] if o["name"]}
        self.assertEqual((options["--step"], options["--eventcontent"], options["--datatier"]), ("LHE,GEN", "RAWSIM", "GEN"))
        self.assertIn("Casino/HepMCWriter/customise.dumpHepMC", [o["value"] for o in gen["options"] if o["name"] == "--customise"])
        self.assertEqual(gen["hepmc_output"], "gen.hepmc")
        self.assertEqual(gen["extra_cmssw_packages"], [{"source": "cmssw/Casino/HepMCWriter", "dest": "Casino/HepMCWriter"}])
        self.assertEqual((delphes["delphes_executable"], delphes["filein"], delphes["fileout"]), ("DelphesHepMC2", "gen.hepmc", "delphes.root"))
        self.assertEqual(delphes["card"], "cards/delphes/delphes_card_CMS.tcl")
        self.assertEqual(delphes_step["final_output_name"], "delphes.root")
        self.assertEqual(nano_step["commands"], [{"command_index": 0, "kind": "nano", "filein": "delphes.root", "fileout": "nanoAODv15.root"}])
        self.assertEqual((delphes_step["job_count"], nano_step["job_count"], nano_step["predecessor_step"]), (2, 1, "delphes"))

        bad = self.repo / "configs" / "run3" / "2024" / "delphes_bad_fanin.yaml"
        bad.write_text(DELPHES_CONFIG.format(nano_epj=150), encoding="utf-8")
        app.main(["download", str(bad)])
        with self.assertRaisesRegex(ValueError, "multiple"):
            app.main(["build-step-runners", str(bad)])


class FakeDAS:
    def __init__(self, generator="HIG-RunIII2024Summer24wmLHEGS-00735"):
        self._generator = generator

    def summary(self, ds):
        return {"nevents": 957670}

    def mcm_prepid(self, ds):
        return "HIG-RunIII2024Summer24NanoAODv15-00308" if "NANOAODSIM" in ds else "HIG-RunIII2024Summer24MiniAODv6-00307"

    def parent(self, ds):
        return MINI

    def find_generator(self, ds):
        return self._generator


def fake_fetcher_for(chains):
    def fetch(url, timeout, verify_certs):
        return json.dumps({"results": {"member_of_chain": chains}})
    return fetch


class FakeAuth:
    def __init__(self, members=None):
        self.members = members

    def get_chained_request(self, chain):
        if self.members is None:
            raise McMAuthUnavailable("redirected to SSO")
        return {"chain": self.members}


class ChainResolverTests(unittest.TestCase):
    def test_select_chain(self):
        nano = "HIG-RunIII2024Summer24NanoAODv15-00308"
        self.assertEqual(select_chain([CHAIN], nano), CHAIN)
        with self.assertRaises(NoChainFound):
            select_chain([], nano)
        with self.assertRaises(NoChainFound):
            select_chain([CHAIN.replace("NanoAODv15", "NanoAODv12")], nano)
        with self.assertRaises(AmbiguousChain) as ctx:
            select_chain([CHAIN, CHAIN.replace("-00118", "-00119")], nano)
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_proxy_resolver(self):
        resolver = ProxyChainResolver(FakeDAS(), fake_fetcher_for([CHAIN]))
        chain = resolver.resolve(NANO, "delphes")
        self.assertTrue(chain.sample_name.startswith("GluGluHHto2B2Tau"))
        self.assertEqual(chain.total_events, 957670)
        self.assertEqual([s.kind for s in chain.steps], ["wmLHEGS"])

        chain = resolver.resolve(NANO, "full-chain", drpremix=DRPREMIX)
        self.assertEqual([s.kind for s in chain.steps], ["wmLHEGS", "DRPremix", "MiniAOD", "NanoAOD"])
        self.assertEqual(chain.step("MiniAOD").prep_id, "HIG-RunIII2024Summer24MiniAODv6-00307")
        with self.assertRaises(DrpremixUnresolved):
            resolver.resolve(NANO, "full-chain")

        unresolved = ProxyChainResolver(FakeDAS(generator=None), fake_fetcher_for([GS_CHAIN]))
        with self.assertRaises(WmlhegsUnresolved):
            unresolved.resolve(NANO, "delphes")
        chain = unresolved.resolve(NANO, "delphes", wmlhegs="GEN-RunIII2024Summer24GS-00054")
        self.assertEqual(chain.step("GS").prep_id, "GEN-RunIII2024Summer24GS-00054")

    def test_ordered_members_prefers_the_structured_chain(self):
        members = ["B2G-RunIII2024Summer24wmLHEGS-02420", "SUS-RunIII2024Summer24DRPremix-02431",
                   "X9-RunIII2024Summer24MiniAODv6-02431", "B2G-RunIII2024Summer24NanoAODv15-02431"]
        ordered = AuthenticatedChainResolver._ordered_members
        self.assertEqual(ordered({"chain": members, "unrelated": "HIG-RunIII2024Summer24NanoAODv15-99999"}), members)
        self.assertEqual(ordered({"chain": {"members": members[:1]}}), members[:1])
        self.assertEqual(ordered({"chain": [], "request_data": {"first": members[:2], "dup": members[0]}}), members[:2])

    def test_resolve_chain_falls_back_to_proxy_when_sso_fails(self):
        seen = []
        chain = resolve_chain(NANO, "full-chain", das=FakeDAS(), fetcher=fake_fetcher_for([CHAIN]), auth_session=FakeAuth(),
                              prefer_auth=True, drpremix=DRPREMIX, on_auth_unavailable=lambda exc: seen.append(str(exc)))
        self.assertEqual(seen, ["redirected to SSO"])
        self.assertEqual(chain.step("DRPremix").prep_id, DRPREMIX)

        partial = FakeAuth(["HIG-RunIII2024Summer24wmLHEGS-00735", "HIG-RunIII2024Summer24MiniAODv6-00307",
                            "HIG-RunIII2024Summer24NanoAODv15-00308"])
        chain = resolve_chain(NANO, "full-chain", das=FakeDAS(), fetcher=fake_fetcher_for([CHAIN]), auth_session=partial,
                              prefer_auth=True, drpremix=DRPREMIX)
        self.assertEqual(chain.step("DRPremix").prep_id, DRPREMIX)

        gs_auth = FakeAuth(["GEN-RunIII2024Summer24GS-00054", "GEN-RunIII2024Summer24DRPremix-00084",
                            "GEN-RunIII2024Summer24MiniAODv6-00084", "GEN-RunIII2024Summer24NanoAODv15-00084"])
        chain = resolve_chain(NANO, "delphes", das=FakeDAS(generator=None), fetcher=fake_fetcher_for([GS_CHAIN]),
                              auth_session=gs_auth, prefer_auth=True)
        self.assertEqual([(s.kind, s.prep_id) for s in chain.steps], [("GS", "GEN-RunIII2024Summer24GS-00054")])


def full_chain(generator=ResolvedStep("wmLHEGS", "HIG-RunIII2024Summer24wmLHEGS-00735")):
    return ResolvedChain(
        dataset=NANO, sample_name="GluGluHH", campaign="RunIII2024Summer24", chain_prepid="HIG-chain_x-1", total_events=957670,
        steps=(generator, ResolvedStep("DRPremix", DRPREMIX), ResolvedStep("MiniAOD", "HIG-RunIII2024Summer24MiniAODv6-00307"),
               ResolvedStep("NanoAOD", "HIG-RunIII2024Summer24NanoAODv15-00308")),
    )


SIZES = dict(total_events=50000, cores=2, memory=6, events_per_job_mini=250, events_per_job_nano=25000, defaults=Defaults())


class ConfigGeneratorTests(unittest.TestCase):
    def test_parse_campaign_location(self):
        for campaign, expected in [("RunIII2024Summer24", ("run3", "2024")), ("RunII2018Autumn18", ("run2", "2018")),
                                   ("Phase2Spring2026", (None, "2026")), ("RunIIINoYear", ("run3", None)), ("Weird", (None, None))]:
            self.assertEqual(parse_campaign_location(campaign), expected, campaign)

    def test_full_chain_config_round_trips_through_the_loader(self):
        config = build_config(full_chain(), mode="full-chain", gridpack="root://host//store/user/${USER}/gp.tar.xz", **SIZES)
        self.assertEqual([s["name"] for s in config["steps"]], ["MiniAODv6", "NanoAODv15"])
        self.assertEqual(config["steps"][0]["setup_ids"][0], "HIG-RunIII2024Summer24wmLHEGS-00735")
        path = Path(tempfile.mkdtemp()) / "s.yaml"
        ConfigWriter().write(config, path)
        with self.assertRaises(FileExistsError):
            ConfigWriter().write(config, path)
        with mock.patch.dict(os.environ, {"USER": "u"}):
            loaded = ConfigLoader().load(path)
        self.assertEqual(loaded["gridpack"], "root://host//store/user/u/gp.tar.xz")
        self.assertEqual(loaded["steps"], config["steps"])

        gs_chain = full_chain(ResolvedStep("GS", "GEN-RunIII2024Summer24GS-00054"))
        self.assertEqual(build_config(gs_chain, mode="full-chain", **SIZES)["steps"][0]["setup_ids"][0], "GEN-RunIII2024Summer24GS-00054")

    def test_delphes_config(self):
        card = "cards/delphes/delphes_card_CMS_hhbbtt_v0.tcl"
        config = build_config(full_chain(), mode="delphes", card=card, **SIZES)
        self.assertEqual(config["steps"][0]["setup_ids"], ["HIG-RunIII2024Summer24wmLHEGS-00735", f"delphes:{card}"])
        self.assertEqual(config["steps"][1]["setup_ids"], ["delphes-nano"])
        self.assertTrue(config["base_output_path"].endswith("/mc_production/delphes"))
        path = Path(tempfile.mkdtemp()) / "s.yaml"
        ConfigWriter().write(config, path)
        ConfigLoader().load(path)

        tree_only = build_config(full_chain(), mode="delphes", card=card, include_delphes_nano=False, **SIZES)
        self.assertEqual([s["name"] for s in tree_only["steps"]], ["delphes-tree"])
        self.assertNotIn("delphes-nano", dump_config(tree_only))
        ConfigWriter().write(tree_only, path, force=True)
        ConfigLoader().load(path)
        with self.assertRaises(ValueError):
            build_config(full_chain(), mode="delphes", **SIZES)

    def test_render_law_command(self):
        extra = "--htcondor-requirements '(A)' --poll-interval 15"
        self.assertEqual(render_law_command("$PWD/x.yaml", "htcondor", extra),
                         'poetry run law run RunSample --config "$PWD/x.yaml" --workflow htcondor ' + extra)
        self.assertEqual(render_law_command("$PWD/x.yaml", "local", extra),
                         'poetry run law run RunSample --config "$PWD/x.yaml" --workflow local')


FULL_CONFIG = """total_events: 2500000
base_output_path: root://host//store/user/x/mc_production
steps:
- name: MiniAODv6
  filename: miniAODv6
  setup_ids:
  - HIG-RunIII2024Summer24wmLHEGS-01175
  - HIG-RunIII2024Summer24DRPremix-00530
  - HIG-RunIII2024Summer24MiniAODv6-00530
  events_per_job: 250
  cores_per_job: 2
  memory_per_job: 6 # in GB
- name: NanoAODv15
  filename: nanoAODv15
  setup_ids:
  - HIG-RunIII2024Summer24NanoAODv15-00308
  events_per_job: 25000
  cores_per_job: 2
  memory_per_job: 6 # in GB
"""


class ConfigListerTests(unittest.TestCase):
    def test_lists_years_and_summaries(self):
        repo = Path(tempfile.mkdtemp())
        config_dir = repo / "configs" / "run3" / "2024"
        config_dir.mkdir(parents=True)
        (config_dir / "Sample_full.yaml").write_text(FULL_CONFIG, encoding="utf-8")
        (config_dir / "Sample_Delphes.yaml").write_text(DELPHES_CONFIG.format(nano_epj=100), encoding="utf-8")

        self.assertEqual(available_years(repo), [("run3/2024", 2)])
        summaries = {s.name: s for s in configs_for_year(repo, "run3/2024")}
        self.assertEqual((summaries["Sample_full"].mode, summaries["Sample_full"].total_events, summaries["Sample_full"].n_steps),
                         ("full-chain", 2500000, 2))
        self.assertEqual(summaries["Sample_Delphes"].mode, "delphes")
        self.assertIn('--config "$PWD/configs/run3/2024/Sample_full.yaml" --workflow htcondor --htcondor-requirements',
                      summaries["Sample_full"].law_command)
