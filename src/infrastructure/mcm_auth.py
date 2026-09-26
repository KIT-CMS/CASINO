from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path

MCM_BASE = "https://cms-pdmv-prod.web.cern.ch/mcm"
MCM_CHAINED_GET_URL = MCM_BASE + "/restapi/chained_requests/get/{chain}"
MCM_PROBE_URL = MCM_BASE + "/restapi/users/get_role"
_DEFAULT_COOKIEFILE = "~/.mcm-sso-cookie.txt"
_SSO_HOST = "auth.cern.ch"


class McMAuthUnavailable(RuntimeError):
    pass


def existing_cookiefile(cookiefile: str | None = None) -> str | None:
    """First existing cookie file among the explicit path, $MCM_SSO_COOKIEFILE and the default."""
    for candidate in (cookiefile, os.environ.get("MCM_SSO_COOKIEFILE"), _DEFAULT_COOKIEFILE):
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def default_cookiefile(cookiefile: str | None = None) -> str:
    """Where a new cookie should be written (need not exist yet)."""
    return str(Path(cookiefile or os.environ.get("MCM_SSO_COOKIEFILE") or _DEFAULT_COOKIEFILE).expanduser())


class McMAuthSession:
    def __init__(self, cookiefile: str | None = None, opener=None, timeout: int = 30) -> None:
        self._cookiefile = cookiefile
        self._opener = opener
        self.timeout = timeout

    def _resolve_cookiefile(self) -> str | None:
        found = existing_cookiefile(self._cookiefile)
        if found:
            return found
        if not (shutil.which("auth-get-sso-cookie") and shutil.which("klist")):
            return None
        target = Path(self._cookiefile or _DEFAULT_COOKIEFILE).expanduser()
        try:
            if subprocess.run(["klist", "-s"]).returncode == 0:
                subprocess.run(["auth-get-sso-cookie", "-u", MCM_BASE, "-o", str(target)], check=True)
                if target.exists():
                    return str(target)
        except (OSError, subprocess.CalledProcessError):
            pass
        return None

    def _build_opener(self):
        if self._opener is not None:
            return self._opener
        cookiefile = self._resolve_cookiefile()
        if not cookiefile:
            raise McMAuthUnavailable(
                "No McM SSO cookie. Set MCM_SSO_COOKIEFILE, place ~/.mcm-sso-cookie.txt, "
                "or install auth-get-sso-cookie and run `kinit`."
            )
        jar = MozillaCookieJar(cookiefile)
        jar.load(ignore_discard=True, ignore_expires=True)
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def is_authenticated(self) -> bool:
        """True iff McM serves a cheap authenticated request instead of bouncing to CERN SSO."""
        opener = self._build_opener()
        try:
            with opener.open(MCM_PROBE_URL, timeout=self.timeout) as response:
                return _SSO_HOST not in response.geturl()
        except urllib.error.HTTPError as exc:
            return _SSO_HOST not in (exc.geturl() or "")

    def get_chained_request(self, chain_prepid: str) -> dict:
        try:
            with self._build_opener().open(MCM_CHAINED_GET_URL.format(chain=chain_prepid), timeout=self.timeout) as response:
                if _SSO_HOST in response.geturl():
                    raise McMAuthUnavailable("McM redirected to CERN SSO — cookie missing or expired.")
                data = json.loads(response.read().decode("utf-8"))
        except McMAuthUnavailable:
            raise
        except Exception as exc:
            raise McMAuthUnavailable(f"McM chained_requests fetch failed: {exc}") from exc
        return data.get("results", {}) if isinstance(data, dict) else {}
