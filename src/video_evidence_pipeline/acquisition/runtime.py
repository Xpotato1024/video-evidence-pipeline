"""Runtime-only network acquisition configuration."""

from __future__ import annotations

import os
import re
from enum import StrEnum

from pydantic import Field, model_validator

from video_evidence_pipeline.domain import StrictModel
from video_evidence_pipeline.domain.job import SourceKind
from video_evidence_pipeline.errors import configuration_error, state_error

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BrowserCookieSource(StrEnum):
    BRAVE = "brave"
    CHROME = "chrome"
    CHROMIUM = "chromium"
    EDGE = "edge"
    FIREFOX = "firefox"
    OPERA = "opera"
    SAFARI = "safari"
    VIVALDI = "vivaldi"
    WHALE = "whale"


class AcquisitionRuntimeConfig(StrictModel):
    cookies_from_browser: BrowserCookieSource | None = None
    box_token_env: str | None = Field(default=None, max_length=128)
    panopto_token_env: str | None = Field(default=None, max_length=128)
    panopto_ytdlp_fallback: bool = False
    network_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    maximum_download_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    maximum_redirects: int = Field(default=5, ge=0, le=10)

    @model_validator(mode="after")
    def environment_names_are_safe(self) -> AcquisitionRuntimeConfig:
        for name in (self.box_token_env, self.panopto_token_env):
            if name is not None and not _ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError("token environment variable name is invalid")
        return self

    def validate_for(self, kind: SourceKind) -> None:
        if kind is SourceKind.LOCAL_FILE or kind is SourceKind.DIRECT_HTTP:
            invalid = any(
                (
                    self.cookies_from_browser,
                    self.box_token_env,
                    self.panopto_token_env,
                    self.panopto_ytdlp_fallback,
                )
            )
        elif kind is SourceKind.GENERIC_YTDLP:
            invalid = any((self.box_token_env, self.panopto_token_env, self.panopto_ytdlp_fallback))
        elif kind is SourceKind.PANOPTO:
            invalid = self.box_token_env is not None or (
                self.cookies_from_browser is not None and not self.panopto_ytdlp_fallback
            )
        elif kind is SourceKind.BOX:
            invalid = any(
                (
                    self.cookies_from_browser,
                    self.panopto_token_env,
                    self.panopto_ytdlp_fallback,
                )
            )
        else:
            invalid = True
        if invalid:
            raise configuration_error(
                "ACQUISITION_RUNTIME_CONFIG_INVALID",
                "runtime credential options do not match the source adapter",
            )

    def token_from_environment(self, name: str | None, *, provider: str) -> str:
        if name is None:
            raise state_error(
                f"{provider.upper()}_CREDENTIAL_REQUIRED",
                f"{provider} acquisition requires an explicit token environment variable",
            )
        value = os.environ.get(name)
        if value is None or not value.strip():
            raise state_error(
                f"{provider.upper()}_CREDENTIAL_REQUIRED",
                f"{provider} access token is unavailable",
            )
        return value
