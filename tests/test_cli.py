import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import casino
from cli.main import _resolve_config_dir, build_launch_command, handle_create_config, handle_mcm_login, run_cli
from cli.prompts import QuestionaryPrompter, ScriptedPrompter
from infrastructure.das import DASClient
from infrastructure.mcm_auth import McMAuthUnavailable

NANO = ("/GluGluHHto2B2Tau_Par-c2-0p00-kl-1p00-kt-1p00_TuneCP5_13p6TeV_powheg-pythia8"
        "/RunIII2024Summer24NanoAODv15-x-v2/NANOAODSIM")
MINI = NANO.replace("NanoAODv15", "MiniAODv6").replace("NANOAODSIM", "MINIAODSIM")
CHAIN = ("HIG-chain_RunIII2024Summer24wmLHEGS_flowRunIII2024Summer24DRPremixNoOutput_"
         "flowRunIII2024Summer24MiniAODv6_flowRunIII2024Summer24NanoAODv15-00118")
SAMPLE = "GluGluHHto2B2Tau_Par-c2-0p00-kl-1p00-kt-1p00_TuneCP5_13p6TeV_powheg-pythia8"
CARD = "cards/delphes/delphes_card_CMS_hhbbtt_v0.tcl"
DELPHES_CONFIG = f"""total_events: 50000
base_output_path: root://host//store/user/x/mc_production/delphes
steps:
- name: delphes-tree
  filename: delphes-tree
  setup_ids:
  - HIG-RunIII2024Summer24wmLHEGS-01175
  - delphes:{CARD}
  events_per_job: 250
  cores_per_job: 2
  memory_per_job: 6 # in GB
"""


def fake_das_runner(table):
    def run(args):
        query = args[-1]
        return next((out for needle, out in table.items() if needle in query), "")
    return run


def fake_fetcher(url, timeout, verify_certs):
    return json.dumps({"results": {"member_of_chain": [CHAIN]}})


class FakeAuth:
    def __init__(self, members=None):
        self.members = members

    def get_chained_request(self, chain):
        if self.members is None:
            raise McMAuthUnavailable("no cookie")
        return {"chain": self.members}


class RecordingPrompter(ScriptedPrompter):
    def __init__(self, answers):
        super().__init__(answers)
        self.text_prompts, self.choice_options, self.choice_defaults = [], [], []

    def text(self, label, default=None):
        self.text_prompts.append((label, default))
        return super().text(label, default)

    def choice(self, label, options, default=None):
        self.choice_options.append(list(options))
        self.choice_defaults.append(default)
        return super().choice(label, options, default)


class CreateConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "configs" / "run3" / "2024").mkdir(parents=True)
        (self.repo / CARD).parent.mkdir(parents=True)
        (self.repo / CARD).write_text("# card")
        self.app = casino.create_application()
        self.das = DASClient(runner=fake_das_runner({
            "dataset dataset=/*GluGluHH*/*/NANOAODSIM": NANO + "\n",
            f"summary dataset={NANO}": '[{"nevents": 957670}]',
            f"mcm dataset={NANO}": "HIG-RunIII2024Summer24NanoAODv15-00308\n",
            f"config dataset={NANO}": "cmsunified_task_HIG-RunIII2024Summer24wmLHEGS-00735__v1_T_x\n",
            f"parent dataset={NANO}": MINI + "\n",
            f"mcm dataset={MINI}": "HIG-RunIII2024Summer24MiniAODv6-00307\n",
        }))

    def _args(self, **overrides):
        base = dict(dataset=None, mode=None, card=None, total_events=None, memory=None, cores=None,
                    events_per_job_mini=None, events_per_job_nano=None, gridpack=None, drpremix=None,
                    wmlhegs=None, chain=None, output_root=None, config_dir=None, force=False,
                    run=False, print_only=True, workflow=None, no_validate=True, no_delphes_nano=False)
        return Namespace(**dict(base, **overrides))

    def _create(self, args, answers, das=None, auth=None):
        out, prompter = [], RecordingPrompter(answers)
        rc = handle_create_config(self.app, args, prompter, self.repo, out.append,
                                  das=das or self.das, fetcher=fake_fetcher, auth_session=auth or FakeAuth())
        return rc, out, prompter

    def _written(self, name: str) -> str:
        return (self.repo / "configs" / "run3" / "2024" / name).read_text(encoding="utf-8")

    def test_interactive_delphes_wizard_writes_config(self):
        # search, pick, mode delphes, card, nano? y, sizes (5 defaults), gridpack? n, start? n
        rc, out, _ = self._create(self._args(), ["GluGluHH", "1", "2", "1", "y", "", "", "", "", "", "n", "n"])

        self.assertEqual(rc, 0)
        text = self._written(f"{SAMPLE}_Delphes.yaml")
        self.assertIn(f"delphes:{CARD}", text)
        self.assertIn("delphes-nano", text)
        self.assertTrue(any("1 dataset" in line for line in out))
        self.assertTrue(any("poetry run law run RunSample" in line for line in out))

    def test_interactive_full_chain_prompts_events_per_job_per_step(self):
        b2g = FakeAuth(["B2G-RunIII2024Summer24wmLHEGS-02420", "B2G-RunIII2024Summer24DRPremix-02431",
                        "B2G-RunIII2024Summer24MiniAODv6-02431", "B2G-RunIII2024Summer24NanoAODv15-02431"])
        rc, _, prompter = self._create(self._args(), ["GluGluHH", "1", "1", "", "", "", "125", "5000", "n"], auth=b2g)

        self.assertEqual(rc, 0)
        self.assertIn(("MiniAODv6 events_per_job", "250"), prompter.text_prompts)
        self.assertIn(("NanoAODv15 events_per_job", "25000"), prompter.text_prompts)
        text = self._written(f"{SAMPLE}.yaml")
        self.assertIn("  events_per_job: 125\n", text)
        self.assertIn("  events_per_job: 5000\n", text)
        self.assertIn("B2G-RunIII2024Summer24wmLHEGS-02420", text)

    def test_interactive_full_chain_prompts_for_drpremix_when_sso_is_unavailable(self):
        rc, out, _ = self._create(self._args(), ["GluGluHH", "1", "1", "HIG-RunIII2024Summer24DRPremix-77777", "", "", "", "", "", "n"])

        self.assertEqual(rc, 0)
        self.assertTrue(any("falling back" in line for line in out))
        self.assertIn("HIG-RunIII2024Summer24DRPremix-77777", self._written(f"{SAMPLE}.yaml"))

    def test_noninteractive_full_chain_uses_flags_and_defaults(self):
        args = self._args(dataset=NANO, mode="full-chain", drpremix="HIG-RunIII2024Summer24DRPremix-00530")
        rc, out, _ = self._create(args, [])

        self.assertEqual(rc, 0)
        text = self._written(f"{SAMPLE}.yaml")
        self.assertIn("total_events: 50000", text)
        self.assertIn("HIG-RunIII2024Summer24DRPremix-00530", text)
        self.assertIn("HIG-RunIII2024Summer24MiniAODv6-00307", text)
        self.assertIn("--htcondor-requirements", "\n".join(out))

    def test_noninteractive_delphes_resolves_the_generator_via_the_grandparent(self):
        nano, mini, aod = ("/TT/RunIII2024Summer24NanoAODv15-x/NANOAODSIM", "/TT/RunIII2024Summer24MiniAODv6-x/MINIAODSIM",
                           "/TT/RunIII2024Summer24DRPremix-x/AODSIM")
        mini_task = "cmsunified_task_GEN-RunIII2024Summer24MiniAODv6-00002__v1_T\n"
        das = DASClient(runner=fake_das_runner({
            f"summary dataset={nano}": '[{"nevents": 1000}]',
            f"mcm dataset={nano}": "GEN-RunIII2024Summer24NanoAODv15-00002\n",
            f"config dataset={nano}": mini_task, f"parent dataset={nano}": mini + "\n",
            f"config dataset={mini}": mini_task, f"parent dataset={mini}": aod + "\n",
            f"config dataset={aod}": mini_task + "cmsunified_task_GEN-RunIII2024Summer24wmLHEGS-00006__v1_T\n",
        }))
        rc, _, _ = self._create(self._args(dataset=nano, mode="delphes", card=CARD, no_delphes_nano=True), [], das=das)

        self.assertEqual(rc, 0)
        text = self._written("TT_Delphes.yaml")
        self.assertIn("GEN-RunIII2024Summer24wmLHEGS-00006", text)
        self.assertNotIn("delphes-nano", text)

    def test_empty_search_aborts(self):
        rc, out, _ = self._create(self._args(), [""])
        self.assertEqual((rc, out[-1]), (1, "No dataset selected."))

    def test_resolve_config_dir(self):
        quiet = lambda *_: None  # noqa: E731
        self.assertEqual(_resolve_config_dir("RunIII2024Summer24", ScriptedPrompter([]), True, quiet), "run3/2024")
        self.assertEqual(_resolve_config_dir("Phase2Spring2026", ScriptedPrompter(["3"]), True, quiet), "run3/2026")
        self.assertEqual(_resolve_config_dir("RunIIINoYear", ScriptedPrompter(["", "2027"]), True, quiet), "run3/2027")
        self.assertEqual(_resolve_config_dir("WeirdCampaign", ScriptedPrompter(["3", "2028"]), True, quiet), "run3/2028")
        out = []
        self.assertIsNone(_resolve_config_dir("WeirdCampaign", ScriptedPrompter([]), False, out.append))
        self.assertIn("--config-dir", out[0])

    def test_build_launch_command_passes_arguments_literally(self):
        extra = ["--htcondor-requirements", '(A)&&(B =!= "x")', "--poll-interval", "15"]
        cmd = build_launch_command("/repo/x/Sample$(touch PWNED).yaml", "htcondor", extra)
        self.assertEqual(cmd[:2], ["bash", "-lc"])
        self.assertIn('--config "$1" --workflow "$2" "${@:3}"', cmd[2])
        self.assertEqual(cmd[4:], ["/repo/x/Sample$(touch PWNED).yaml", "htcondor", *extra])


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        config_dir = self.repo / "configs" / "run3" / "2024"
        config_dir.mkdir(parents=True)
        (config_dir / "Sample_Delphes.yaml").write_text(DELPHES_CONFIG)
        self.app = casino.create_application()

    def _run(self, argv, answers, printer):
        prompter = RecordingPrompter(answers)
        with mock.patch("cli.main.auth_status_lines", return_value=["  X SENTINEL-AUTH"]):
            rc = run_cli(argv, app=self.app, prompter=prompter, repo_root=self.repo, printer=printer)
        return rc, prompter

    def test_list_configs_prints_the_command_only_after_a_selection(self):
        out = []
        rc, prompter = self._run(["list-configs"], ["1", "1"], out.append)
        self.assertEqual(rc, 0)
        self.assertTrue(any("poetry run law run RunSample" in line and "Sample_Delphes" in line for line in out))
        self.assertTrue(any("Sample_Delphes" in opt and "delphes" in opt and "50000 evt" in opt for opt in prompter.choice_options[-1]))
        self.assertFalse(any("SENTINEL-AUTH" in line for line in out))

        out = []
        self._run(["list-configs"], ["1", "2"], out.append)  # Back
        self.assertFalse(any("poetry run law run RunSample" in line for line in out))

    def test_menu_prints_the_auth_banner_and_quits(self):
        out = []
        rc, prompter = self._run([], ["4"], out.append)
        self.assertEqual(rc, 0)
        self.assertTrue(any("SENTINEL-AUTH" in line for line in out))
        self.assertEqual((prompter.choice_options[0][0], prompter.choice_defaults[0]), ("Create config", 0))

    def test_abort_exits_cleanly(self):
        class AbortingPrompter:
            def choice(self, *a, **k):
                raise KeyboardInterrupt

        out = []
        with mock.patch("cli.main.auth_status_lines", return_value=[]):
            rc = run_cli([], app=self.app, prompter=AbortingPrompter(), repo_root=self.repo, printer=out.append)
        self.assertEqual((rc, out[-1]), (130, "Aborted."))


class McmLoginTests(unittest.TestCase):
    def _login(self, out, user=None, **overrides):
        kwargs = dict(login_fn=lambda prompt_otp, cookiefile, printer: cookiefile, authenticator=lambda cf: True,
                      ticket_present=lambda: True, prompt_otp=lambda attempt: "123456")
        kwargs.update(overrides)
        return handle_mcm_login(Namespace(cookiefile="/tmp/probe-cookie.txt", user=user), out.append, **kwargs)

    def test_success_when_the_probe_accepts_the_cookie(self):
        out = []
        self.assertEqual(self._login(out), 0)
        self.assertTrue(any("accept" in line.lower() for line in out))

    def test_reports_rejected_cookie_and_failed_login(self):
        def boom(prompt_otp, cookiefile, printer):
            raise McMAuthUnavailable("2FA aborted")

        out = []
        self.assertEqual(self._login(out, authenticator=lambda cf: False), 1)
        self.assertTrue(any("reject" in line.lower() for line in out))
        out = []
        self.assertEqual(self._login(out, login_fn=boom), 1)
        self.assertTrue(any("2FA aborted" in line for line in out))

    def test_runs_kinit_when_no_ticket_exists(self):
        kinit_calls, login_calls = [], []
        login_fn = lambda **k: login_calls.append(1) or k["cookiefile"]  # noqa: E731
        rc = self._login([], user="sdaigler", login_fn=login_fn, ticket_present=lambda: False,
                         kinit_fn=lambda user: kinit_calls.append(user) or True)
        self.assertEqual((rc, kinit_calls, login_calls), (0, ["sdaigler"], [1]))

        out = []
        rc = self._login(out, user="sdaigler", login_fn=login_fn, ticket_present=lambda: False, kinit_fn=lambda user: False)
        self.assertEqual((rc, login_calls), (1, [1]))
        self.assertTrue(any("kinit" in line.lower() for line in out))
        rc = self._login([], login_fn=login_fn, ticket_present=lambda: False, prompt_user=lambda: "",
                         kinit_fn=lambda user: kinit_calls.append(user) or True)
        self.assertEqual((rc, kinit_calls), (1, ["sdaigler"]))


class PrompterTests(unittest.TestCase):
    def test_scripted_prompter(self):
        prompter = ScriptedPrompter(["", "typed", "2", "y", "n", ""])
        self.assertEqual(prompter.text("a", default="def"), "def")
        self.assertEqual(prompter.text("b", default="def"), "typed")
        self.assertEqual(prompter.choice("pick", ["x", "y", "z"]), 1)
        self.assertEqual([prompter.confirm("ok?"), prompter.confirm("ok?"), prompter.confirm("ok?", default=True)], [True, False, True])

    def test_questionary_prompter_maps_answers_and_aborts(self):
        with mock.patch("questionary.text") as text, mock.patch("questionary.select") as select, mock.patch("questionary.confirm") as confirm:
            text.return_value.ask.return_value = "typed"
            select.return_value.ask.return_value = 1
            confirm.return_value.ask.return_value = True
            self.assertEqual(QuestionaryPrompter().text("label", default="def"), "typed")
            text.assert_called_once_with("label", default="def")
            self.assertEqual(QuestionaryPrompter().choice("pick", ["a", "b", "c"]), 1)
            self.assertEqual([c.value for c in select.call_args.kwargs["choices"]], [0, 1, 2])
            self.assertTrue(QuestionaryPrompter().confirm("ok?"))

            text.return_value.ask.return_value = None
            with self.assertRaises(KeyboardInterrupt):
                QuestionaryPrompter().text("label")
