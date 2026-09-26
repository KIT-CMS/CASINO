# Scripts

Utility scripts for CASINO workflows live here. They are not installed as
console entrypoints; run them from the repository checkout.

## `generate_production_commands.py`

Pass a single YAML config to print its detached tmux launch command using
session `1`:

```bash
poetry run python scripts/generate_production_commands.py \
  configs/run3/2024/production/<sample>.yaml
```

Single-config mode only prints the command; it does not write a file or start
the production.

Generate a detached tmux launch command for every direct-child YAML config in a
configuration directory:

```bash
poetry run python scripts/generate_production_commands.py \
  configs/run3/2024/production
```

The generator only writes
`configs/run3/2024/production/start-production.sh`; it does not start any
production. Review the generated commands, then launch them explicitly:

```bash
bash configs/run3/2024/production/start-production.sh
```

The generated file is ignored by Git. Configs are sorted by filename and use
detached tmux sessions named `1`, `2`, and so on. List or attach to sessions
with `tmux ls` and `tmux attach -t 1`.

## `plot_nanoaod_variables.py`

`plot_nanoaod_variables.py` creates quick validation plots from final NanoAOD
ROOT files. It builds a ROOT `TChain`, draws a fixed set of common NanoAOD
branches, and writes one image per histogram.

The default plots cover:

- `nMuon`, `Muon_pt`, `Muon_phi`
- `nElectron`, `Electron_pt`
- `nTau`, `Tau_pt`, `Tau_decayMode`
- `nJet`, `Jet_pt`

Missing branches are skipped, so the script can still produce the plots that
exist in a given NanoAOD file.

For NanoAOD-shaped files produced from Delphes, pass `--delphes`. This keeps the
shared muon, electron, and jet plots, omits CMS-specific `Tau_*` plots, and adds
the direct Delphes fields `Jet_tauTag` and `Jet_tauWeight`.

### Requirements

Run the script in an environment with PyROOT available, for example a ROOT or
CMSSW environment. Extra tools are needed for non-local inputs:

- `xrdfs` for listing `root://` directories
- `dasgoclient` for `--dataset` mode

These requirements are fulfilled by the `casino-py3.11` Conda environment from
the main README setup. Activate it before running the script:

```bash
conda activate casino-py3.11
```

### Usage

Plot all `.root` files in a local NanoAOD directory:

```bash
python scripts/plot_nanoaod_variables.py /path/to/nanoaod/output
```

Plot a single ROOT file:

```bash
python scripts/plot_nanoaod_variables.py /path/to/file.root
```

Plot a Delphes-derived NanoAOD file:

```bash
python scripts/plot_nanoaod_variables.py --delphes /path/to/delphes-nano.root
```

Plot files from a remote `root://` directory:

```bash
python scripts/plot_nanoaod_variables.py \
  root://cmsdcache-kit-disk.gridka.de//store/user/${USER}/casino/<sample>/nanoAODv15
```

Create reference plots directly from a DAS dataset:

```bash
python scripts/plot_nanoaod_variables.py \
  --dataset /A/B/NANOAODSIM
```

### Outputs

For path inputs, plots are written below:

```text
artifacts/nanoaod-plots/
```

For `--dataset` inputs, plots are written below:

```text
artifacts/reference-plots/
```

Use `--output-root` to choose another output directory and `--label` to override
the leaf output directory name. Generated plot artifacts belong under
`artifacts/` or another local output directory and should not be committed.

### Useful Options

- `--format png --format pdf`: write multiple output formats accepted by
  `TCanvas.SaveAs`
- `--selection "nMuon > 0"`: apply a ROOT `TTree::Draw` selection to every plot
- `--delphes`: use the Delphes-specific plot list with `Jet_tauTag` and
  `Jet_tauWeight` instead of CMS `Tau_*` fields
- `--recursive`: search recursively below a local or `root://` input directory
- `--plot-each-file`: also create per-file plots in subdirectories
- `--tree Events`: choose a different tree name
- `--max-files 20`: limit the number of DAS files used in `--dataset` mode
- `--max-files 0`: remove the script-side DAS file limit

Show the full command-line help with:

```bash
python scripts/plot_nanoaod_variables.py --help
```

## `plot_nanoaod_ratio.py`

`plot_nanoaod_ratio.py` compares a produced NanoAOD output against a reference
DAS NanoAOD dataset. It uses the same default variables and binning as
`plot_nanoaod_variables.py`, normalizes both histograms to unit area, and writes
the bin-by-bin `NanoAOD / reference` ratio.

Example:

```bash
python scripts/plot_nanoaod_ratio.py \
  /path/to/nanoaod/output \
  --reference-dataset /A/B/NANOAODSIM
```

The produced NanoAOD input can be a local directory, a single ROOT file, or a
`root://` directory. The reference side is queried via `dasgoclient` and read
through the configured XRootD redirector.

Ratio plots are written below:

```text
artifacts/nanoaod-reference-ratios/
```

Useful options mirror the single-dataset plotting script where applicable:

- `--format png --format pdf`: write multiple output formats
- `--selection "nMuon > 0"`: apply the same selection to both datasets
- `--recursive`: search recursively below the produced NanoAOD input directory
- `--tree Events`: choose a different tree name
- `--max-files 20`: limit the number of reference DAS files
- `--reference-label official`: shorten the ratio label and output directory
- `--nanoaod-label local`: change the numerator label on the ratio axis
