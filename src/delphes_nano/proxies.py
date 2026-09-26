from __future__ import annotations

import awkward as ak

from .reader import DelphesReader

# Stand-ins for Delphes' binary b-tag flag so working-point cuts still select tagged jets.
BTAG_PROXY_TAGGED = 0.99
BTAG_PROXY_UNTAGGED = 0.01


def build_jet_btag(reader: DelphesReader) -> dict[str, object]:
    flag = reader.array("Jet.BTag")
    return {"Jet_btagDeepFlavB": ak.where(flag > 0, BTAG_PROXY_TAGGED, BTAG_PROXY_UNTAGGED)}
