#include <fstream>
#include <string>

#include "FWCore/Framework/interface/one/EDAnalyzer.h"
#include "FWCore/Framework/interface/MakerMacros.h"
#include "FWCore/Framework/interface/Event.h"
#include "FWCore/ParameterSet/interface/ParameterSet.h"
#include "FWCore/Utilities/interface/EDGetToken.h"
#include "FWCore/Utilities/interface/InputTag.h"
#include "FWCore/Utilities/interface/propagate_const.h"
#include "SimDataFormats/GeneratorProducts/interface/HepMCProduct.h"

#include "HepMC/IO_GenEvent.h"

// Thread-friendly replacement for the legacy IOMC/Input HepMCEventWriter: the
// `edm::EDAnalyzer` base was REMOVED in CMSSW_13_0_0 (its header is a hard #error in
// CMSSW_14). Writes the generated event as HepMC2 ASCII (HepMC::IO_GenEvent), which
// `DelphesHepMC2` reads. A `one::EDAnalyzer` with a shared resource serializes the single
// output-file write; the file is opened/closed in beginJob/endJob (one file per job).
class HepMCEventWriter : public edm::one::EDAnalyzer<edm::one::SharedResources> {
public:
  explicit HepMCEventWriter(const edm::ParameterSet &params);
  ~HepMCEventWriter() override;

private:
  void beginJob() override;
  void endJob() override;
  void analyze(const edm::Event &event, const edm::EventSetup &es) override;

  edm::propagate_const<HepMC::IO_GenEvent *> output_;
  const edm::EDGetTokenT<edm::HepMCProduct> hepMCToken_;
  const std::string fileName_;
};

HepMCEventWriter::HepMCEventWriter(const edm::ParameterSet &params)
    : hepMCToken_(consumes<edm::HepMCProduct>(params.getParameter<edm::InputTag>("hepMCProduct"))),
      fileName_(params.getParameter<std::string>("fileName")) {
  usesResource("HepMCEventWriter");
}

HepMCEventWriter::~HepMCEventWriter() = default;

void HepMCEventWriter::beginJob() {
  output_ = new HepMC::IO_GenEvent(fileName_.c_str(), std::ios::out);
}

void HepMCEventWriter::endJob() {
  if (output_) {
    delete output_.get();
    output_ = nullptr;
  }
}

void HepMCEventWriter::analyze(const edm::Event &event, const edm::EventSetup &es) {
  // The writer runs from an unconditional EndPath, so it is called even on events the
  // generator EDFilter vetoed (e.g. FxFx jet matching in amcatnlo samples), for which
  // `generatorSmeared` is never produced. getHandle returns an invalid handle in that case
  // (it does not throw); dereferencing it would raise a fatal ProductNotFound. Skipping the
  // event mirrors how cmsDriver gates its PoolOutputModule (SelectEvents = generation_step):
  // only accepted events carry a GEN HepMC, so only those are written.
  const edm::Handle<edm::HepMCProduct> product = event.getHandle(hepMCToken_);
  if (!product.isValid())
    return;
  output_->write_event(product->GetEvent());
}

DEFINE_FWK_MODULE(HepMCEventWriter);
