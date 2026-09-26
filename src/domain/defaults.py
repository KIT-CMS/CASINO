from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

# Display form of the extra `law run` flags for HTCondor; shlex.split() yields the argv.
_DEFAULT_HTCONDOR_EXTRA_ARGS = (
    "--htcondor-requirements '(TARGET.ProvidesCPU)&&(TARGET.ProvidesIO)"
    '&&(TARGET.Machine =!= "mdm1.etp.kit.edu")&&(TARGET.Machine =!= "mdm2.etp.kit.edu")'
    '&&(TARGET.Machine =!= "ms01.etp.kit.edu")&&(TARGET.Machine =!= "ms02.etp.kit.edu")'
    '&&(TARGET.Machine =!= "ms03.etp.kit.edu")&&(TARGET.Machine =!= "ms04.etp.kit.edu")'
    "' --poll-interval 15"
)


@dataclass(frozen=True)
class Defaults:
    total_events: int = 50000
    memory_per_job_gb: int = 6
    cores_per_job: int = 2
    events_per_job_mini: int = 250
    events_per_job_nano: int = 25000
    base_output_path: str = "root://cmsdcache-kit-disk.gridka.de:1094//store/user/${USER}/mc_production"
    delphes_output_subdir: str = "delphes"
    workflow: str = "htcondor"
    das_datatier: str = "NANOAODSIM"
    htcondor_extra_args: str = _DEFAULT_HTCONDOR_EXTRA_ARGS


def load_defaults(repo_root: Path) -> Defaults:
    path = Path(repo_root) / "data" / "defaults.json"
    if not path.exists():
        return Defaults()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {field.name for field in fields(Defaults)}
    return Defaults(**{key: value for key, value in raw.items() if key in known})
