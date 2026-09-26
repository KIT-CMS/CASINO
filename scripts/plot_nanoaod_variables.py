#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_OUTPUT_ROOT = Path("artifacts") / "nanoaod-plots"
DEFAULT_REFERENCE_OUTPUT_ROOT = Path("artifacts") / "reference-plots"
DEFAULT_DAS_INSTANCE = "prod/global"
DEFAULT_XROOTD_SERVER = "cms-xrd-global.cern.ch"
DEFAULT_MAX_DATASET_FILES = 10


@dataclass(frozen=True)
class PlotSpec:
    expression: str
    histogram: str
    bins: int
    low: float
    high: float
    x_title: str
    required_branch: str

    @property
    def draw_expression(self) -> str:
        return (
            f"{self.expression} >> "
            f"{self.histogram}({self.bins}, {self.low:g}, {self.high:g})"
        )


DEFAULT_PLOTS = [
    PlotSpec("nMuon", "h_n_mu", 10, 0, 10, "Number of muons", "nMuon"),
    PlotSpec("Muon_pt", "h_mu_pt", 50, 0, 150, "Muon p_{T} [GeV]", "Muon_pt"),
    PlotSpec("Muon_phi", "h_mu_phi", 50, -3.2, 3.2, "Muon #phi", "Muon_phi"),
    PlotSpec("nElectron", "h_n_ele", 10, 0, 10, "Number of electrons", "nElectron"),
    PlotSpec("Electron_pt", "h_ele_pt", 50, 0, 150, "Electron p_{T} [GeV]", "Electron_pt"),
    PlotSpec("nTau", "h_n_tau", 10, 0, 10, "Number of taus", "nTau"),
    PlotSpec("Tau_pt", "h_tau_pt", 50, 0, 200, "Tau p_{T} [GeV]", "Tau_pt"),
    PlotSpec("Tau_decayMode", "h_tau_dm", 13, 0, 13, "Tau decay mode", "Tau_decayMode"),
    PlotSpec("nJet", "h_n_jet", 20, 0, 20, "Number of jets", "nJet"),
    PlotSpec("Jet_pt", "h_jet_pt", 60, 0, 300, "Jet p_{T} [GeV]", "Jet_pt"),
]

DELPHES_PLOTS = [
    *DEFAULT_PLOTS[:5],
    *DEFAULT_PLOTS[8:],
    PlotSpec("Jet_tauTag", "h_jet_tau_tag", 2, 0, 2, "Delphes jet tau tag", "Jet_tauTag"),
    PlotSpec(
        "Jet_tauWeight",
        "h_jet_tau_weight",
        50,
        0,
        1,
        "Delphes jet tau weight",
        "Jet_tauWeight",
    ),
]


def plots_for_mode(delphes: bool) -> list[PlotSpec]:
    return DELPHES_PLOTS if delphes else DEFAULT_PLOTS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plot default variables from NanoAOD ROOT files in a local or root:// directory, "
        "or from a DAS dataset."
    )
    parser.add_argument(
        "input_path", nargs="?",
        help="Directory containing NanoAOD .root files, a single .root file, or a root:// directory URI. "
        "Omit when using --dataset.",
    )
    parser.add_argument("--dataset", help="CMS dataset name to query with dasgoclient, e.g. /A/B/NANOAODSIM.")
    parser.add_argument(
        "--das-instance", default=DEFAULT_DAS_INSTANCE,
        help=f"DAS instance appended to the file query; empty to omit. Default: {DEFAULT_DAS_INSTANCE}",
    )
    parser.add_argument(
        "--xrootd-server", default=DEFAULT_XROOTD_SERVER,
        help=f"XRootD redirector for DAS logical file names (host or root:// URI). Default: {DEFAULT_XROOTD_SERVER}",
    )
    parser.add_argument(
        "--max-files", type=int, default=DEFAULT_MAX_DATASET_FILES,
        help=f"Maximum number of DAS files in --dataset mode; 0 for no cap. Default: {DEFAULT_MAX_DATASET_FILES}",
    )
    parser.add_argument(
        "-o", "--output-root", default=None,
        help=f"Directory for the plots. Defaults to {DEFAULT_OUTPUT_ROOT} for INPUT_PATH "
        f"and {DEFAULT_REFERENCE_OUTPUT_ROOT} for --dataset.",
    )
    parser.add_argument(
        "--label",
        help="Leaf output subdirectory. Defaults to the final NanoAOD path component of INPUT_PATH, "
        "or the processed dataset name in --dataset mode.",
    )
    parser.add_argument(
        "--format", action="append", dest="formats", default=None,
        help="Output format for TCanvas.SaveAs, e.g. png or pdf. Can be repeated. Default: png",
    )
    parser.add_argument("--selection", default="", help="ROOT TTree::Draw selection applied to every plot.")
    parser.add_argument(
        "--delphes", action="store_true",
        help="Plot Delphes-derived NanoAOD fields: Jet_tauTag and Jet_tauWeight instead of the CMS Tau plots.",
    )
    parser.add_argument("--recursive", action="store_true", help="Search .root files recursively below INPUT_PATH.")
    parser.add_argument(
        "--plot-each-file", action="store_true",
        help="Also plot each ROOT file separately into a subdirectory named after it.",
    )
    parser.add_argument("--tree", default="Events", help="TTree/TChain name inside the NanoAOD files. Default: Events")
    return parser


def is_remote_root_uri(path: str) -> bool:
    return path.startswith("root://")


def parse_remote_root_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "root" or not parsed.netloc:
        raise ValueError(f"Invalid root:// URI: {uri}")

    remote_path = "/" + parsed.path.lstrip("/")
    return parsed.netloc, remote_path


def make_remote_root_uri(server: str, path: str) -> str:
    if path.startswith("root://"):
        return path
    return f"root://{server}//{path.lstrip('/')}"


def list_remote_root_files(uri: str, recursive: bool) -> list[str]:
    if uri.endswith(".root"):
        return [uri]

    server, remote_path = parse_remote_root_uri(uri)
    command = ["xrdfs", server, "ls"]
    if recursive:
        command.append("-R")
    command.append(remote_path)

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "xrdfs is required to list remote root:// directories, but it is not in PATH"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(f"Failed to list remote directory {uri}{detail}") from exc

    files = []
    for line in result.stdout.splitlines():
        entry = line.strip()
        if not entry:
            continue
        path = entry.split()[-1]
        if path.endswith(".root"):
            files.append(make_remote_root_uri(server, path))

    return sorted(files)


def list_local_root_files(path_text: str, recursive: bool) -> list[str]:
    path = Path(path_text).expanduser()
    if path.is_file():
        return [str(path)] if path.suffix == ".root" else []
    if not path.is_dir():
        raise RuntimeError(f"Input path does not exist or is not a directory: {path}")

    iterator = path.rglob("*.root") if recursive else path.glob("*.root")
    return sorted(str(root_file) for root_file in iterator)


def discover_root_files(input_path: str, recursive: bool) -> list[str]:
    if is_remote_root_uri(input_path):
        return list_remote_root_files(input_path, recursive)
    return list_local_root_files(input_path, recursive)


def resolve_output_root(explicit_output_root: str | None, is_dataset: bool) -> Path:
    if explicit_output_root:
        return Path(explicit_output_root)
    if is_dataset:
        return DEFAULT_REFERENCE_OUTPUT_ROOT
    return DEFAULT_OUTPUT_ROOT


def dataset_file_query(dataset: str, das_instance: str | None) -> str:
    query = f"file dataset={dataset}"
    if das_instance:
        query = f"{query} instance={das_instance}"
    return query


def parse_das_file_output(output: str) -> list[str]:
    files = []
    seen_files: set[str] = set()

    for line in output.splitlines():
        for token in line.split():
            if token.startswith("/store/") and token.endswith(".root"):
                if token not in seen_files:
                    files.append(token)
                    seen_files.add(token)

    return files


def xrootd_uri_for_lfn(lfn: str, xrootd_server: str) -> str:
    if lfn.startswith("root://"):
        return lfn

    server = xrootd_server.strip().removeprefix("root://").rstrip("/")
    return make_remote_root_uri(server, lfn)


def discover_das_dataset_files(
    dataset: str,
    das_instance: str | None,
    xrootd_server: str,
    max_files: int | None,
) -> list[str]:
    if max_files is not None and max_files < 0:
        raise RuntimeError("--max-files must be greater than or equal to 0")

    command = [
        "dasgoclient",
        f"--query={dataset_file_query(dataset, das_instance)}",
    ]
    if max_files:
        command.append(f"--limit={max_files}")

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "dasgoclient is required for --dataset mode, but it is not in PATH"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(f"Failed to query DAS dataset {dataset}{detail}") from exc

    lfns = parse_das_file_output(result.stdout)
    if max_files:
        lfns = lfns[:max_files]

    return [xrootd_uri_for_lfn(lfn, xrootd_server) for lfn in lfns]


def output_relative_path(input_path: str, explicit_label: str | None) -> Path:
    source_path = source_directory_path(input_path)

    label = explicit_label
    if label is None:
        label = source_path.name

    output_leaf = sanitize_path_component(label)
    sample_name = source_path.parent.name
    if not sample_name:
        return Path(output_leaf)

    return Path(sanitize_path_component(sample_name)) / output_leaf


def dataset_output_relative_path(dataset: str, explicit_label: str | None) -> Path:
    parts = [part for part in dataset.strip("/").split("/") if part]
    if not parts:
        return Path(sanitize_path_component(explicit_label or "dataset"))

    primary_dataset = sanitize_path_component(parts[0])
    processed_dataset = parts[1] if len(parts) > 1 else parts[0]
    output_leaf = sanitize_path_component(explicit_label or processed_dataset)
    return Path(primary_dataset) / output_leaf


def source_directory_path(input_path: str) -> Path:
    stripped = input_path.rstrip("/")
    if is_remote_root_uri(stripped):
        _, remote_path = parse_remote_root_uri(stripped)
        source_path = Path(remote_path)
    else:
        source_path = Path(stripped)

    if source_path.suffix == ".root":
        return source_path.parent

    return source_path


def root_file_path(root_file: str) -> Path:
    if is_remote_root_uri(root_file):
        _, remote_path = parse_remote_root_uri(root_file)
        return Path(remote_path)

    return Path(root_file)


def root_file_output_label(root_file: str) -> str:
    file_path = root_file_path(root_file)
    label = file_path.stem or file_path.name
    return sanitize_path_component(label)


def root_file_output_dirs(output_dir: Path, root_files: list[str]) -> list[Path]:
    used_labels: set[str] = set()
    output_dirs = []

    for root_file in root_files:
        label = root_file_output_label(root_file)
        unique_label = label
        suffix = 2
        while unique_label in used_labels:
            unique_label = f"{label}_{suffix}"
            suffix += 1

        used_labels.add(unique_label)
        output_dirs.append(output_dir / unique_label)

    return output_dirs


def sanitize_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized.strip("._") or "nanoaod"


def import_root():
    try:
        import ROOT  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PyROOT is required. Run this script in a ROOT/CMSSW environment."
        ) from exc

    ROOT.gROOT.SetBatch(True)
    return ROOT


def branch_exists(chain, branch_name: str) -> bool:
    if chain.GetTreeNumber() < 0:
        chain.LoadTree(0)
    branches = chain.GetListOfBranches()
    return bool(branches and branches.FindObject(branch_name))


def add_files_to_chain(root_module, tree_name: str, files: list[str]):
    chain = root_module.TChain(tree_name)
    added_files = []
    skipped_files = []

    for root_file in files:
        add_result = chain.Add(root_file)
        if add_result > 0:
            added_files.append(root_file)
        else:
            skipped_files.append(root_file)

    return chain, added_files, skipped_files


def draw_plot(
    root_module,
    chain,
    plot: PlotSpec,
    output_dir: Path,
    formats: list[str],
    selection: str,
) -> bool:
    if not branch_exists(chain, plot.required_branch):
        print(
            f"Skipping {plot.histogram}: missing branch {plot.required_branch}",
            file=sys.stderr,
        )
        return False

    root_module.gDirectory.Delete(f"{plot.histogram};*")
    drawn_values = chain.Draw(plot.draw_expression, selection, "goff")
    if drawn_values < 0:
        print(
            f"Skipping {plot.histogram}: Draw failed for {plot.expression}",
            file=sys.stderr,
        )
        return False

    histogram = root_module.gDirectory.Get(plot.histogram)
    if not histogram:
        print(f"Skipping {plot.histogram}: histogram was not created", file=sys.stderr)
        return False

    histogram.SetDirectory(0)
    histogram.SetLineWidth(2)
    histogram.GetXaxis().SetTitle(plot.x_title)
    histogram.GetYaxis().SetTitle("Entries")

    canvas = root_module.TCanvas(f"c_{plot.histogram}", plot.histogram, 800, 700)
    canvas.SetTicks(1, 1)
    histogram.Draw("hist")

    for output_format in formats:
        output_path = output_dir / f"{plot.histogram}.{output_format.lstrip('.')}"
        canvas.SaveAs(str(output_path))

    return True


def draw_plots(
    root_module,
    tree_name: str,
    root_files: list[str],
    output_dir: Path,
    formats: list[str],
    selection: str,
    plots: list[PlotSpec] | None = None,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    chain, added_files, skipped_files = add_files_to_chain(
        root_module,
        tree_name,
        root_files,
    )
    for skipped_file in skipped_files:
        print(f"WARNING: could not add {skipped_file}", file=sys.stderr)

    if not added_files:
        raise RuntimeError(f"No files could be added to TChain('{tree_name}')")

    print(f"Added {len(added_files)} file(s) to TChain('{tree_name}')")
    print(f"Entries: {chain.GetEntries()}")
    print(f"Writing plots to {output_dir}")

    saved_count = 0
    plot_specs = DEFAULT_PLOTS if plots is None else plots
    for plot in plot_specs:
        if draw_plot(root_module, chain, plot, output_dir, formats, selection):
            saved_count += 1

    return saved_count


def draw_each_root_file(
    root_module,
    tree_name: str,
    root_files: list[str],
    output_dir: Path,
    formats: list[str],
    selection: str,
    plots: list[PlotSpec] | None = None,
) -> int:
    saved_count = 0
    for root_file, file_output_dir in zip(
        root_files,
        root_file_output_dirs(output_dir, root_files),
    ):
        print(f"Plotting {root_file}")
        saved_count += draw_plots(
            root_module,
            tree_name,
            [root_file],
            file_output_dir,
            formats,
            selection,
            plots,
        )

    return saved_count


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dataset and args.input_path:
        parser.error("pass either INPUT_PATH or --dataset, not both")
    if not args.dataset and not args.input_path:
        parser.error("INPUT_PATH or --dataset is required")
    if args.max_files < 0:
        parser.error("--max-files must be greater than or equal to 0")

    try:
        if args.dataset:
            root_files = discover_das_dataset_files(
                dataset=args.dataset,
                das_instance=args.das_instance,
                xrootd_server=args.xrootd_server,
                max_files=args.max_files,
            )
            output_relative = dataset_output_relative_path(args.dataset, args.label)
            input_description = args.dataset
        else:
            root_files = discover_root_files(args.input_path, args.recursive)
            output_relative = output_relative_path(args.input_path, args.label)
            input_description = args.input_path
    except RuntimeError as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    if not root_files:
        parser.exit(1, f"ERROR: No .root files found in {input_description}\n")

    output_dir = resolve_output_root(args.output_root, bool(args.dataset)) / output_relative
    formats = args.formats or ["png"]
    plots = plots_for_mode(args.delphes)

    try:
        root_module = import_root()
    except RuntimeError as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    try:
        saved_count = draw_plots(
            root_module,
            args.tree,
            root_files,
            output_dir,
            formats,
            args.selection,
            plots,
        )
        if args.plot_each_file:
            saved_count += draw_each_root_file(
                root_module,
                args.tree,
                root_files,
                output_dir,
                formats,
                args.selection,
                plots,
            )
    except RuntimeError as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    if saved_count == 0:
        parser.exit(1, "ERROR: No plots were saved\n")

    print(f"Saved {saved_count} plot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
