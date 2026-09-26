import importlib.util
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.support import REPO_ROOT


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class GenerateProductionCommandsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script("generate_production_commands")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name)
        self.config_dir = self.repo_root / "configs" / "run3" / "2024" / "production"
        self.config_dir.mkdir(parents=True)

    def test_generates_one_numbered_tmux_command_per_config(self):
        for name in ("z.yaml", "a.yaml", "notes.txt"):
            (self.config_dir / name).write_text(name, encoding="utf-8")
        (self.config_dir / "nested").mkdir()
        (self.config_dir / "nested" / "nested.yaml").write_text("ignored", encoding="utf-8")
        (self.repo_root / "data").mkdir()
        (self.repo_root / "data" / "defaults.json").write_text(json.dumps({
            "workflow": "htcondor", "htcondor_extra_args": "--htcondor-requirements '(CUSTOM)' --poll-interval 30",
        }), encoding="utf-8")
        output_path = self.config_dir / "start-production.sh"
        output_path.write_text("stale content\n", encoding="utf-8")

        generated_path, count = self.module.generate_launch_script(self.config_dir, self.repo_root)

        self.assertEqual((generated_path, count), (output_path, 2))
        content = output_path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("#!/usr/bin/env bash\n"))
        self.assertNotIn("stale content", content)
        commands = [shlex.split(line) for line in content.splitlines() if line.startswith("tmux ")]
        self.assertEqual([argv[:7] for argv in commands], [["tmux", "new-session", "-d", "-s", str(i), "bash", "-lc"] for i in (1, 2)])
        self.assertIn("$PWD/configs/run3/2024/production/a.yaml", commands[0][7])
        for needle in ["source ~/.bashrc", f"cd {self.repo_root}", "conda activate casino-py3.11", "source ./setup.sh",
                       "poetry run law run RunSample", "--workflow htcondor", "(CUSTOM)", "--poll-interval 30"]:
            self.assertIn(needle, commands[0][7])
        for path, message in [(self.repo_root / "missing", "does not exist"), (self.config_dir / "a.yaml", "not a directory"),
                              (self.config_dir / "nested" / "empty", "no YAML configs")]:
            (self.config_dir / "nested" / "empty").mkdir(exist_ok=True)
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, message):
                self.module.discover_configs(path)

    def test_main_prints_a_single_yaml_and_rejects_invalid_inputs(self):
        config_path = self.config_dir / "sample.yaml"
        config_path.write_text("sample", encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch.object(self.module, "REPO_ROOT", self.repo_root), redirect_stdout(stdout):
            self.assertEqual(self.module.main([str(config_path)]), 0)
        argv = shlex.split(stdout.getvalue().strip())
        self.assertEqual(argv[:5], ["tmux", "new-session", "-d", "-s", "1"])
        self.assertIn("$PWD/configs/run3/2024/production/sample.yaml", argv[7])
        self.assertFalse((self.config_dir / "start-production.sh").exists())

        (self.config_dir / "notes.txt").write_text("not a config", encoding="utf-8")
        for argv in ([str(self.config_dir / "notes.txt")], [str(self.repo_root / "missing")]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as context:
                self.module.main(argv)
            self.assertEqual(context.exception.code, 2)


class FakeAxis:
    def __init__(self):
        self.title = ""

    def SetTitle(self, title):
        self.title = title


class FakeHistogram:
    def __init__(self, name, bins):
        self.name, self.bins, self.directory, self.line_width = name, list(bins), None, None
        self.x_axis, self.y_axis, self.sumw2_called, self.draw_options = FakeAxis(), FakeAxis(), False, []

    def Clone(self, name):
        return FakeHistogram(name, self.bins)

    def Sumw2(self):
        self.sumw2_called = True

    def Integral(self):
        return sum(self.bins)

    def Scale(self, factor):
        self.bins = [value * factor for value in self.bins]

    def Divide(self, other):
        self.bins = [a / b if b else 0.0 for a, b in zip(self.bins, other.bins)]

    def SetDirectory(self, directory):
        self.directory = directory

    def SetLineWidth(self, width):
        self.line_width = width

    def GetMaximum(self):
        return max(self.bins)

    def GetXaxis(self):
        return self.x_axis

    def GetYaxis(self):
        return self.y_axis

    def Draw(self, option):
        self.draw_options.append(option)


class FakeRootModule:
    def __init__(self, histogram):
        self.histogram = histogram
        self.canvas = mock.Mock()
        self.line = mock.Mock()
        self.gDirectory = mock.Mock(Get=lambda name: histogram if name == histogram.name else None)

    def TCanvas(self, *args):
        return self.canvas

    def TLine(self, *args):
        return self.line


class PlotScriptsTests(unittest.TestCase):
    def setUp(self):
        self.variables = load_script("plot_nanoaod_variables")
        self.ratio = load_script("plot_nanoaod_ratio")

    def test_plot_modes_and_output_paths(self):
        delphes = {plot.required_branch for plot in self.variables.plots_for_mode(delphes=True)}
        default = {plot.required_branch for plot in self.variables.plots_for_mode(delphes=False)}
        self.assertTrue({"Electron_pt", "Jet_tauTag", "Jet_tauWeight"} <= delphes)
        self.assertNotIn("Tau_pt", delphes)
        self.assertTrue({"Tau_pt", "Tau_decayMode"} <= default)
        self.assertTrue(self.variables.build_parser().parse_args(["--delphes", "/tmp/x"]).delphes)

        dataset = "/GluGluHH_TuneCP5/RunIII2024Summer24NanoAODv15-v2/NANOAODSIM"
        self.assertEqual(self.variables.dataset_output_relative_path(dataset, None), Path("GluGluHH_TuneCP5") / "RunIII2024Summer24NanoAODv15-v2")
        self.assertEqual(self.variables.resolve_output_root(None, is_dataset=True), Path("artifacts") / "reference-plots")
        self.assertEqual(self.variables.resolve_output_root("custom", is_dataset=False), Path("custom"))
        self.assertEqual(self.ratio.ratio_output_relative_path("/tmp/out/Sample/NanoAODv15-abc", dataset, None, None),
                         Path("Sample") / "NanoAODv15-abc" / "vs_RunIII2024Summer24NanoAODv15-v2")

    def test_das_file_discovery(self):
        output = "/store/mc/file_1.root\nDAS succeeded after 1 attempts 0\n/store/mc/not.txt\nmeta /store/mc/file_2.root\n/store/mc/file_1.root\n"
        self.assertEqual(self.variables.parse_das_file_output(output), ["/store/mc/file_1.root", "/store/mc/file_2.root"])
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="/store/mc/file_1.root\n/store/mc/file_2.root\n", stderr="")
        with mock.patch.object(self.variables.subprocess, "run", return_value=completed) as run:
            files = self.variables.discover_das_dataset_files("/A/B/C", "prod/global", "cms-xrd-global.cern.ch", max_files=1)
        self.assertEqual(files, ["root://cms-xrd-global.cern.ch//store/mc/file_1.root"])
        run.assert_called_once_with(["dasgoclient", "--query=file dataset=/A/B/C instance=prod/global", "--limit=1"],
                                    check=True, capture_output=True, text=True)
        with mock.patch.object(self.variables.subprocess, "run", side_effect=FileNotFoundError), \
             self.assertRaisesRegex(RuntimeError, "dasgoclient is required"):
            self.variables.discover_das_dataset_files("/A/B/C", "prod/global", "cms-xrd-global.cern.ch", max_files=10)

    def test_ratio_histograms_are_normalized_and_drawn_with_error_bars(self):
        ratio = self.ratio.build_normalized_ratio_histogram(FakeHistogram("num", [2.0, 2.0]), FakeHistogram("den", [1.0, 3.0]),
                                                            ratio_name="ratio", x_title="p_{T}", y_title="A / B")
        self.assertAlmostEqual(ratio.bins[0], 2.0)
        self.assertAlmostEqual(ratio.bins[1], 2.0 / 3.0)
        self.assertEqual((ratio.GetXaxis().title, ratio.directory, ratio.line_width), ("p_{T}", 0, 2))
        self.assertIsNone(self.ratio.build_normalized_ratio_histogram(FakeHistogram("num", [0.0]), FakeHistogram("den", [1.0]),
                                                                      ratio_name="r", x_title="x", y_title="y"))

        histogram = FakeHistogram("h_mu_pt_nanoaod", [4.0, 9.0])
        root = FakeRootModule(histogram)
        chain = mock.Mock(Draw=mock.Mock(return_value=2))
        with mock.patch.object(self.ratio, "branch_exists", return_value=True):
            drawn = self.ratio.draw_histogram(root, chain, self.ratio.DEFAULT_PLOTS[1], "h_mu_pt_nanoaod", "", "NanoAOD")
        self.assertIs(drawn, histogram)
        self.assertTrue(histogram.sumw2_called)
        self.ratio.save_ratio_plot(root, histogram, self.ratio.DEFAULT_PLOTS[1], Path("plots"), ["png"])
        self.assertEqual(histogram.draw_options, ["E1X0"])
        root.line.SetLineStyle.assert_called_once_with(2)
        root.canvas.SaveAs.assert_called_once_with("plots/h_mu_pt_ratio.png")
