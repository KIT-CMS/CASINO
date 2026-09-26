"""Interactive CERN SSO login (Kerberos + OTP) that writes a cookie McM accepts.

McM enforces 2FA, so the Kerberos-only cookie from auth-get-sso-cookie is rejected. This reuses
its SPNEGO handshake and cookie serializer and adds the OTP form step.
"""
from __future__ import annotations

from infrastructure.mcm_auth import MCM_BASE, McMAuthUnavailable

_AUTH_HOSTNAME = "auth.cern.ch"


def _import_cern_sso():
    try:
        from auth_get_sso_cookie import cern_sso
    except Exception as exc:  # pragma: no cover - environment dependent
        raise McMAuthUnavailable(f"interactive SSO login needs the auth-get-sso-cookie package: {exc}")
    return cern_sso


def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html or "", "html.parser")


def _is_otp_page(html: str) -> bool:
    return _soup(html).find("input", attrs={"name": "otp"}) is not None


def _otp_form_fields(html: str):
    """(action, {field: value}) for the OTP form incl. hidden inputs, or None without a form."""
    form = _soup(html).find("form")
    if form is None:
        return None
    data = {inp.get("name"): inp.get("value", "") or "" for inp in form.find_all("input") if inp.get("name")}
    return form.get("action"), data


def _submit_otp(session, otp_page, prompt_otp, verify, printer, attempts):
    """Prompt for and POST the OTP, retrying while CERN rejects it; returns the post-SSO response."""
    page = otp_page
    for attempt in range(1, attempts + 1):
        fields = _otp_form_fields(page.text)
        if fields is None:
            raise McMAuthUnavailable("CERN 2FA page had no submittable form.")
        action, data = fields
        code = (prompt_otp(attempt) or "").strip()
        if not code:
            raise McMAuthUnavailable("2FA aborted — no code entered.")
        data["otp"] = code
        page = session.post(action, data=data, verify=verify)
        if _AUTH_HOSTNAME in page.url and _is_otp_page(page.text):
            printer("CERN rejected the 2FA code — try again.")
            continue
        return page
    raise McMAuthUnavailable(f"2FA failed after {attempts} attempts.")


def login_with_2fa(prompt_otp, *, url=MCM_BASE, cookiefile, verify=True,
                   auth_hostname=_AUTH_HOSTNAME, printer=print, attempts=3) -> str:
    """Log in to url via Kerberos + OTP and save the cookies to cookiefile; returns cookiefile."""
    cern_sso = _import_cern_sso()
    # login_with_kerberos would POST the OTP page as a SAML auto-form; neutralise that.
    saved_saml = cern_sso.post_session_saml
    cern_sso.post_session_saml = lambda session, response: None
    try:
        session, response = cern_sso.login_with_kerberos(url, verify, auth_hostname, silent=True)
    finally:
        cern_sso.post_session_saml = saved_saml

    if response.status_code != 302 and _is_otp_page(response.text):
        response = _submit_otp(session, response, prompt_otp, verify, printer, attempts)
    if getattr(response, "status_code", None) == 302 and response.headers.get("Location"):
        session.get(response.headers["Location"], verify=verify)
    cern_sso.save_cookies_lwp(session.cookies, cookiefile)
    return cookiefile
