"""Read-only status of the VOMS proxy and the McM SSO cookie, shown as a CLI banner.

The cookie status probes McM instead of trusting the file's expiry: under CERN 2FA a
Kerberos-only cookie looks fresh yet is rejected.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from infrastructure.mcm_auth import McMAuthUnavailable, existing_cookiefile

_VOMS_HINT = "voms-proxy-init -voms cms"
_COOKIE_HINT = "run `casino mcm-login` (Kerberos + 2FA), or pass --drpremix"
_PROBE_TIMEOUT = 8


@dataclass(frozen=True)
class AuthStatus:
    label: str
    ok: bool
    detail: str
    hint: str | None = None


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def voms_proxy_status(runner=subprocess.run) -> AuthStatus:
    label = "VOMS proxy"
    try:
        proc = runner(["voms-proxy-info", "-timeleft"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return AuthStatus(label, False, "voms-proxy-info not found", _VOMS_HINT)
    except OSError as exc:
        return AuthStatus(label, False, f"voms-proxy-info failed: {exc}", _VOMS_HINT)
    tokens = (line.strip() for line in (proc.stdout or "").splitlines())
    seconds = next((int(token) for token in tokens if token.lstrip("-").isdigit()), 0)
    if proc.returncode != 0 or seconds <= 0:
        return AuthStatus(label, False, "no valid proxy", _VOMS_HINT)
    return AuthStatus(label, True, f"valid, {_fmt_duration(seconds)} left")


def mcm_cookie_status(cookiefile=None, resolver=existing_cookiefile, authenticator=None) -> AuthStatus:
    label = "McM SSO cookie"
    path = resolver(cookiefile)
    if not path:
        return AuthStatus(label, False, "not found", _COOKIE_HINT)
    if authenticator is None:
        from infrastructure.mcm_auth import McMAuthSession

        def authenticator():
            return McMAuthSession(cookiefile=path, timeout=_PROBE_TIMEOUT).is_authenticated()

    try:
        accepted = authenticator()
    except McMAuthUnavailable:
        accepted = False
    except Exception as exc:
        return AuthStatus(label, False, f"present, could not verify ({type(exc).__name__})", _COOKIE_HINT)
    if accepted:
        return AuthStatus(label, True, "valid (McM accepts it)")
    return AuthStatus(label, False, "present but rejected by McM — re-auth needed (McM may require 2FA)", _COOKIE_HINT)


def _status_line(status: AuthStatus) -> str:
    line = f"  {'✓' if status.ok else '✗'} {status.label}: {status.detail}"
    if not status.ok and status.hint:
        line += f"   →  {status.hint}"
    return line


def auth_status_lines(
    runner=subprocess.run, cookiefile=None, cookie_resolver=existing_cookiefile, authenticator=None,
) -> list[str]:
    return [
        _status_line(voms_proxy_status(runner=runner)),
        _status_line(mcm_cookie_status(cookiefile=cookiefile, resolver=cookie_resolver, authenticator=authenticator)),
    ]
