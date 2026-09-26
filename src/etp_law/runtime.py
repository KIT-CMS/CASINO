"""Turns a step plan into per-branch shell programs (pure functions, no law dependency)."""
from __future__ import annotations

import hashlib
import math
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.cmsdriver import MONITORING_CUSTOMISE, basename_from_file_spec

DEFAULT_LOCAL_CONTAINER_IMAGE = "/cvmfs/unpacked.cern.ch/registry.hub.docker.com/cmssw/cms:rhel8-m"
DEFAULT_CONTAINER_RUNTIME = "singularity"
PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
RANDOM_SEED_MODULE_OFFSETS = {"externalLHEProducer": 1, "generator": 2}
# Pythia8's Random:seed limit; 0 is excluded because it means "seed from the clock".
RANDOM_SEED_MAX_VALUE = 900_000_000
QUIET_MESSAGE_LOGGER = "process.MessageLogger.cerr.FwkReport.reportEvery=1000"

# Collapses the multi-MB "found files: [...]" pile-up list cmsDriver prints for premix campaigns.
DAS_FILE_LIST_FILTER_AWK = r"""
BEGIN { in_list=0; count=0 }
in_list {
  if ($0 ~ /^\/store\//) { count++; next }
  print "found files: " count " files (list suppressed)"
  in_list=0; count=0
}
/^found files:/ {
  tmp = $0
  n = gsub(/\/store\//, "", tmp)
  if (n > 0) {
    print "found files: " n " files (list suppressed)"
    next
  }
  in_list=1; count=0
  next
}
{ print }
END { if (in_list) print "found files: " count " files (list suppressed)" }
"""

# Sanity check for Geant4's ENSDFSTATE data: touch the cvmfs catalogs, then fail readably.
_G4_DATA_CHECK = [
    'if [ -n "${G4ENSDFSTATEDATA:-}" ]; then',
    '  for _g4_dir in "${G4ENSDFSTATEDATA}" "${G4LEVELGAMMADATA:-}" '
    '"${G4NEUTRONHPDATA:-}" "${G4PARTICLEXSDATA:-}" "${G4LEDATA:-}"; do',
    '    [ -n "${_g4_dir}" ] && ls "${_g4_dir}/" >/dev/null 2>&1 || true',
    "  done",
    '  if [ ! -r "${G4ENSDFSTATEDATA}/ENSDFSTATE.dat" ]; then',
    '    echo "FATAL: ${G4ENSDFSTATEDATA}/ENSDFSTATE.dat is not readable" >&2',
    '    ls -la "${G4ENSDFSTATEDATA}/" >&2 2>&1 || true',
    "    exit 1",
    "  fi",
    "fi",
]

Options = list[dict[str, str | None]]


@dataclass(frozen=True, slots=True)
class OutputTargetSpec:
    backend: str
    path: str  # POSIX: absolute path. WLCG: relative to the wlcg_fs base, never a full URI.
    relative_path: str  # <sample>/<slug>-<hash>/<filename>_<branch>.root
    uri: str  # full storage URI for logging, stage-out and cmsRun inputs


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    command_index: int
    scram_arch: str
    cmssw_release: str
    fragment_path: str | None
    fragment_target: str | None
    python_filename: str
    filein: str | None
    fileout: str
    local_output_path: str
    cmsrun_input_files: tuple[str, ...]
    cmsdriver_argv: tuple[str, ...]
    cmsrun_argv: tuple[str, ...]
    kind: str = "cmssw"
    extra_cmssw_packages: tuple[tuple[str, str], ...] = ()  # (source_abs, dest_subpath) copied before scram b
    hepmc_output: str | None = None
    card: str | None = None
    delphes_argv: tuple[str, ...] = ()
    ntuplize_argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BranchExecutionPlan:
    branch: int
    event_count: int
    scratch_dir: str
    output: OutputTargetSpec
    external_inputs: tuple[str, ...]
    commands: tuple[PreparedCommand, ...]


def resolve_output_backend(output_root: str, requested_backend: str) -> str:
    if requested_backend not in {"auto", "posix", "wlcg"}:
        raise ValueError(f"Unsupported output backend {requested_backend!r}")
    if requested_backend != "auto":
        return requested_backend
    return "wlcg" if "://" in output_root else "posix"


def resolve_output_root(plan: dict[str, Any], output_root_override: str | None) -> str:
    return output_root_override or str(plan["base_output_path"])


def branch_event_count(total_events: int, events_per_job: int, branch: int) -> int:
    if branch < 0:
        raise ValueError("branch must be non-negative")
    remaining = total_events - branch * events_per_job
    if remaining <= 0:
        raise ValueError(f"Branch {branch} does not exist for total_events={total_events}")
    return min(events_per_job, remaining)


def find_step(plan: dict[str, Any], step_name_or_slug: str) -> dict[str, Any]:
    for step in plan["steps"]:
        if step_name_or_slug in (step["name"], step["slug"]):
            return step
    raise KeyError(f"Unknown step {step_name_or_slug!r}")


def predecessor_step(plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
    slug = step.get("predecessor_step")
    return find_step(plan, slug) if slug else None


def predecessor_branches_for_branch(plan: dict[str, Any], step: dict[str, Any], branch: int) -> list[int]:
    predecessor = predecessor_step(plan, step)
    if predecessor is None:
        return []
    fan_in = step["events_per_job"] // predecessor["events_per_job"]
    start = branch * fan_in
    stop = min(start + fan_in, predecessor["job_count"])
    if start >= stop:
        raise ValueError(f"Step {step['name']} branch {branch} does not map to predecessor branches")
    return list(range(start, stop))


def _filename_stem(step: dict[str, Any]) -> str:
    return step.get("filename") or step["slug"]


def step_output_hash(events_per_job: int, total_events: int, filename_stem: str) -> str:
    """Fingerprint of the config inputs that shape a step's output layout.

    Folded into the output directory so a re-run with different sharding never reuses (and
    silently skips) branch files of a previous run. Raw inputs, not job_count, so that two
    total_events values that ceil to the same job_count still get distinct directories.
    """
    payload = f"{events_per_job}:{total_events}:{filename_stem}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:8]


def step_output_dir(step: dict[str, Any], total_events: int) -> str:
    return f"{step['slug']}-{step_output_hash(step['events_per_job'], total_events, _filename_stem(step))}"


def step_seed_salt(step: dict[str, Any], total_events: int) -> int:
    """The output fingerprint as an int: distinct stored output <=> distinct random stream."""
    return int(step_output_hash(step["events_per_job"], total_events, _filename_stem(step)), 16)


def branch_output_relative_path(plan: dict[str, Any], step: dict[str, Any], branch: int) -> Path:
    step_dir = step_output_dir(step, plan["total_events"])
    return Path(plan["sample_name"]) / step_dir / f"{_filename_stem(step)}_{branch + 1}.root"


def build_output_target(
    plan: dict[str, Any], step: dict[str, Any], branch: int, output_root: str, output_backend: str,
) -> OutputTargetSpec:
    relative = branch_output_relative_path(plan, step, branch).as_posix()
    if output_backend == "posix":
        path = uri = str(Path(output_root) / relative)
    else:
        path, uri = relative, output_root.rstrip("/") + "/" + relative
    return OutputTargetSpec(backend=output_backend, path=path, relative_path=relative, uri=uri)


def predecessor_output_file_specs(plan, step, branch: int, output_root: str, output_backend: str) -> list[str]:
    """cmsRun input specs of the predecessor branches feeding this branch (file: for POSIX, URI for WLCG)."""
    predecessor = predecessor_step(plan, step)
    if predecessor is None:
        return []
    specs = []
    for upstream_branch in predecessor_branches_for_branch(plan, step, branch):
        target = build_output_target(plan, predecessor, upstream_branch, output_root, output_backend)
        specs.append(f"file:{target.uri}" if target.backend == "posix" else target.uri)
    return specs


def _option_value(options: Options, name: str) -> str | None:
    return next((o["value"] for o in options if o["name"] == name), None)


def _set_option(options: Options, name: str, value: str | None) -> None:
    for option in options:
        if option["name"] == name:
            option["value"] = value
            return
    options.append({"name": name, "value": value})


def _add_customise_command(options: Options, command: str, replaces: re.Pattern[str] | None = None) -> None:
    """Append a statement to --customise_commands, replacing an existing match of `replaces` if any."""
    current = _option_value(options, "--customise_commands") or ""
    if replaces is not None and replaces.search(current):
        _set_option(options, "--customise_commands", replaces.sub(command, current, count=1))
    else:
        _set_option(options, "--customise_commands", f"{current};{command}" if current else command)


def _cmsdriver_step_names(options: Options) -> set[str]:
    value = _option_value(options, "--step") or ""
    return {token.split(":", 1)[0] for token in value.split(",") if token}


def _module_seed(branch: int, module_label: str, salt: int) -> int:
    """1 + ((salt + base) % MAX) with base = 2*branch + offset: collision-free within a production."""
    if module_label not in RANDOM_SEED_MODULE_OFFSETS:
        raise ValueError(f"Unsupported random seed module {module_label!r}")
    if branch < 0:
        raise ValueError("branch must be non-negative")
    base = branch * len(RANDOM_SEED_MODULE_OFFSETS) + RANDOM_SEED_MODULE_OFFSETS[module_label]
    if base >= RANDOM_SEED_MAX_VALUE:
        raise ValueError(
            f"Seed base {base} for branch {branch} reaches RANDOM_SEED_MAX_VALUE; "
            "too many branches to keep per-production seeds collision-free"
        )
    return 1 + ((salt + base) % RANDOM_SEED_MAX_VALUE)


def _external_lhe_seed(branch: int, salt: int) -> int:
    return _module_seed(branch, "externalLHEProducer", salt)


def _ensure_generation_random_seeds(options: Options, branch: int, salt: int) -> None:
    for step_name, module in (("LHE", "externalLHEProducer"), ("GEN", "generator")):
        if step_name in _cmsdriver_step_names(options):
            pattern = re.compile(
                rf"process\.RandomNumberGeneratorService\.{module}\.initialSeed\s*=\s*"
                r"(?:int\([^;]*\)|cms\.untracked\.uint32\([^;]*\)|[^;]+)"
            )
            seed = _module_seed(branch, module, salt)
            command = f"process.RandomNumberGeneratorService.{module}.initialSeed=int({seed})"
            _add_customise_command(options, command, pattern)


def _resolve_runtime_placeholders(value: str, branch: int, salt: int) -> str:
    runtime_values = {"SEED": str(_external_lhe_seed(branch, salt))}

    def replace(match: re.Match[str]) -> str:
        placeholder = match.group(1)
        if placeholder not in runtime_values:
            raise ValueError(f"Unsupported runtime placeholder {placeholder!r} in command option {value!r}")
        return runtime_values[placeholder]

    return PLACEHOLDER_PATTERN.sub(replace, value)


def _resolve_command_options(options: Options, branch: int, salt: int) -> Options:
    resolved = []
    for option in options:
        if option.get("name") == "--customise" and option.get("value") == MONITORING_CUSTOMISE:
            continue
        option = dict(option)
        if isinstance(option.get("value"), str) and "${" in option["value"]:
            option["value"] = _resolve_runtime_placeholders(option["value"], branch, salt)
        resolved.append(option)
    return resolved


def _options_to_argv(fragment_target: str | None, options: Options) -> list[str]:
    argv = ["cmsDriver.py"] + ([fragment_target] if fragment_target else [])
    for option in options:
        if option["name"] is not None:
            argv.append(option["name"])
        if option["value"] is not None:
            argv.append(option["value"])
    return argv


def _prepare_delphes_command(command: dict[str, Any], scratch_dir: Path, rewritten_outputs: dict[str, str]) -> PreparedCommand:
    hepmc_name = basename_from_file_spec(str(command["filein"]))
    hepmc_path = rewritten_outputs.get(hepmc_name, str(scratch_dir / hepmc_name))
    delphes_out = str(scratch_dir / basename_from_file_spec(str(command["fileout"])))
    return PreparedCommand(
        command_index=int(command["command_index"]), scram_arch="", cmssw_release="", fragment_path=None,
        fragment_target=None, python_filename="", filein=hepmc_path, fileout=str(command["fileout"]),
        local_output_path=delphes_out, cmsrun_input_files=(), cmsdriver_argv=(), cmsrun_argv=(),
        kind="delphes", card=str(command["card"]),
        delphes_argv=(str(command["delphes_executable"]), str(command["card"]), delphes_out, hepmc_path),
    )


def _prepare_nano_command(command: dict[str, Any], scratch_dir: Path, external_inputs: list[str]) -> PreparedCommand:
    """The ntuplizer merges every predecessor Delphes tree of this branch; inputs precede the output."""
    if not external_inputs:
        raise ValueError("nano command has no predecessor delphes output to ntuplize")
    nano_out = str(scratch_dir / basename_from_file_spec(str(command["fileout"])))
    return PreparedCommand(
        command_index=int(command["command_index"]), scram_arch="", cmssw_release="", fragment_path=None,
        fragment_target=None, python_filename="", filein=external_inputs[0], fileout=str(command["fileout"]),
        local_output_path=nano_out, cmsrun_input_files=tuple(external_inputs), cmsdriver_argv=(), cmsrun_argv=(),
        kind="nano", ntuplize_argv=("python3", "-m", "delphes_nano", *external_inputs, nano_out),
    )


def _prepare_cmssw_command(
    command: dict[str, Any], step: dict[str, Any], branch: int, event_count: int, salt: int, is_source_step: bool,
    scratch_dir: Path, rewritten_outputs: dict[str, str], external_inputs: list[str],
) -> PreparedCommand:
    local_output_path = scratch_dir / basename_from_file_spec(str(command["fileout"]))
    filein = command.get("filein")
    cmsrun_input_files: tuple[str, ...] = ()
    if filein:
        internal = rewritten_outputs.get(basename_from_file_spec(filein))
        if internal:
            filein = internal
        elif external_inputs:
            filein = external_inputs[0]
            if len(external_inputs) > 1:
                cmsrun_input_files = tuple(external_inputs)

    options = _resolve_command_options(list(command["options"]), branch, salt)
    if is_source_step and command.get("filein") is None:
        _ensure_generation_random_seeds(options, branch, salt)
        # Keeps event identities unique across independently generated branches after a merge.
        _add_customise_command(options, f"process.source.firstLuminosityBlock=cms.untracked.uint32({branch + 1})")
    _add_customise_command(options, QUIET_MESSAGE_LOGGER)
    _set_option(options, "--fileout", f"file:{local_output_path}")
    if filein:
        _set_option(options, "--filein", filein)
    else:
        options = [option for option in options if option["name"] != "--filein"]
    _set_option(options, "--number", str(event_count))
    _set_option(options, "--number_out", str(event_count))

    return PreparedCommand(
        command_index=int(command["command_index"]),
        scram_arch=str(command["scram_arch"]),
        cmssw_release=str(command["cmssw_release"]),
        fragment_path=command.get("fragment_path"),
        fragment_target=command.get("fragment_target"),
        python_filename=str(command["python_filename"]),
        filein=filein,
        fileout=str(command["fileout"]),
        local_output_path=str(local_output_path),
        cmsrun_input_files=cmsrun_input_files,
        cmsdriver_argv=tuple(_options_to_argv(command.get("fragment_target"), options)),
        cmsrun_argv=("cmsRun", "-n", str(step["cores_per_job"]), str(command["python_filename"])),
        kind=command.get("kind", "cmssw"),
        extra_cmssw_packages=tuple((pkg["source"], pkg["dest"]) for pkg in command.get("extra_cmssw_packages", [])),
        hepmc_output=command.get("hepmc_output"),
    )


def build_branch_execution_plan(
    plan: dict[str, Any],
    step_name_or_slug: str,
    branch: int,
    scratch_dir: Path,
    output_root_override: str | None = None,
    output_backend_request: str = "auto",
) -> BranchExecutionPlan:
    step = find_step(plan, step_name_or_slug)
    total_events = int(plan["total_events"])
    output_root = resolve_output_root(plan, output_root_override)
    output_backend = resolve_output_backend(output_root, output_backend_request)
    external_inputs = predecessor_output_file_specs(plan, step, branch, output_root, output_backend)
    event_count = branch_event_count(total_events, step["events_per_job"], branch)
    is_source_step = predecessor_step(plan, step) is None
    salt = step_seed_salt(step, total_events)

    scratch_dir = scratch_dir.resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)

    rewritten_outputs: dict[str, str] = {}  # basename of a command's fileout -> scratch path for later commands
    prepared: list[PreparedCommand] = []
    for command in step["commands"]:
        kind = command.get("kind", "cmssw")
        if kind == "delphes":
            prepared.append(_prepare_delphes_command(command, scratch_dir, rewritten_outputs))
            continue
        if kind == "nano":
            prepared.append(_prepare_nano_command(command, scratch_dir, external_inputs))
            continue
        if command.get("hepmc_output"):
            rewritten_outputs[command["hepmc_output"]] = str(scratch_dir / command["hepmc_output"])
        cmssw = _prepare_cmssw_command(
            command, step, branch, event_count, salt, is_source_step, scratch_dir, rewritten_outputs, external_inputs,
        )
        prepared.append(cmssw)
        rewritten_outputs[basename_from_file_spec(str(command["fileout"]))] = f"file:{cmssw.local_output_path}"

    return BranchExecutionPlan(
        branch=branch,
        event_count=event_count,
        scratch_dir=str(scratch_dir),
        output=build_output_target(plan, step, branch, output_root, output_backend),
        external_inputs=tuple(external_inputs),
        commands=tuple(prepared),
    )


def _run_with_log_tail(argv: tuple[str, ...], log_file: Path, label: str) -> list[str]:
    return [
        f"log_file={shlex.quote(str(log_file))}",
        f'if {shlex.join(argv)} > "${{log_file}}" 2>&1; then',
        f'  echo "{label} completed"',
        "else",
        "  rc=$?",
        f'  echo "{label} failed (rc=${{rc}}), last 500 log lines:" >&2',
        '  tail -n 500 "${log_file}" >&2 || true',
        "  exit ${rc}",
        "fi",
    ]


def build_cmssw_command_script(
    command: PreparedCommand, scratch_dir: Path, cmssw_cache_dir: Path, compile_cores: int,
) -> str:
    """Bash program: set up the CMSSW release, run cmsDriver + cmsRun, log to scratch, tail on failure."""
    release_src = cmssw_cache_dir / command.cmssw_release / "src"
    log_file = scratch_dir / f"cmsrun-{command.command_index}.log"
    timing_file = scratch_dir / f"cmsrun-{command.command_index}.time"
    lines = [
        "set -euo pipefail",
        # scram clears PYTHONPATH but not PYTHONHOME, so the LCG stack's value would break CMSSW's Python.
        "unset PYTHONHOME",
        "unset PYTHONPATH",
        # LCG's Geant4 libs and G4*DATA vars must not leak into CMSSW's scram runtime (scram appends
        # to type="path" vars instead of overwriting, producing unreadable colon-joined data paths).
        "unset LD_LIBRARY_PATH",
        'for _v in $(compgen -e 2>/dev/null | grep -E "^(G4|GEANT4)" || true); do unset "${_v}"; done',
        "unset _v",
        "export LANG=${LANG:-C.UTF-8}",
        "export LC_ALL=${LC_ALL:-C.UTF-8}",
        f"export SCRAM_ARCH={shlex.quote(command.scram_arch)}",
        "source /cvmfs/cms.cern.ch/cmsset_default.sh",
        f"mkdir -p {shlex.quote(str(cmssw_cache_dir))}",
        f"cd {shlex.quote(str(cmssw_cache_dir))}",
        f"if [ ! -d {shlex.quote(command.cmssw_release + '/src')} ]; then",
        f"  scram p CMSSW {shlex.quote(command.cmssw_release)}",
        "fi",
        f"cd {shlex.quote(str(release_src))}",
        "scram b ProjectRename",
        'eval "$(scram runtime -sh)"',
    ]
    if command.fragment_path and command.fragment_target:
        lines += [
            f"mkdir -p {shlex.quote(str(Path(command.fragment_target).parent))}",
            f"cp {shlex.quote(command.fragment_path)} {shlex.quote(command.fragment_target)}",
        ]
    for source, dest in command.extra_cmssw_packages:
        lines += [
            f"mkdir -p {shlex.quote(str(Path(dest).parent))}",
            f"rm -rf {shlex.quote(dest)}",
            f"cp -r {shlex.quote(source)} {shlex.quote(dest)}",
        ]
    if (command.fragment_path and command.fragment_target) or command.extra_cmssw_packages:
        lines.append(f"scram b -j {compile_cores}")
    lines += [
        f"cd {shlex.quote(str(scratch_dir))}",
        f"{shlex.join(command.cmsdriver_argv)} | awk {shlex.quote(DAS_FILE_LIST_FILTER_AWK)}",
        *_G4_DATA_CHECK,
    ]
    if command.cmsrun_input_files:
        lines += [
            f"cat <<'EOF' >> {shlex.quote(command.python_filename)}",
            "# Override the auto-generated source so merged predecessor branches are all consumed.",
            "process.source.fileNames = cms.untracked.vstring(",
            *(f"    {file_spec!r}," for file_spec in command.cmsrun_input_files),
            ")",
            "EOF",
        ]
    # `if cmd; then ... else rc=$?` (not `if ! cmd`): the `!` would turn a real failure into rc=0.
    lines += [
        f"log_file={shlex.quote(str(log_file))}",
        f"timing_file={shlex.quote(str(timing_file))}",
        f"TIMEFORMAT='cmsRun command {command.command_index} timing: wall=%3R s user=%3U s sys=%3S s cpu=%P'",
        f'if {{ time {{ {shlex.join(command.cmsrun_argv)} > "${{log_file}}" 2>&1; }}; }} 2> "${{timing_file}}"; then',
        '  cat "${timing_file}"',
        f'  echo "cmsRun command {command.command_index} completed"',
        "else",
        "  rc=$?",
        '  echo "cmsRun failed (rc=${rc}), timing:" >&2',
        '  cat "${timing_file}" >&2 || true',
        '  echo "cmsRun failed (rc=${rc}), last 500 log lines:" >&2',
        '  tail -n 500 "${log_file}" >&2 || true',
        "  exit ${rc}",
        "fi",
    ]
    return "\n".join(lines)


def _squote(value: str) -> str:
    """Always single-quote (shlex.quote leaves /cvmfs/... paths bare, which is fine but inconsistent)."""
    return "'" + value.replace("'", "'\\''") + "'"


def build_lcg_command_script(command: PreparedCommand, scratch_dir: Path, lcg_setup: str, repo_src: Path) -> str:
    """Bash program for a delphes/nano command: source the LCG view (outside scram), run, tail on failure."""
    log_file = scratch_dir / f"lcg-{command.kind}-{command.command_index}.log"
    lines = [
        "set -eo pipefail",  # the LCG setup.sh is not nounset-safe; -u is enabled after sourcing it
        "unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH GFAL_PLUGIN_DIR GFAL_CONFIG_DIR",
        f"source {_squote(lcg_setup)}",
        "set -u",
        f"export PYTHONPATH={_squote(str(repo_src))}:${{PYTHONPATH:-}}",
        f"cd {_squote(str(scratch_dir))}",
    ]
    if command.kind == "delphes":
        if command.card:
            # A `set PileUpFile <name>` in the card resolves against the working dir: link the sibling file in.
            card = _squote(command.card)
            lines += [
                f"__pu_dir=$(dirname {card})",
                f"__pu=$(awk '$1==\"set\" && $2==\"PileUpFile\" {{print $3; exit}}' {card} 2>/dev/null || true)",
                'if [ -n "${__pu}" ] && [ ! -e "${__pu}" ] && [ -e "${__pu_dir}/${__pu}" ]; then',
                '  ln -sf "${__pu_dir}/${__pu}" "${__pu}"',
                "fi",
            ]
        lines.append(f"rm -f {_squote(command.local_output_path)}")  # Delphes refuses to overwrite
        argv, label = command.delphes_argv, f"Delphes command {command.command_index}"
    elif command.kind == "nano":
        argv, label = command.ntuplize_argv, f"ntuplize command {command.command_index}"
    else:
        raise ValueError(f"build_lcg_command_script does not handle kind={command.kind!r}")
    return "\n".join(lines + _run_with_log_tail(argv, log_file, label))


def required_bind_paths(
    repo_root: Path, scratch_dir: Path, cmssw_cache_dir: Path, output_root: str, output_backend: str,
    input_specs: list[str], fragment_paths: list[str],
) -> list[Path]:
    scratch_dir, cmssw_cache_dir = scratch_dir.resolve(), cmssw_cache_dir.resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    cmssw_cache_dir.mkdir(parents=True, exist_ok=True)
    mounts = {repo_root.resolve(), scratch_dir, cmssw_cache_dir, Path("/cvmfs")}
    if output_backend == "posix":
        output_root_path = Path(output_root).resolve()
        output_root_path.mkdir(parents=True, exist_ok=True)
        mounts.add(output_root_path)
    for file_spec in input_specs:
        if "://" not in file_spec:
            path = Path(file_spec.removeprefix("file:")).resolve()
            mounts.add(path.parent if path.suffix else path)
    mounts.update(Path(fragment_path).resolve().parent for fragment_path in fragment_paths)
    return sorted(mounts)


def build_local_container_command(runtime: str, image: str, bind_paths: list[Path], shell_script: str) -> list[str]:
    command = [runtime, "exec"]
    for bind_path in bind_paths:
        command += ["-B", str(bind_path)]
    return command + [image, "/bin/bash", "-lc", shell_script]
