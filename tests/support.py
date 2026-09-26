import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import casino


SAMPLE_NAME = "GluGluHHto2B2Tau_Par-c2-0p00-kl-1p00-kt-1p00_TuneCP5_13p6TeV_powheg-pythia8"
ARTIFACTS_ROOT = Path("artifacts")
MCM_ROOT = ARTIFACTS_ROOT / "mcm-commands"
PLANS_ROOT = ARTIFACTS_ROOT / "step-plans"
TESTDATA_ROOT = REPO_ROOT / "tests" / "testdata" / SAMPLE_NAME
CONFIG_FIXTURE = TESTDATA_ROOT / "config" / f"{SAMPLE_NAME}.yaml"
SETUP_FIXTURE_DIR = TESTDATA_ROOT / "setup"
SETUP_FILE_MAP = {
    "HIG-RunIII2024Summer24wmLHEGS-01175": "lhegs.sh",
    "HIG-RunIII2024Summer24DRPremix-00530": "drpremix.sh",
    "HIG-RunIII2024Summer24MiniAODv6-00530": "miniAODv6.sh",
    "HIG-RunIII2024Summer24NanoAODv15-00308": "nanoAODv15.sh",
}
FRAGMENT_FIXTURE = SETUP_FIXTURE_DIR / "fragment.py"


def load_fixture(path: Path) -> str:
    return path.read_text(encoding="utf-8")


RESPONSES = {
    setup_id: load_fixture(SETUP_FIXTURE_DIR / filename)
    for setup_id, filename in SETUP_FILE_MAP.items()
}


class NoopGridpackChecker:
    def ensure_exists(self, gridpack_uri: str) -> None:
        del gridpack_uri


class CasinoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # setup.sh exports BASE_DIR, which would remap the tmpdir config paths onto the real repo.
        original_base_dir = os.environ.pop("BASE_DIR", None)
        if original_base_dir is not None:
            self.addCleanup(os.environ.__setitem__, "BASE_DIR", original_base_dir)

        # CI containers do not set USER, which the HTCondor job config reads.
        original_user = os.environ.get("USER")
        os.environ["USER"] = "casino-test-user"
        if original_user is None:
            self.addCleanup(os.environ.pop, "USER", None)
        else:
            self.addCleanup(os.environ.__setitem__, "USER", original_user)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmpdir.name)
        (self.repo / "configs" / "run3" / "2024").mkdir(parents=True)
        self.config_path = self.repo / "configs" / "run3" / "2024" / f"{SAMPLE_NAME}.yaml"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def write_default_config(self) -> Path:
        self.config_path.write_text(load_fixture(CONFIG_FIXTURE), encoding="utf-8")
        return self.config_path

    def prepare_plan(self) -> Path:
        """Write the default config, download its McM artifacts and build the step plan."""
        config_path = self.write_default_config()
        app = self.create_application()
        app.main(["download", str(config_path)])
        app.main(["build-step-runners", str(config_path)])
        return config_path

    def load_plan(self, sample_name: str = SAMPLE_NAME) -> dict:
        plan_path = self.repo / PLANS_ROOT / "run3" / "2024" / f"{sample_name}.json"
        return json.loads(plan_path.read_text(encoding="utf-8"))

    def create_application(self, fetcher=None, gridpack_checker=None):
        return casino.create_application(
            fetcher=fetcher or self.fake_fetch_text,
            gridpack_checker=gridpack_checker or NoopGridpackChecker(),
        )

    def fake_fetch_text(self, url: str, timeout: int, verify_certs: bool) -> str:
        del timeout, verify_certs
        setup_id = url.rsplit("/", 1)[-1]
        if "get_fragment" in url:
            self.assertEqual(setup_id, "HIG-RunIII2024Summer24wmLHEGS-01175")
            return load_fixture(FRAGMENT_FIXTURE)
        return RESPONSES[setup_id]

    def fake_fetch_text_json(self, url: str, timeout: int, verify_certs: bool) -> str:
        del timeout, verify_certs
        setup_id = url.rsplit("/", 1)[-1]
        if "get_fragment" in url:
            return json.dumps(load_fixture(FRAGMENT_FIXTURE))
        return json.dumps(RESPONSES[setup_id])
