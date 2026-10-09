"""Configuration settings for cliffracer-cyanide."""

from __future__ import annotations

import math

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RANDOM_MODE = "random"

# The names a mode can be given by, as `normalize_mode` spells them: the four
# failure modes and the aliases the dispatch hook accepts for each.
FAULT_MODE_NAMES = frozenset(
    {
        "slow",
        "raise_after_delay",
        "raise_delay",
        "raise",
        "fault",
        "sleep_past_timeout",
        "timeout",
        "sleep_timeout",
        "drop_reply",
        "drop",
    }
)
MODE_NAMES = FAULT_MODE_NAMES | {RANDOM_MODE}


def normalize_mode(mode: str) -> str:
    """The spelling a mode name is compared in: lower case, `-` read as `_`."""
    return mode.lower().replace("-", "_")


def check_seconds(value: float, what: str) -> float:
    """Return *value*, or raise `ValueError` naming *what* unless it is a finite number of seconds >= 0.

    A negative delay is refused by `asyncio.sleep` and a NaN one is not a delay, and neither is
    discovered until a fault runs, inside the dispatch hook.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{what} must be a finite number of seconds, 0 or more, not {value!r}")
    return float(value)


def check_a_mode(mode: str | None) -> str | None:
    """Return *mode*, or raise `ValueError` naming the names that exist.

    An empty mode is no mode, as the dispatch hook reads it.
    """
    if mode and normalize_mode(mode) not in MODE_NAMES:
        raise ValueError(
            f"unknown cyanide mode {mode!r}: the modes are {', '.join(sorted(MODE_NAMES))}"
        )
    return mode


class CyanideConfig(BaseSettings):
    """Settings for cyanide fault injection.

    Disabled by default to prevent unintended fault activation.
    """

    model_config = SettingsConfigDict(env_prefix="CLIFFRACER_CYANIDE_")

    enabled: bool = False
    slow_delay: float = 1.0
    raise_delay: float = 0.5
    # Past the 30 s that `ServiceClient` and `ServiceConfig.request_timeout`
    # default to, so a call that reaches this mode outlasts its caller.
    sleep_timeout_duration: float = 60.0
    mode: str | None = None

    # Random mode walks these in the order written, each taking its share of
    # the unit interval, so each must be a probability and together they must
    # not exceed 1: a weight past the remainder is cut short, not honoured.
    slow_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    drop_reply_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    raise_after_delay_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    sleep_past_timeout_weight: float = Field(default=0.0, ge=0.0, le=1.0)

    seed: str | int | None = None

    # How many injections the extension remembers. Bounded because the record
    # lives for the life of the service and a soak runs for hours: an unbounded
    # list would grow with traffic. Oldest is dropped first, and the count of
    # drops is reported, so a reader can tell "not injected" from "no longer
    # remembered" -- see CyanideExtension.injections.
    #
    # ge=1 because both smaller values are traps. A negative limit reaches
    # `deque(maxlen=...)` and dies with "maxlen must be non-negative", naming
    # the implementation rather than the field. Zero is worse: it is accepted,
    # records nothing, and counts every injection as dropped -- so a reader
    # sees a record that is permanently "incomplete" and can never attribute
    # anything, which is the false-failure shape this record exists to remove.
    injection_record_limit: int = Field(default=1024, ge=1)

    @field_validator("slow_delay", "raise_delay", "sleep_timeout_duration")
    @classmethod
    def _delay_can_be_slept(cls, value: float, info: ValidationInfo) -> float:
        return check_seconds(value, str(info.field_name))

    @field_validator("mode")
    @classmethod
    def _mode_is_a_mode(cls, mode: str | None) -> str | None:
        return check_a_mode(mode)

    @model_validator(mode="after")
    def _weights_fit_in_one(self) -> CyanideConfig:
        total = (
            self.slow_weight
            + self.drop_reply_weight
            + self.raise_after_delay_weight
            + self.sleep_past_timeout_weight
        )
        # The tolerance is for sums like 0.1 + 0.2 + 0.7, which are 1 to the eye.
        if total > 1.0 + 1e-9:
            raise ValueError(
                f"the four *_weight settings add up to {total:g}; they are shares of one "
                "probability and must add up to at most 1"
            )
        return self


__all__ = ["CyanideConfig"]
