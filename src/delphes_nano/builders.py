from __future__ import annotations

import awkward as ak
import numpy as np

from .reader import DelphesReader

MUON_MASS = 0.105658
ELECTRON_MASS = 0.000511


def _counts(jagged: ak.Array) -> np.ndarray:
    return np.asarray(ak.num(jagged, axis=1))


def _first_or(reader: DelphesReader, name: str, default: float = 0.0) -> np.ndarray:
    return np.asarray(ak.fill_none(ak.firsts(reader.array(name)), default))


def _lepton(reader: DelphesReader, delphes: str, nano: str, mass: float) -> dict[str, object]:
    pt = reader.array(f"{delphes}.PT")
    return {
        f"n{nano}": _counts(pt),
        f"{nano}_pt": pt,
        f"{nano}_eta": reader.array(f"{delphes}.Eta"),
        f"{nano}_phi": reader.array(f"{delphes}.Phi"),
        f"{nano}_charge": reader.array(f"{delphes}.Charge"),
        f"{nano}_mass": ak.full_like(pt, mass),
    }


def build_muons(reader: DelphesReader) -> dict[str, object]:
    return _lepton(reader, "Muon", "Muon", MUON_MASS)


def build_electrons(reader: DelphesReader) -> dict[str, object]:
    return _lepton(reader, "Electron", "Electron", ELECTRON_MASS)


def build_jets(reader: DelphesReader) -> dict[str, object]:
    pt = reader.array("Jet.PT")
    return {
        "nJet": _counts(pt),
        "Jet_pt": pt,
        "Jet_eta": reader.array("Jet.Eta"),
        "Jet_phi": reader.array("Jet.Phi"),
        "Jet_mass": reader.array("Jet.Mass"),
        "Jet_tauTag": reader.array("Jet.TauTag"),
        "Jet_tauWeight": reader.array("Jet.TauWeight"),
    }


def build_met(reader: DelphesReader) -> dict[str, object]:
    out = {"MET_pt": _first_or(reader, "MissingET.MET"), "MET_phi": _first_or(reader, "MissingET.Phi")}
    if reader.maybe_array("PuppiMissingET.MET") is not None:
        out["PuppiMET_pt"] = _first_or(reader, "PuppiMissingET.MET")
        out["PuppiMET_phi"] = _first_or(reader, "PuppiMissingET.Phi")
    else:
        out["PuppiMET_pt"], out["PuppiMET_phi"] = out["MET_pt"], out["MET_phi"]
    return out
