"""Strict machine-facing domain contracts."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict


def _normalize_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDateTime = Annotated[AwareDatetime, AfterValidator(_normalize_utc)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
