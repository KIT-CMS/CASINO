import FWCore.ParameterSet.Config as cms


def dumpHepMC(process):
    """Append an EndPath that writes the post-Pythia8 GEN event as HepMC2 ASCII.

    Reads `generatorSmeared` (the edm::HepMCProduct present after --step GEN) and
    writes `gen.hepmc` into the cmsRun working directory (CASINO's per-branch scratch).
    DelphesHepMC2 then reads that file directly.
    """
    process.hepMCWriter = cms.EDAnalyzer(
        "HepMCEventWriter",
        hepMCProduct=cms.InputTag("generatorSmeared"),
        fileName=cms.string("gen.hepmc"),
    )
    process.hepMCWriterEndPath = cms.EndPath(process.hepMCWriter)
    if hasattr(process, "schedule") and process.schedule is not None:
        process.schedule.append(process.hepMCWriterEndPath)
    return process
