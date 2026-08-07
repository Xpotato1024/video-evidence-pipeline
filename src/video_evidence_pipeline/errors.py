"""Stable, safe errors exposed by the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION = 2
    AUTHORIZATION_OR_STATE = 3
    IO = 4
    INTEGRITY = 5


@dataclass(slots=True)
class VepError(Exception):
    code: str
    safe_message: str
    exit_code: ExitCode

    def __str__(self) -> str:
        return self.safe_message


def configuration_error(code: str, message: str) -> VepError:
    return VepError(code, message, ExitCode.CONFIGURATION)


def state_error(code: str, message: str) -> VepError:
    return VepError(code, message, ExitCode.AUTHORIZATION_OR_STATE)


def io_error(code: str, message: str) -> VepError:
    return VepError(code, message, ExitCode.IO)


def integrity_error(code: str, message: str) -> VepError:
    return VepError(code, message, ExitCode.INTEGRITY)
