from __future__ import annotations

from pathlib import Path

import awkward as ak
import numpy as np
import uproot

EVENTS_TREE_NAME = "Events"


def _branch_type(array: object):
    return array.dtype if isinstance(array, np.ndarray) else ak.Array(array).type.content


def write_events(path: str | Path, columns: dict[str, object]) -> None:
    # mktree + extend, not `f[name] = {...}`: uproot 5.4+ turns the shorthand into an RNTuple,
    # which NanoAOD tooling does not read. NanoAOD is a TTree named Events.
    branch_types = {name: _branch_type(array) for name, array in columns.items()}
    with uproot.recreate(str(path)) as f:
        f.mktree(EVENTS_TREE_NAME, branch_types)
        f[EVENTS_TREE_NAME].extend(dict(columns))
