"""Machine-readable handoff from acquisition to human authentication."""

from __future__ import annotations

from typing import Literal, TypedDict

from video_evidence_pipeline.acquisition.runtime import AcquisitionRuntimeConfig
from video_evidence_pipeline.domain.locators import panopto_identity
from video_evidence_pipeline.errors import VepError

_PANOPTO_AUTHENTICATION_CODES = {
    "PANOPTO_AUTH_REFERENCE_REQUIRED",
    "PANOPTO_AUTHENTICATION_REQUIRED",
    "PANOPTO_AUTHENTICATION_EXPIRED",
    "PANOPTO_CREDENTIAL_REQUIRED",
    "PANOPTO_MFA_REQUIRED",
    "PANOPTO_REAUTH_REQUIRED",
    "PANOPTO_SSO_REQUIRED",
}
_BOX_AUTHENTICATION_CODES = {"BOX_CREDENTIAL_REQUIRED", "BOX_REAUTH_REQUIRED"}


class AuthenticationMethod(TypedDict):
    kind: Literal["browser_session", "environment_token"]
    credential_options: list[str]
    browser: str | None
    browser_url: str | None


class AuthenticationHandoff(TypedDict):
    provider: Literal["panopto", "box"]
    action: Literal["authenticate", "configure_credential"]
    actor: Literal["user"]
    methods: list[AuthenticationMethod]


def authentication_handoff(
    error: VepError,
    *,
    runtime: AcquisitionRuntimeConfig | None,
    panopto_locator: str | None,
    via_workflow: bool,
) -> AuthenticationHandoff | None:
    """Represent only recognized auth failures and provider-supported operations."""
    browser = runtime.cookies_from_browser if runtime is not None else None
    if error.code in _BOX_AUTHENTICATION_CODES:
        return {
            "provider": "box",
            "action": (
                "configure_credential"
                if error.code == "BOX_CREDENTIAL_REQUIRED"
                else "authenticate"
            ),
            "actor": "user",
            "methods": [
                {
                    "kind": "environment_token",
                    "credential_options": ["--box-token-env"],
                    "browser": None,
                    "browser_url": None,
                }
            ],
        }
    if error.code not in _PANOPTO_AUTHENTICATION_CODES:
        return None

    token_reference = runtime.panopto_token_env if runtime is not None else None
    missing_reference = token_reference is None and browser is None
    methods: list[AuthenticationMethod] = []
    method = error.authentication_method
    if method == "browser_session" or (
        method is None and (browser is not None or missing_reference)
    ):
        browser_url = None
        if panopto_locator is not None:
            tenant, session_id = panopto_identity(panopto_locator)
            # Custom tenant hostnames are supported by acquisition; only link directly to
            # a known Panopto-operated namespace in a user-facing authentication handoff.
            if tenant.endswith((".panopto.com", ".panopto.eu", ".panopto.asia")):
                browser_url = f"https://{tenant}/Panopto/Pages/Viewer.aspx?id={session_id}"
        methods.append(
            {
                "kind": "browser_session",
                "credential_options": (
                    ["--cookies-from-browser"]
                    if via_workflow
                    else ["--cookies-from-browser", "--panopto-ytdlp-fallback"]
                ),
                "browser": browser.value if browser is not None else None,
                "browser_url": browser_url,
            }
        )
    if method == "environment_token" or (method is None and (token_reference or missing_reference)):
        methods.append(
            {
                "kind": "environment_token",
                "credential_options": ["--panopto-token-env", "--panopto-tenant-host"],
                "browser": None,
                "browser_url": None,
            }
        )
    return {
        "provider": "panopto",
        "action": (
            "configure_credential"
            if missing_reference
            or error.code in {"PANOPTO_AUTH_REFERENCE_REQUIRED", "PANOPTO_CREDENTIAL_REQUIRED"}
            else "authenticate"
        ),
        "actor": "user",
        "methods": methods,
    }
