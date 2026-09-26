from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from domain.chain import (
    ResolvedChain,
    ResolvedStep,
    campaign_of,
    classify_prepid,
    drpremix_variant_of,
    sample_name_of,
)
from infrastructure.das import GENERATOR_KINDS
from infrastructure.mcm import member_of_chain
from infrastructure.mcm_auth import McMAuthUnavailable

FULL_CHAIN_TAIL = ("DRPremix", "MiniAOD", "NanoAOD")
REQUEST_PREPID_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"([A-Za-z0-9]+-Run[A-Za-z0-9]+(?:wmLHEGS|GS|DRPremix|MiniAOD[A-Za-z0-9]*|NanoAOD[A-Za-z0-9]*)-\d+)"
    r"(?![A-Za-z0-9])"
)


class ChainResolutionError(RuntimeError):
    pass


class NoChainFound(ChainResolutionError):
    pass


class AmbiguousChain(ChainResolutionError):
    def __init__(self, candidates):
        super().__init__(f"Dataset belongs to multiple chains: {candidates}. Use --chain.")
        self.candidates = candidates


class WmlhegsUnresolved(ChainResolutionError):
    def __init__(self, dataset, chain_prepid):
        super().__init__(
            f"Could not resolve the generator prep-id (wmLHEGS or GS) for {dataset}. "
            f"Use --generator <prepid> or enable McM SSO. Chain: {chain_prepid}"
        )
        self.dataset = dataset
        self.chain_prepid = chain_prepid


class DrpremixUnresolved(ChainResolutionError):
    def __init__(self, campaign, variant, chain_prepid):
        super().__init__(
            f"Could not resolve the DR-Premix prep-id for ({campaign}, {variant}). "
            f"Use --drpremix <prepid> or enable McM SSO. "
            f"See https://cms-pdmv-prod.web.cern.ch/mcm/chained_requests?prepid={chain_prepid}"
        )
        self.campaign = campaign
        self.variant = variant
        self.chain_prepid = chain_prepid


def _string_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _string_values(nested)


def _request_prepids(value: Any, *, embedded: bool) -> list[str]:
    found: list[str] = []
    for text in _string_values(value):
        if embedded:
            found.extend(match.group(1) for match in REQUEST_PREPID_RE.finditer(text))
        elif REQUEST_PREPID_RE.fullmatch(text):
            found.append(text)
    return list(dict.fromkeys(found))


def select_chain(chains: list[str], nano_prepid: str) -> str:
    token = "NanoAODv15" if "NanoAODv15" in nano_prepid else "NanoAOD"
    matches = [c for c in chains if token in c]
    if not matches:
        raise NoChainFound(f"No chain found for {nano_prepid}")
    if len(matches) > 1:
        raise AmbiguousChain(matches)
    return matches[0]


def _nano_and_chain(das, fetcher, dataset: str, chain: str | None) -> tuple[str, str]:
    nano_prepid = das.mcm_prepid(dataset)
    if not nano_prepid:
        raise NoChainFound(f"DAS has no McM prep-id for {dataset}")
    return nano_prepid, chain or select_chain(member_of_chain(nano_prepid, fetcher=fetcher), nano_prepid)


def _resolved(das, dataset: str, nano_prepid: str, chain_prepid: str, steps) -> ResolvedChain:
    return ResolvedChain(
        dataset=dataset,
        sample_name=sample_name_of(dataset),
        campaign=campaign_of(nano_prepid),
        chain_prepid=chain_prepid,
        total_events=int(das.summary(dataset).get("nevents", 0)),
        steps=tuple(steps),
    )


class ProxyChainResolver:
    """Resolves the chain with only a grid proxy: DAS plus McM's public API."""

    def __init__(self, das, fetcher) -> None:
        self.das = das
        self.fetcher = fetcher

    def resolve(self, dataset: str, mode: str, *, wmlhegs=None, drpremix=None, chain=None) -> ResolvedChain:
        nano_prepid, chain_prepid = _nano_and_chain(self.das, self.fetcher, dataset, chain)
        generator_prepid = wmlhegs or self.das.find_generator(dataset)
        generator_kind = classify_prepid(generator_prepid) if generator_prepid else None
        if generator_kind not in GENERATOR_KINDS:
            raise WmlhegsUnresolved(dataset, chain_prepid)
        steps = [ResolvedStep(generator_kind, generator_prepid)]
        if mode == "full-chain":
            if not drpremix:
                raise DrpremixUnresolved(campaign_of(nano_prepid), drpremix_variant_of(chain_prepid), chain_prepid)
            mini_prepid = self.das.mcm_prepid(self.das.parent(dataset))
            steps += [
                ResolvedStep("DRPremix", drpremix),
                ResolvedStep("MiniAOD", mini_prepid),
                ResolvedStep("NanoAOD", nano_prepid),
            ]
        return _resolved(self.das, dataset, nano_prepid, chain_prepid, steps)


class AuthenticatedChainResolver:
    """Resolves every member from McM's SSO-protected chained_requests document."""

    def __init__(self, auth_session, das, fetcher) -> None:
        self.auth_session = auth_session
        self.das = das
        self.fetcher = fetcher

    def resolve(self, dataset: str, mode: str, *, chain=None) -> ResolvedChain:
        nano_prepid, chain_prepid = _nano_and_chain(self.das, self.fetcher, dataset, chain)
        members = self._ordered_members(self.auth_session.get_chained_request(chain_prepid))
        by_kind = {classify_prepid(pid): pid for pid in members if classify_prepid(pid)}
        generator_kind = next((kind for kind in ("wmLHEGS", "GS") if kind in by_kind), None)
        if generator_kind is None:
            raise McMAuthUnavailable("chained_requests document lacked a generator (wmLHEGS or GS) member")
        order = [generator_kind] if mode == "delphes" else [generator_kind, *FULL_CHAIN_TAIL]
        if any(kind not in by_kind for kind in order):
            raise McMAuthUnavailable("chained_requests doc missing a full-chain member")
        steps = [ResolvedStep(kind, by_kind[kind]) for kind in order]
        return _resolved(self.das, dataset, nano_prepid, chain_prepid, steps)

    @staticmethod
    def _ordered_members(results: dict) -> list[str]:
        # Prefer the flat chain list; fall back to token-boundary matches anywhere in the response.
        return _request_prepids(results.get("chain"), embedded=False) or _request_prepids(results, embedded=True)


def resolve_chain(dataset, mode, *, das, fetcher, auth_session=None, prefer_auth=False,
                  wmlhegs=None, drpremix=None, chain=None, on_auth_unavailable=None) -> ResolvedChain:
    if prefer_auth and auth_session is not None:
        try:
            return AuthenticatedChainResolver(auth_session, das, fetcher).resolve(dataset, mode, chain=chain)
        except McMAuthUnavailable as exc:
            if on_auth_unavailable is not None:
                on_auth_unavailable(exc)
    return ProxyChainResolver(das, fetcher).resolve(dataset, mode, wmlhegs=wmlhegs, drpremix=drpremix, chain=chain)
