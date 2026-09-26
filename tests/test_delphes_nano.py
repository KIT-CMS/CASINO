import tempfile
import unittest
from pathlib import Path

import awkward as ak
import uproot

from delphes_nano.builders import build_electrons, build_jets, build_met, build_muons
from delphes_nano.pipeline import convert
from delphes_nano.reader import DelphesReader
from tests.support import REPO_ROOT

BRANCHES = {
    "Muon.PT": [[25.0, 12.0], [40.0]], "Muon.Eta": [[0.5, -1.1], [0.2]], "Muon.Phi": [[0.1, 2.0], [-0.3]],
    "Muon.Charge": [[-1, 1], [1]],
    "Electron.PT": [[35.0], []], "Electron.Eta": [[-0.4], []], "Electron.Phi": [[1.2], []], "Electron.Charge": [[-1], []],
    "Jet.PT": [[120.0], [60.0, 30.0]], "Jet.Eta": [[0.3], [1.0, -2.0]], "Jet.Phi": [[0.0], [1.5, -1.5]],
    "Jet.Mass": [[15.0], [8.0, 4.0]], "Jet.BTag": [[1], [0, 0]], "Jet.TauTag": [[0], [1, 0]], "Jet.TauWeight": [[0.01], [0.87, 0.02]],
    "MissingET.MET": [[30.0], [55.0]], "MissingET.Phi": [[1.2], [-0.7]], "Event.Weight": [[1.0], [1.0]],
}


def write_synthetic_delphes_tree(path: Path) -> None:
    types = {name: "var * int32" if name.endswith(("Charge", "BTag", "TauTag")) else "var * float64" for name in BRANCHES}
    with uproot.recreate(path) as f:  # mktree: Delphes writes TTrees, not RNTuples
        tree = f.mktree("Delphes", types)
        tree.extend({name: ak.Array(values) for name, values in BRANCHES.items()})


class DelphesNanoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.delphes = Path(self.tmp.name) / "delphes.root"
        write_synthetic_delphes_tree(self.delphes)

    def test_builders_map_delphes_objects_to_nanoaod_columns(self):
        reader = DelphesReader(f"file:{self.delphes}")
        self.addCleanup(reader.close)
        self.assertEqual(reader.num_events(), 2)
        muons, electrons, jets, met = build_muons(reader), build_electrons(reader), build_jets(reader), build_met(reader)
        self.assertEqual(list(muons["nMuon"]), [2, 1])
        self.assertEqual(ak.to_list(muons["Muon_mass"][0]), [0.105658, 0.105658])
        self.assertEqual(list(electrons["nElectron"]), [1, 0])
        self.assertEqual(ak.to_list(electrons["Electron_charge"][0]), [-1])
        self.assertEqual(ak.to_list(jets["Jet_tauWeight"][1]), [0.87, 0.02])
        self.assertAlmostEqual(met["MET_pt"][1], 55.0)
        self.assertAlmostEqual(met["PuppiMET_phi"][0], 1.2)

    def test_convert_writes_an_events_ttree_and_merges_inputs(self):
        nano = Path(self.tmp.name) / "nano.root"
        convert(str(self.delphes), str(nano))
        with uproot.open(nano) as f:
            events = f["Events"]
            self.assertEqual((events.classname, events.num_entries), ("TTree", 2))  # NanoAOD tooling does not read RNTuples
            self.assertEqual(ak.to_list(events["Muon_pt"].array()[0]), [25.0, 12.0])
            self.assertEqual(list(events["nJet"].array()), [1, 2])
            btag = events["Jet_btagDeepFlavB"].array()
            self.assertTrue(all(0.0 <= v <= 1.0 for v in ak.flatten(btag)))
            self.assertAlmostEqual(events["genWeight"].array()[0], 1.0)
            self.assertEqual(ak.to_list(ak.num(events["LHEScaleWeight"].array(), axis=1)), [0, 0])

        second = Path(self.tmp.name) / "delphes2.root"
        write_synthetic_delphes_tree(second)
        convert([f"file:{self.delphes}", str(second)], str(nano))
        with uproot.open(nano) as f:
            self.assertEqual(f["Events"].num_entries, 4)
            self.assertEqual(list(f["Events"]["nJet"].array()), [1, 2, 1, 2])

    def test_repo_ships_the_card_and_hepmc_writer(self):
        card = (REPO_ROOT / "cards" / "delphes" / "delphes_card_CMS.tcl").read_text(encoding="utf-8")
        for needle in ["module TreeWriter", "MissingET", "FatJetFinder", "ComputeNsubjettiness 1", "ComputeSoftDrop 1"]:
            self.assertIn(needle, card)
        package = REPO_ROOT / "cmssw" / "Casino" / "HepMCWriter"
        source = (package / "plugins" / "HepMCEventWriter.cc").read_text(encoding="utf-8")
        self.assertIn('#include "HepMC/IO_GenEvent.h"', source)
        self.assertIn("DEFINE_FWK_MODULE(HepMCEventWriter);", source)
        self.assertIn("isValid()", source)  # vetoed events (FxFx) carry no generatorSmeared product
        self.assertIn('<use name="hepmc"/>', (package / "plugins" / "BuildFile.xml").read_text(encoding="utf-8"))
        customise = (package / "python" / "customise.py").read_text(encoding="utf-8")
        self.assertIn("def dumpHepMC(process)", customise)
        self.assertIn('cms.string("gen.hepmc")', customise)
