"""A `nats_url` that is refused never puts its password in the error, the log line or the repr.

A broker URL may carry a user and a password, and pydantic prints the input it refused into the
message, into `errors()` and `json()`, and into the traceback. The refusal is built by the
validator with the input redacted instead, so the password is in none of them. The text a refusal
adds names the field and the reason and never repeats the value, and the part of a URL in front of
a `://` that is not a scheme, which may be a user and a password, is not repeated either.

What this does not cover: loguru's `diagnose=True`, its default, prints the value of every local
variable on the lines of a traceback, so a raw URL held in a caller's frame is shown by a sink
configured that way whatever the exception says. These read sinks with `diagnose=False`;
`test_a_refused_config_overlay_ends_the_run_and_shows_no_secret.py` reads a `diagnose=True` sink.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from loguru import logger
from pydantic import ValidationError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.cli.discovery import DiscoveryError
from cliffracer.cli.main import build_orchestrator
from cliffracer.core.construction import apply_config_overlay
from cliffracer.runners.orchestrator import ServiceRunner
from tests.unit.cli_fixtures import refused_nats_url as fixture
from tests.unit.cli_fixtures.refused_nats_url import BadlyConfiguredService

pytestmark = pytest.mark.unit

SECRET = "sup3rs3cretpassw0rd"
MOD = "tests.unit.cli_fixtures.refused_nats_url"

# Each is refused, and each carries the password somewhere a careless message would repeat it.
URLS_WITH_THE_PASSWORD = [
    f"ftp://user:{SECRET}@h:4333",  # a wrong scheme
    f"nats://user:{SECRET}@h:notaport",  # a bad port
    f"nats://user:{SECRET}@",  # no host
    f"user:{SECRET}@http://x",  # what is in front of '://' is not a scheme
    f"NATS://user:{SECRET}@h:4333",  # a scheme in upper case
    f"nats://user/{SECRET}@h:4333",  # a '/' in the user ends the host early
    f"nats://user:{SECRET}@a b:4333",  # whitespace in the host
    f"nats://user:{SECRET}@h:4333,nats://user:{SECRET}@g:4333",  # two servers
]


def _every_text_of(error: ValidationError) -> dict[str, str]:
    return {
        "str": str(error),
        "repr": repr(error),
        "errors()": repr(error.errors()),
        "json()": error.json(),
        "cause": repr(error.__cause__),
        "context": repr(error.__context__),
    }


def _refusal(url: str) -> ValidationError:
    with pytest.raises(ValidationError) as refused:
        ServiceConfig(name="a", nats_url=url)
    return refused.value


def test_CONTROL_the_leak_this_guards_against_is_real_for_pydantic_alone():
    """Without the redaction pydantic repeats the whole input, so the checks below can fail."""
    from pydantic import BaseModel, field_validator

    class Plain(BaseModel):
        url: str

        @field_validator("url")
        @classmethod
        def _refuse(cls, value: str) -> str:
            raise ValueError("refused")

    with pytest.raises(ValidationError) as refused:
        Plain(url=URLS_WITH_THE_PASSWORD[0])

    assert SECRET in str(refused.value)
    assert SECRET in repr(refused.value.errors())
    assert SECRET in refused.value.json()


@pytest.mark.parametrize("url", URLS_WITH_THE_PASSWORD)
def test_the_password_is_in_no_text_of_the_refusal(url):
    refused = _refusal(url)

    leaks = {where: text for where, text in _every_text_of(refused).items() if SECRET in text}
    assert leaks == {}, leaks


@pytest.mark.parametrize("url", URLS_WITH_THE_PASSWORD)
def test_the_refusal_still_names_the_field_and_the_reason(url):
    """The instrument: a refusal that said nothing would pass the check above."""
    refused = _refusal(url)

    assert refused.errors()[0]["loc"] == ("nats_url",)
    assert "nats_url cannot be connected to" in str(refused)
    assert "***@" in str(refused) or "***@" in repr(refused.errors()), str(refused)


def test_an_assignment_hides_the_password_and_leaves_the_config_alone():
    config = ServiceConfig(name="a")
    before = config.nats_url

    with pytest.raises(ValidationError) as refused:
        config.nats_url = URLS_WITH_THE_PASSWORD[0]

    assert [w for w, t in _every_text_of(refused.value).items() if SECRET in t] == []
    assert config.nats_url == before


def test_the_overlay_of_a_flag_hides_the_password_from_the_error():
    svc = CliffracerService(ServiceConfig(name="a"))

    with pytest.raises(ValidationError) as refused:
        apply_config_overlay(svc, {"nats_url": URLS_WITH_THE_PASSWORD[0]})

    assert [w for w, t in _every_text_of(refused.value).items() if SECRET in t] == []


def _captured_log_of(run) -> str:
    buffer = io.StringIO()
    sink = logger.add(buffer, level="DEBUG", diagnose=False, backtrace=False)
    try:
        run()
    finally:
        logger.remove(sink)
    return buffer.getvalue()


class _Svc(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="pw_svc", health_port=0))


def test_the_runners_refusal_line_does_not_show_the_password():
    runner = ServiceRunner(_Svc, overrides={"nats_url": URLS_WITH_THE_PASSWORD[0]})
    runner._running = True
    runner._shutdown_event.set()  # leave after the first failure instead of waiting to retry

    log = _captured_log_of(lambda: asyncio.run(runner._run_service()))

    assert "Not retrying" in log and "nats_url cannot be connected to" in log, log
    assert SECRET not in log, log
    # The refusal line is the field and the reason, once, and not the ValidationError's own text.
    not_retrying = [line for line in log.splitlines() if "Not retrying" in line]
    assert len(not_retrying) == 1, log
    assert "nats_url: nats_url cannot be connected to" in not_retrying[0], not_retrying[0]
    assert "input_value" not in log and "validation error for" not in log, log


def test_the_cli_reports_a_refused_url_without_the_password():
    with pytest.raises(DiscoveryError) as refused:
        build_orchestrator(
            [f"{MOD}:{BadlyConfiguredService.__name__}"],
            nats_url=None,
            log_level=None,
            config_path=None,
        )

    assert "nats_url cannot be connected to" in str(refused.value)
    assert SECRET not in str(refused.value)
    assert SECRET not in repr(refused.value.__cause__)
    assert fixture.PASSWORD == SECRET, "the fixture and this file must name the same password"
