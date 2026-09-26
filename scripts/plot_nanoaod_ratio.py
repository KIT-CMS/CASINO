#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from plot_nanoaod_variables import (  # noqa: E402
    DEFAULT_DAS_INSTANCE,
    DEFAULT_MAX_DATASET_FILES,
    DEFAULT_PLOTS,
    DEFAULT_XROOTD_SERVER,
    PlotSpec,
    add_files_to_chain,
    branch_exists,
    dataset_output_relative_path,
    discover_das_dataset_files,
    discover_root_files,
    import_root,
    output_relative_path,
    sanitize_path_component,
)


DEFAULT_OUTPUT_ROOT = Path("artifacts") / "nanoaod-reference-ratios"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plot normalized ratios between a produced NanoAOD output and a reference DAS NanoAOD dataset."
    )
    parser.add_argument(
        "nanoaod_input",
        help="Directory containing produced NanoAOD .root files, a single .root file, or a root:// directory URI.",
    )
    parser.add_argument(
        "--reference-dataset", required=True,
        help="Reference CMS dataset name to query with dasgoclient, e.g. /A/B/NANOAODSIM.",
    )
    parser.add_argument(
        "--das-instance", default=DEFAULT_DAS_INSTANCE,
        help=f"DAS instance appended to the reference file query; empty to omit. Default: {DEFAULT_DAS_INSTANCE}",
    )
    parser.add_argument(
        "--xrootd-server", default=DEFAULT_XROOTD_SERVER,
        help=f"XRootD redirector for DAS logical file names (host or root:// URI). Default: {DEFAULT_XROOTD_SERVER}",
    )
    parser.add_argument(
        "--max-files", type=int, default=DEFAULT_MAX_DATASET_FILES,
        help=f"Maximum number of reference DAS files; 0 for no cap. Default: {DEFAULT_MAX_DATASET_FILES}",
    )
    parser.add_argument(
        "-o", "--output-root", default=None,
        help=f"Directory for the ratio plots. Default: {DEFAULT_OUTPUT_ROOT}.",
    )
    parser.add_argument(
        "--label",
        help="NanoAOD output leaf directory. Defaults to the final NanoAOD path component of NANOAOD_INPUT.",
    )
    parser.add_argument(
        "--reference-label",
        help="Short reference label for the output directory and y-axis. Defaults to the processed dataset "
        "name for the directory and 'reference' for the y-axis.",
    )
    parser.add_argument("--nanoaod-label", default="NanoAOD", help="NanoAOD label in the ratio y-axis. Default: NanoAOD")
    parser.add_argument(
        "--format", action="append", dest="formats", default=None,
        help="Output format for TCanvas.SaveAs, e.g. png or pdf. Can be repeated. Default: png",
    )
    parser.add_argument("--selection", default="", help="ROOT TTree::Draw selection applied to every plot.")
    parser.add_argument(
        "--recursive", action="store_true", help="Search produced NanoAOD .root files recursively below NANOAOD_INPUT.",
    )
    parser.add_argument("--tree", default="Events", help="TTree/TChain name inside the NanoAOD files. Default: Events")
    return parser


def resolve_output_root(explicit_output_root: str | None) -> Path:
    if explicit_output_root:
        return Path(explicit_output_root)
    return DEFAULT_OUTPUT_ROOT


def reference_output_label(
    reference_dataset: str,
    explicit_reference_label: str | None,
) -> str:
    if explicit_reference_label:
        return sanitize_path_component(explicit_reference_label)

    return dataset_output_relative_path(reference_dataset, None).name


def ratio_output_relative_path(
    nanoaod_input: str,
    reference_dataset: str,
    explicit_nanoaod_label: str | None,
    explicit_reference_label: str | None,
) -> Path:
    nanoaod_relative = output_relative_path(nanoaod_input, explicit_nanoaod_label)
    reference_label = reference_output_label(reference_dataset, explicit_reference_label)
    return nanoaod_relative / f"vs_{reference_label}"


def set_if_available(target, method_name: str, *args) -> None:
    method = getattr(target, method_name, None)
    if method:
        method(*args)


def build_normalized_ratio_histogram(
    numerator,
    denominator,
    ratio_name: str,
    x_title: str,
    y_title: str,
):
    numerator_integral = numerator.Integral()
    denominator_integral = denominator.Integral()
    if numerator_integral <= 0 or denominator_integral <= 0:
        return None

    numerator_normalized = numerator.Clone(f"{ratio_name}_numerator_norm")
    denominator_normalized = denominator.Clone(f"{ratio_name}_denominator_norm")
    set_if_available(numerator_normalized, "SetDirectory", 0)
    set_if_available(denominator_normalized, "SetDirectory", 0)
    set_if_available(numerator_normalized, "Sumw2")
    set_if_available(denominator_normalized, "Sumw2")
    numerator_normalized.Scale(1.0 / numerator_integral)
    denominator_normalized.Scale(1.0 / denominator_integral)

    ratio = numerator_normalized.Clone(ratio_name)
    set_if_available(ratio, "SetDirectory", 0)
    set_if_available(ratio, "Sumw2")
    ratio.Divide(denominator_normalized)
    ratio.SetLineWidth(2)
    ratio.GetXaxis().SetTitle(x_title)
    ratio.GetYaxis().SetTitle(y_title)
    set_if_available(ratio, "SetStats", False)
    set_if_available(ratio, "SetMinimum", 0.0)
    if hasattr(ratio, "GetMaximum"):
        maximum = ratio.GetMaximum()
        if maximum > 0:
            set_if_available(ratio, "SetMaximum", max(2.0, maximum * 1.2))

    return ratio


def draw_histogram(
    root_module,
    chain,
    plot: PlotSpec,
    histogram_name: str,
    selection: str,
    source_label: str,
):
    if not branch_exists(chain, plot.required_branch):
        print(
            f"Skipping {plot.histogram}: missing branch {plot.required_branch} "
            f"in {source_label}",
            file=sys.stderr,
        )
        return None

    draw_expression = (
        f"{plot.expression} >> "
        f"{histogram_name}({plot.bins}, {plot.low:g}, {plot.high:g})"
    )
    root_module.gDirectory.Delete(f"{histogram_name};*")
    drawn_values = chain.Draw(draw_expression, selection, "goff")
    if drawn_values < 0:
        print(
            f"Skipping {plot.histogram}: Draw failed for {plot.expression} "
            f"in {source_label}",
            file=sys.stderr,
        )
        return None

    histogram = root_module.gDirectory.Get(histogram_name)
    if not histogram:
        print(
            f"Skipping {plot.histogram}: histogram was not created for {source_label}",
            file=sys.stderr,
        )
        return None

    set_if_available(histogram, "Sumw2")
    histogram.SetDirectory(0)
    return histogram


def save_ratio_plot(
    root_module,
    ratio,
    plot: PlotSpec,
    output_dir: Path,
    formats: list[str],
) -> None:
    canvas = root_module.TCanvas(f"c_{plot.histogram}_ratio", plot.histogram, 800, 700)
    canvas.SetTicks(1, 1)
    set_if_available(ratio, "SetMarkerStyle", 20)
    set_if_available(ratio, "SetMarkerSize", 0.8)
    ratio.Draw("E1X0")

    if hasattr(root_module, "TLine"):
        unit_line = root_module.TLine(plot.low, 1.0, plot.high, 1.0)
        unit_line.SetLineStyle(2)
        unit_line.Draw("same")

    for output_format in formats:
        output_path = output_dir / f"{plot.histogram}_ratio.{output_format.lstrip('.')}"
        canvas.SaveAs(str(output_path))


def draw_ratio_plot(
    root_module,
    nanoaod_chain,
    reference_chain,
    plot: PlotSpec,
    output_dir: Path,
    formats: list[str],
    selection: str,
    y_title: str,
) -> bool:
    nanoaod_histogram = draw_histogram(
        root_module,
        nanoaod_chain,
        plot,
        f"{plot.histogram}_nanoaod",
        selection,
        "NanoAOD",
    )
    if nanoaod_histogram is None:
        return False

    reference_histogram = draw_histogram(
        root_module,
        reference_chain,
        plot,
        f"{plot.histogram}_reference",
        selection,
        "reference",
    )
    if reference_histogram is None:
        return False

    ratio = build_normalized_ratio_histogram(
        nanoaod_histogram,
        reference_histogram,
        ratio_name=f"{plot.histogram}_ratio",
        x_title=plot.x_title,
        y_title=y_title,
    )
    if ratio is None:
        print(
            f"Skipping {plot.histogram}: cannot normalize an empty histogram",
            file=sys.stderr,
        )
        return False

    save_ratio_plot(root_module, ratio, plot, output_dir, formats)
    return True


def chain_from_files(root_module, tree_name: str, root_files: list[str], label: str):
    chain, added_files, skipped_files = add_files_to_chain(
        root_module,
        tree_name,
        root_files,
    )
    for skipped_file in skipped_files:
        print(f"WARNING: could not add {skipped_file}", file=sys.stderr)

    if not added_files:
        raise RuntimeError(f"No {label} files could be added to TChain('{tree_name}')")

    print(f"Added {len(added_files)} {label} file(s) to TChain('{tree_name}')")
    print(f"{label} entries: {chain.GetEntries()}")
    return chain


def draw_ratio_plots(
    root_module,
    tree_name: str,
    nanoaod_files: list[str],
    reference_files: list[str],
    output_dir: Path,
    formats: list[str],
    selection: str,
    y_title: str,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    nanoaod_chain = chain_from_files(root_module, tree_name, nanoaod_files, "NanoAOD")
    reference_chain = chain_from_files(
        root_module,
        tree_name,
        reference_files,
        "reference",
    )

    print(f"Writing ratio plots to {output_dir}")

    saved_count = 0
    for plot in DEFAULT_PLOTS:
        if draw_ratio_plot(
            root_module,
            nanoaod_chain,
            reference_chain,
            plot,
            output_dir,
            formats,
            selection,
            y_title,
        ):
            saved_count += 1

    return saved_count


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.max_files < 0:
        parser.error("--max-files must be greater than or equal to 0")

    try:
        nanoaod_files = discover_root_files(args.nanoaod_input, args.recursive)
        reference_files = discover_das_dataset_files(
            dataset=args.reference_dataset,
            das_instance=args.das_instance,
            xrootd_server=args.xrootd_server,
            max_files=args.max_files,
        )
    except RuntimeError as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    if not nanoaod_files:
        parser.exit(1, f"ERROR: No .root files found in {args.nanoaod_input}\n")
    if not reference_files:
        parser.exit(1, f"ERROR: No .root files found in {args.reference_dataset}\n")

    output_dir = resolve_output_root(args.output_root) / ratio_output_relative_path(
        args.nanoaod_input,
        args.reference_dataset,
        args.label,
        args.reference_label,
    )
    formats = args.formats or ["png"]
    reference_axis_label = args.reference_label or "reference"
    y_title = f"{args.nanoaod_label} / {reference_axis_label} (normalized)"

    try:
        root_module = import_root()
    except RuntimeError as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    try:
        saved_count = draw_ratio_plots(
            root_module,
            args.tree,
            nanoaod_files,
            reference_files,
            output_dir,
            formats,
            args.selection,
            y_title,
        )
    except RuntimeError as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    if saved_count == 0:
        parser.exit(1, "ERROR: No plots were saved\n")

    print(f"Saved {saved_count} ratio plot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
