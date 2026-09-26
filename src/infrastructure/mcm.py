from __future__ import annotations

import json
import re
import ssl
import urllib.request

MCM_PUBLIC_API = "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests"
MCM_SETUP_URL = MCM_PUBLIC_API + "/get_setup/{setup_id}"
MCM_FRAGMENT_URL = MCM_PUBLIC_API + "/get_fragment/{setup_id}"
MCM_REQUEST_GET_URL = MCM_PUBLIC_API + "/get/{prepid}"
FRAGMENT_URL_RE = re.compile(r"get_fragment/([A-Za-z0-9-]+)")


class McMResponseError(RuntimeError):
    pass


def fetch_text(url: str, timeout: int, verify_certs: bool) -> str:
    context = ssl.create_default_context() if verify_certs else ssl._create_unverified_context()
    with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
        return normalize_mcm_payload(response.read().decode("utf-8"))


def normalize_mcm_payload(text: str) -> str:
    """McM sometimes returns the script as a JSON-encoded string; unwrap it."""
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] != '"' or stripped[-1] != '"':
        return text
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    return decoded if isinstance(decoded, str) else text


def validate_mcm_artifact_payload(text: str, *, resource_kind: str, setup_id: str) -> str:
    normalized = normalize_mcm_payload(text)
    stripped = normalized.strip()
    prefix = f"McM {resource_kind} download failed for {setup_id}"
    if not stripped:
        raise McMResponseError(f"{prefix}: empty response")
    if stripped.lower().startswith(("<!doctype html", "<html")):
        raise McMResponseError(f"{prefix}: unexpected HTML response")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return normalized
    if isinstance(payload, dict) and payload.get("results") is False:
        raise McMResponseError(f"{prefix}: {payload.get('message') or 'McM returned an unsuccessful response'}")
    raise McMResponseError(f"{prefix}: unexpected JSON response")


def extract_fragment_ids(script_text: str) -> list[str]:
    return list(dict.fromkeys(FRAGMENT_URL_RE.findall(script_text)))


def get_request(prepid, *, fetcher=fetch_text, timeout: int = 30, verify_certs: bool = False) -> dict:
    data = json.loads(fetcher(MCM_REQUEST_GET_URL.format(prepid=prepid), timeout, verify_certs))
    return data.get("results", {}) if isinstance(data, dict) else {}


def member_of_chain(prepid, *, fetcher=fetch_text, timeout: int = 30, verify_certs: bool = False) -> list[str]:
    chains = get_request(prepid, fetcher=fetcher, timeout=timeout, verify_certs=verify_certs).get("member_of_chain", [])
    return list(chains) if isinstance(chains, list) else []
