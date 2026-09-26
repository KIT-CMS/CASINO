# CASINO

The **C**MS-**A**ligned **S**imulation **I**nfrastructure for **N**anoAOD **O**utputs :D

CASINO reproduces official CMS Monte Carlo samples. It downloads the McM setup scripts of a
sample's production chain, turns the embedded `cmsDriver.py` commands into a step plan and
replays them under `cmsRun`, either locally in a singularity container or on HTCondor, driven by
[`law`](https://github.com/riga/law). A Delphes fast-simulation flavor replaces the
GEANT4-based chain with `DelphesHepMC2` plus a NanoAOD-shaped ntuplizer.

There are two entry points:

| Tool | Purpose |
| --- | --- |
| `casino` | CLI: create and browse sample configs, log in to McM, prepare McM artifacts and step plans |
| `law run RunSample` | run the production for one sample config, locally or on HTCondor |

A typical session:

```bash
conda activate casino-py3.11
source ./setup.sh
poetry run casino create-config         # pick a NanoAOD dataset from DAS, writes configs/<run>/<year>/<sample>.yaml
poetry run law run RunSample --config "$PWD/configs/run3/2024/<sample>.yaml" --workflow htcondor
```

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [The `casino` CLI](#the-casino-cli)
- [McM authentication](#mcm-authentication)
- [Sample configs](#sample-configs)
- [Running productions with law](#running-productions-with-law)
- [Generated files](#generated-files)
- [Tests](#tests)

## Requirements

- Linux with CVMFS (`/cvmfs/cms.cern.ch`, `/cvmfs/unpacked.cern.ch`)
- Python 3.11 via Conda or Mamba, and Poetry
- A grid certificate in the CMS VO; a VOMS proxy is needed for DAS queries and grid storage
- `singularity` for local runs, HTCondor submit access for batch runs
- Optional: a CERN account with 2FA for automatic full-chain resolution via McM SSO

## Installation

Install Poetry once:

```bash
curl -sSL https://install.python-poetry.org | python3.11 -
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Create the Conda environment, install the native GFAL bindings used by `law` / `wlcg_fs`, and
install the project:

```bash
conda create -n casino-py3.11 python=3.11
conda activate casino-py3.11
conda install -c conda-forge python-gfal2
poetry install
```

Conda provides Python 3.11 and native packages such as `python-gfal2`. Poetry installs the
dependencies from `pyproject.toml` / `poetry.lock` and the `casino` package itself into the
active Conda environment: the tracked `poetry.toml` sets `virtualenvs.create = false`, so no
second virtualenv is created. If Poetry created one earlier, remove it with
`poetry env remove --all`.

Verify GFAL and build the law task index (once):

```bash
python -c "import gfal2; gfal2.creat_context(); print('ok')"
source ./setup.sh
poetry run law index
```

In every new shell, run `conda activate casino-py3.11` and `source ./setup.sh`. `setup.sh`
exports `BASE_DIR`, `LAW_HOME` and `LAW_CONFIG_FILE`, puts `src/` on `PYTHONPATH` and links
`dasgoclient` from CVMFS into the Conda environment.

## The `casino` CLI

| Command | Purpose |
| --- | --- |
| `casino` | Start menu: create config, show configs, log in to McM |
| `casino create-config` | Find a NanoAOD dataset in DAS, resolve its production chain and write a validated config |
| `casino list-configs [run3/2024]` | Browse existing configs and print their `law run` command |
| `casino mcm-login` | Kerberos + 2FA login that writes the McM SSO cookie |
| `casino download` | Download the McM setup scripts for one config (or `--all`) |
| `casino build-step-runners` | Build the step plan for one config (or `--all`) |

All commands are run as `poetry run casino ...`; `--help` lists the options of each command.

The start menu and `create-config` need a VOMS proxy (`voms-proxy-init -voms cms`) for DAS. At
startup they print an auth-status banner; its McM line is a live probe that reports whether McM
actually accepts the cookie, not only whether the cookie file is unexpired:

```text
Auth status:
  ✓ VOMS proxy: valid, 11h59m left
  ✗ McM SSO cookie: present but rejected by McM — re-auth needed (McM may require 2FA)   →  pass --drpremix, or point MCM_SSO_COOKIEFILE at a browser-exported McM cookie
```

### Creating a config

`casino create-config` searches DAS for a NanoAOD dataset, resolves the production chain in McM
and writes `configs/<run>/<year>/<sample>.yaml` (`<sample>_Delphes.yaml` in Delphes mode). It
then validates the config by downloading the McM scripts and building the step plan, prints the
matching `law run` command and offers to start it.

```bash
poetry run casino create-config                       # interactive wizard
poetry run casino create-config \
    --dataset /TTbar_TuneCP5_13p6TeV.../NANOAODSIM \
    --mode full-chain                                 # non-interactive: GEN→MiniAOD + NanoAOD
poetry run casino create-config \
    --dataset /TTbar.../NANOAODSIM \
    --mode delphes --card cards/delphes/delphes_card_CMS.tcl
```

Passing `--dataset` makes the run non-interactive; values that are not given come from
`data/defaults.json`.

| Option | Meaning |
| --- | --- |
| `--dataset <path>` | NanoAOD dataset; omit to search DAS interactively |
| `--mode full-chain\|delphes` | Full CMSSW chain (MiniAOD + NanoAOD steps) or the Delphes flavor |
| `--card <path>` | Delphes card; interactively chosen from `cards/delphes/` |
| `--no-delphes-nano` | Delphes: write only the `delphes-tree` step |
| `--drpremix <prepid>` | DR-Premix prep-id, see [McM authentication](#mcm-authentication) |
| `--generator <prepid>` | Generator (wmLHEGS or GS) prep-id when it cannot be derived; alias `--wmlhegs` |
| `--chain <prepid>` | Chained request to use when the dataset belongs to several chains |
| `--total-events`, `--cores`, `--memory` | Override the defaults for the whole sample / per job (memory in GB) |
| `--events-per-job-mini`, `--events-per-job-nano` | Events per job of the first and second step |
| `--gridpack <root://...>` | Use a [custom gridpack](#custom-gridpacks) |
| `--output-root <path>` | `base_output_path` of the config |
| `--config-dir run3/2024` | Target directory when it cannot be derived from the campaign name |
| `--force` | Overwrite an existing config |
| `--no-validate` | Skip the download and step-plan validation |
| `--workflow local\|htcondor` | Workflow of the printed `law run` command (default: `htcondor`) |
| `--run`, `--print-only` | Start the production immediately / only print the command |

### Downloading scripts and building step plans

`RunSample` runs these steps automatically. The commands are useful for debugging or for
preparing the artifacts explicitly:

```bash
poetry run casino download configs/run3/2024/<sample>.yaml
poetry run casino download --all                         # every config under configs/, skips existing files
poetry run casino build-step-runners configs/run3/2024/<sample>.yaml
poetry run casino build-step-runners --all
```

| Option | Commands | Meaning |
| --- | --- | --- |
| `--overwrite` | both | Replace existing downloads / step plans |
| `--driver-root <path>` | both | McM artifact directory instead of `artifacts/mcm-commands` |
| `--timeout <seconds>` | `download` | McM HTTP timeout |
| `--verify-certs` | `download` | Enable TLS certificate verification for McM downloads |

## McM authentication

A grid proxy and DAS are enough to derive every chain member except the DR-Premix request. For
full-chain configs its prep-id must come from one of three sources (there is no seeded or
learned cache file):

1. **`casino mcm-login`**: interactive Kerberos + 2FA login that writes a working McM cookie.

   ```bash
   poetry run casino mcm-login          # or `--user <name>`
   ```

   Without a valid Kerberos ticket it runs `kinit` (username defaults to `$USER`, override with
   `--user`). It then asks for the authenticator-app 2FA code, saves the cookie to
   `~/.mcm-sso-cookie.txt` (or `--cookiefile` / `$MCM_SSO_COOKIEFILE`) and verifies that McM
   accepts it. Re-run it when the cookie expires. It is also available in the start menu.

2. **Pass the prep-id directly** (no SSO needed). Read it off the McM chain page that the
   wizard links to:

   ```bash
   poetry run casino create-config --dataset /.../NANOAODSIM --mode full-chain \
       --drpremix HIG-RunIII2024Summer24DRPremix-XXXXX
   ```

   The interactive wizard asks for the prep-id instead.

3. **Export a browser cookie** for `cms-pdmv-prod.web.cern.ch` from a session with a completed
   2FA login, save it in Netscape/Mozilla format and point CASINO at it:

   ```bash
   export MCM_SSO_COOKIEFILE=/path/to/mcm-cookie.txt    # or ~/.mcm-sso-cookie.txt
   ```

With a valid cookie CASINO reads the full chain from McM's SSO-protected `chained_requests`
API.

> **McM enforces 2FA.** The Kerberos-only cookie that a bare `auth-get-sso-cookie` produces is
> rejected: after the Kerberos step Keycloak asks for an OTP, which a non-interactive tool cannot
> supply.

The VOMS proxy (`voms-proxy-init -voms cms`) is independent of McM SSO.

## Sample configs

Configs live under `configs/<run>/<year>/`. The file name (without `.yaml`) is the sample name
and becomes the output directory below `base_output_path`.

```yaml
total_events: 2500000
base_output_path: root://cmsdcache-kit-disk.gridka.de:1094//store/user/${USER}/mc_production

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
```

| Key | Meaning |
| --- | --- |
| `total_events` | Events to produce (required) |
| `base_output_path` | POSIX path or `root://` URI (required); environment variables such as `${USER}` are expanded |
| `gridpack` | Optional [custom gridpack](#custom-gridpacks) |
| `steps` | Ordered processing steps; each step reads the output of the previous one |
| `steps[].name` | Step name (required) |
| `steps[].setup_ids` | McM prep-ids whose `cmsDriver.py` commands run in one job (required) |
| `steps[].events_per_job` | Events per job; sets the number of jobs of the step (required) |
| `steps[].cores_per_job`, `steps[].memory_per_job` | CPU cores and memory in GB per job (required) |
| `steps[].filename` | Output file stem (optional) |

Defaults used by `create-config` (events, cores, memory, output path, workflow, extra HTCondor
arguments) live in `data/defaults.json`.

### Delphes flavor

Delphes stages are written as `setup_ids` tokens. `delphes:<card>` follows a single generator
(wmLHEGS or GS) prep-id; the optional `delphes-nano` step must directly follow the Delphes step:

```yaml
steps:
- name: delphes-tree
  filename: delphes-tree
  setup_ids:
  - HIG-RunIII2024Summer24wmLHEGS-00729
  - delphes:cards/delphes/delphes_card_CMS_hhbbtt_v1.tcl
  events_per_job: 10
  cores_per_job: 4
  memory_per_job: 8
- name: delphes-nano
  filename: delphes-nano
  setup_ids:
  - delphes-nano
  events_per_job: 25000
  cores_per_job: 2
  memory_per_job: 6
```

The Delphes step runs the generator request up to `GEN` in CMSSW, writes HepMC to job scratch and runs `DelphesHepMC2`
with the given card (paths relative to the repository; cards live in `cards/delphes/`). The
Delphes tree is staged out. The `delphes-nano` step turns it into a NanoAOD-shaped ntuple.

### Custom gridpacks

For samples that should use a custom-built gridpack, add `gridpack` next to `base_output_path`:

```yaml
base_output_path: root://cmsdcache-kit-disk.gridka.de:1094//store/user/${USER}/casino
gridpack: root://cmsdcache-kit-disk.gridka.de:1094//store/user/${USER}/gridpacks/ggHH_kl_m1.tgz
steps:
  ...
```

`gridpack` must point to an already uploaded `root://...` tarball; CASINO does not upload local
gridpacks. When it is set, `casino build-step-runners` creates a user-editable fragment next to
the config:

```text
configs/run3/2024/fragments/<sample-name>-fragment.py
```

The fragment replaces the McM CVMFS gridpack with the configured URI and uses CMSSW's xrootd
gridpack runner. An existing fragment is preserved; pass `--overwrite` to regenerate it from the
downloaded McM fragment and the current `gridpack` value.

## Running productions with law

`RunSample` runs the last step of a config and pulls in everything it needs: the McM download,
the step plan and all preceding steps. Each step is a law workflow with one branch per job.

### Local

Jobs run in the `cms:rhel8-m` singularity image from CVMFS:

```bash
source ./setup.sh
poetry run law run RunSample \
  --config "$PWD/configs/run3/2024/<sample>.yaml" \
  --workflow local \
  --output-root /path/to/output
```

### HTCondor

Source `setup.sh` first so that the bundled repository re-creates the same environment on the
worker node, and make sure the submit environment can access grid storage:

```bash
source ./setup.sh
python -c "import gfal2; gfal2.creat_context(); print('ok')"
voms-proxy-info -exists || voms-proxy-init -voms cms
poetry run law run RunSample \
  --config "$PWD/configs/run3/2024/<sample>.yaml" \
  --workflow htcondor
```

The working tree (including uncommitted changes), the step plan and the McM artifacts are bundled
and shipped to the workers; a CMSSW tarball is built once per release and architecture.

- The default accounting group is `cms.production` (`--htcondor-accounting-group`).
- CPU, memory and disk requests are derived from the step config (disk: 3 GB + 2 MB per event);
  `--htcondor-request-cpus`, `--htcondor-request-memory` (MB) and `--htcondor-request-disk` (KB)
  override them when non-zero.
- `casino create-config` and `casino list-configs` print the command including the
  `htcondor_extra_args` from `data/defaults.json` (node requirements, poll interval).

### Outputs

Outputs are written below `base_output_path` from the config unless `--output-root` is given;
`root://` URIs go to grid storage, anything else to the local file system. Each job writes

```text
<output-root>/<sample>/<step>-<hash>/<filename>_<job>.root
```

`<hash>` is derived from `events_per_job`, `total_events` and the file name. Changing any of them
writes to a fresh directory instead of mixing with existing files. The same hash salts the random
seeds, so a re-run reproduces its seeds while different productions get independent random
streams.

### Running law without `setup.sh`

```bash
PYTHONPATH="$PWD/src" LAW_CONFIG_FILE="$PWD/law.cfg" python -m law run RunSample \
  --config "$PWD/configs/run3/2024/<sample>.yaml" \
  --workflow local \
  --output-root /path/to/output
```

## Generated files

Everything below `artifacts/` is ignored by Git and recreated on demand:

- `artifacts/mcm-commands/`: downloaded McM setup scripts and fragments
- `artifacts/step-plans/`: step plan JSON per config
- `artifacts/.law/`, `artifacts/.law_jobs/`: law index and HTCondor job files

Gridpack fragments in `configs/<run>/<year>/fragments/` are generated once and then
user-editable (see [Custom gridpacks](#custom-gridpacks)).

## Tests

```bash
conda activate casino-py3.11
python -m unittest discover -s tests
```
