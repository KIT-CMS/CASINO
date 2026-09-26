from __future__ import annotations

from collections.abc import Iterable

import awkward as ak
import numpy as np

from .builders import build_electrons, build_jets, build_met, build_muons
from .proxies import build_jet_btag
from .reader import DelphesReader
from .writer import write_events


def _gen_weight(reader: DelphesReader) -> np.ndarray:
    # HepMC2 carries a single weight; systematic weight vectors are dropped.
    weight = reader.maybe_array("Event.Weight")
    if weight is None:
        return np.ones(reader.num_events(), dtype=np.float64)
    return np.asarray(ak.fill_none(ak.firsts(weight), 1.0))


def _empty_weight_family(reader: DelphesReader) -> ak.Array:
    """Length-0-per-event sentinel so NanoAOD tooling still finds the branch."""
    n = reader.num_events()
    return ak.Array(ak.contents.ListOffsetArray(
        ak.index.Index64(np.zeros(n + 1, dtype=np.int64)),
        ak.contents.NumpyArray(np.array([], dtype=np.float32)),
    ))


def _build_columns(reader: DelphesReader) -> dict[str, object]:
    columns: dict[str, object] = {}
    for build in (build_muons, build_electrons, build_jets, build_met, build_jet_btag):
        columns.update(build(reader))
    columns["genWeight"] = _gen_weight(reader)
    for name in ("LHEScaleWeight", "LHEPdfWeight", "PSWeight"):
        columns[name] = _empty_weight_family(reader)
    return columns


def _concatenate_columns(per_file: list[dict[str, object]]) -> dict[str, object]:
    if len(per_file) == 1:
        return per_file[0]
    merged: dict[str, object] = {}
    for key in per_file[0]:
        parts = [columns[key] for columns in per_file]
        merged[key] = np.concatenate(parts) if isinstance(parts[0], np.ndarray) else ak.concatenate(parts)
    return merged


def convert(delphes_paths: str | Iterable[str], nano_path: str) -> None:
    """Convert one or more Delphes trees (merged event-wise) into a NanoAOD-shaped Events TTree."""
    paths = [delphes_paths] if isinstance(delphes_paths, str) else list(delphes_paths)
    if not paths:
        raise ValueError("convert requires at least one Delphes input tree")
    per_file = []
    for path in paths:
        reader = DelphesReader(path)
        try:
            per_file.append(_build_columns(reader))
        finally:
            reader.close()
    write_events(nano_path, _concatenate_columns(per_file))
