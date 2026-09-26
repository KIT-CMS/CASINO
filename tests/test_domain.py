import tempfile
import unittest
from pathlib import Path

from domain.chain import ResolvedChain, ResolvedStep, campaign_of, classify_prepid, drpremix_variant_of, sample_name_of
from domain.configuration import ConfigLoader, classify_setup_id, step_slug
from domain.defaults import Defaults, load_defaults
from tests.support import CasinoTestCase

CUSTOM_GRIDPACK = "root://cmsdcache-kit-disk.gridka.de:1094//store/user/${USER}/gridpacks/ggHH_kl_m1.tgz"
CHAIN = ("HIG-chain_RunIII2024Summer24wmLHEGS_flowRunIII2024Summer24DRPremixNoOutput_"
         "flowRunIII2024Summer24MiniAODv6_flowRunIII2024Summer24NanoAODv15-00118")
HEADER = "total_events: 100\nbase_output_path: /tmp/out\n"
MCM_STEP = (
    "- name: gen\n  setup_ids:\n  - HIG-RunIII2024Summer24wmLHEGS-01175\n"
    "  events_per_job: 100\n  cores_per_job: 1\n  memory_per_job: 2\n"
)
DELPHES_STEP = (
    "- name: delphes\n  setup_ids:\n  - HIG-RunIII2024Summer24wmLHEGS-01175\n"
    "  - delphes:cards/delphes/delphes_card_CMS.tcl\n"
    "  events_per_job: 100\n  cores_per_job: 1\n  memory_per_job: 2\n"
)
NANO_STEP = "- name: delphes-nano\n  setup_ids:\n  - delphes-nano\n  events_per_job: 100\n  cores_per_job: 1\n  memory_per_job: 2\n"


def _load(text: str) -> dict:
    path = Path(tempfile.mkdtemp()) / "configs" / "run3" / "2024" / "s.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return ConfigLoader().load(path)


class ConfigLoaderTests(CasinoTestCase):
    def test_loads_config_and_expands_variables(self):
        config_path = self.write_default_config()
        text = config_path.read_text(encoding="utf-8").replace(
            "base_output_path: /ceph/sdaigler",
            f"base_output_path: root://grid.example//store/user/${{USER}}/mc\ngridpack: {CUSTOM_GRIDPACK}",
        )
        config_path.write_text(text, encoding="utf-8")

        config = ConfigLoader().load(config_path)

        self.assertEqual(config["base_output_path"], "root://grid.example//store/user/casino-test-user/mc")
        self.assertIn("/store/user/casino-test-user/gridpacks/", config["gridpack"])
        self.assertEqual([step["name"] for step in config["steps"]], ["MiniAODv6", "NanoAODv15"])
        self.assertEqual(config["steps"][1], {
            "name": "NanoAODv15", "setup_ids": ["HIG-RunIII2024Summer24NanoAODv15-00308"],
            "events_per_job": 1000, "cores_per_job": 4, "memory_per_job": 4,
        })
        self.assertEqual(len(ConfigLoader().unique_setup_ids(config)), 4)

    def test_rejects_invalid_configs(self):
        cases = [
            ("total_events: 1\nbase_output_path: /tmp\nsteps:\n- name: Step\n  setup_ids:\n  - X\n",
             r"Step 1 is missing required keys: \['cores_per_job', 'events_per_job', 'memory_per_job'\]"),
            (HEADER + "steps:\n" + MCM_STEP + "gridpack: root://h//p\n", "after steps are not supported"),
            (HEADER + "gridpack:\nsteps:\n" + MCM_STEP, "non-empty root:// URI"),
            (HEADER + "gridpack: true\nsteps:\n" + MCM_STEP, "non-empty root:// URI"),
            (HEADER + "gridpack: /ceph/gridpack.tgz\nsteps:\n" + MCM_STEP, "non-empty root:// URI"),
            (HEADER + "gridpack: root://\nsteps:\n" + MCM_STEP, "non-empty root:// URI"),
        ]
        for text, message in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, message):
                    _load(text)

    def test_delphes_stages(self):
        self.assertEqual(classify_setup_id("HIG-RunIII2024Summer24wmLHEGS-01175"), ("mcm", None))
        self.assertEqual(classify_setup_id("delphes-nano"), ("nano", None))
        self.assertEqual(classify_setup_id("delphes:cards/x.tcl"), ("delphes", "cards/x.tcl"))
        self.assertEqual(step_slug("Mini AOD/v6"), "Mini-AOD-v6")

        config = _load(HEADER + "steps:\n" + DELPHES_STEP + NANO_STEP)
        self.assertEqual(config["steps"][1]["setup_ids"], ["delphes-nano"])
        self.assertEqual(ConfigLoader().unique_setup_ids(config), ["HIG-RunIII2024Summer24wmLHEGS-01175"])

        cases = [
            (DELPHES_STEP.replace("delphes:cards/delphes/delphes_card_CMS.tcl", "delphes"), "card"),
            (DELPHES_STEP + NANO_STEP.replace("  - delphes-nano\n", "  - delphes-nano\n  - X\n"), "delphes-nano"),
            (NANO_STEP, "predecessor"),
            (MCM_STEP + NANO_STEP, "predecessor step must be a delphes step"),
        ]
        for steps, message in cases:
            with self.subTest(steps=steps):
                with self.assertRaisesRegex(ValueError, message):
                    _load(HEADER + "steps:\n" + steps)


class ChainParsingTests(unittest.TestCase):
    def test_classifies_prep_ids(self):
        for prep_id, kind in [
            ("HIG-RunIII2024Summer24wmLHEGS-01175", "wmLHEGS"),
            ("GEN-RunIII2024Summer24GS-00054", "GS"),
            ("HIG-RunIII2024Summer24DRPremix-00530", "DRPremix"),
            ("HIG-RunIII2024Summer24MiniAODv6-00307", "MiniAOD"),
            ("HIG-RunIII2024Summer24NanoAOD-00009", "NanoAOD"),
            ("HIG-RunIII2024Summer24Foo-00001", None),
        ]:
            self.assertEqual(classify_prepid(prep_id), kind, prep_id)
        self.assertEqual(campaign_of("GEN-RunIII2024Summer24MiniAODv6-00007"), "RunIII2024Summer24")
        self.assertEqual(drpremix_variant_of(CHAIN), "DRPremixNoOutput")
        self.assertEqual(drpremix_variant_of(CHAIN.replace("DRPremixNoOutput", "DRPremix")), "DRPremix")
        self.assertEqual(sample_name_of("/GluGluHH/RunIII2024Summer24NanoAODv15-x/NANOAODSIM"), "GluGluHH")

        chain = ResolvedChain(
            dataset="/a/b/NANOAODSIM", sample_name="a", campaign="RunIII2024Summer24", chain_prepid=CHAIN,
            total_events=100, steps=(ResolvedStep("wmLHEGS", "HIG-...wmLHEGS-1"),),
        )
        self.assertEqual(chain.step("wmLHEGS").prep_id, "HIG-...wmLHEGS-1")
        self.assertIsNone(chain.step("DRPremix"))


class DefaultsTests(unittest.TestCase):
    def test_load_defaults_overrides_known_keys_only(self):
        repo = Path(tempfile.mkdtemp())
        self.assertEqual(load_defaults(repo), Defaults())
        (repo / "data").mkdir()
        (repo / "data" / "defaults.json").write_text('{"memory_per_job_gb": 8, "totally_unknown": 1}')

        defaults = load_defaults(repo)

        self.assertEqual(defaults.memory_per_job_gb, 8)
        self.assertEqual(defaults.cores_per_job, 2)
        self.assertEqual(defaults.total_events, 50000)
        self.assertIn("--htcondor-requirements", defaults.htcondor_extra_args)
