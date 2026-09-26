from __future__ import annotations

from dataclasses import dataclass

# Longest first so endswith never confuses MiniAOD/MiniAODv6 or NanoAOD/NanoAODv15.
_TIERS = ["NanoAODv15", "NanoAOD", "MiniAODv6", "MiniAOD", "DRPremix", "wmLHEGS", "GS"]
_TIER_FAMILY = {tier: tier.rstrip("v0123456789") if "AOD" in tier else tier for tier in _TIERS}


def _middle_token(prep_id: str) -> str:
    """HIG-RunIII2024Summer24NanoAODv15-00308 -> RunIII2024Summer24NanoAODv15"""
    without_rev = prep_id.rsplit("-", 1)[0]
    return without_rev.partition("-")[2] or without_rev


def _tier_of(prep_id: str) -> str | None:
    token = _middle_token(prep_id)
    return next((tier for tier in _TIERS if token.endswith(tier)), None)


def classify_prepid(prep_id: str) -> str | None:
    tier = _tier_of(prep_id)
    return _TIER_FAMILY.get(tier) if tier else None


def campaign_of(prep_id: str) -> str:
    token = _middle_token(prep_id)
    tier = _tier_of(prep_id)
    return token[: -len(tier)] if tier else token


def drpremix_variant_of(chain_prepid: str) -> str:
    if "DRPremixNoOutput" in chain_prepid:
        return "DRPremixNoOutput"
    if "DRPremix" in chain_prepid:
        return "DRPremix"
    raise ValueError(f"No DR-Premix flow in chain prep-id: {chain_prepid!r}")


def sample_name_of(dataset: str) -> str:
    parts = [p for p in dataset.split("/") if p]
    if not parts:
        raise ValueError(f"Not a dataset path: {dataset!r}")
    return parts[0]


@dataclass(frozen=True)
class ResolvedStep:
    kind: str
    prep_id: str


@dataclass(frozen=True)
class ResolvedChain:
    dataset: str
    sample_name: str
    campaign: str
    chain_prepid: str | None
    total_events: int
    steps: tuple[ResolvedStep, ...]

    def step(self, kind: str) -> ResolvedStep | None:
        return next((step for step in self.steps if step.kind == kind), None)
