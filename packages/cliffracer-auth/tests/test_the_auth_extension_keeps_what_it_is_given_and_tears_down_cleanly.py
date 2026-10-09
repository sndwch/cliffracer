"""What the auth extension keeps of its arguments, and what it does for a timer with no token.

An extension built with no issuer is refused for the missing argument. A header given after the
issuer positionally is kept by each service's copy. An issuer or a token factory given as a
`SharedDependency` is the one used. A timer with no token and no default user runs with no identity,
and teardown after it does nothing.
"""

import pytest
from cliffracer_auth import AuthConfig
from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import SimpleAuthService, auth_context_var

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-a-real-one-0123456789abcdef"


def _issuer() -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET))


def _timer() -> WorkerContext:
    return WorkerContext(
        kind="timer", subject="svc.timer.tick", headers={}, correlation_id=None, payload={}, data={}
    )


def test_an_extension_with_no_issuer_is_refused_for_its_missing_argument():
    with pytest.raises(TypeError, match="auth"):
        AuthExtension()


def test_a_header_given_after_the_issuer_positionally_is_kept_by_each_service():
    """`AuthExtension.__new__` wraps the issuer in `SharedDependency` and passes the arguments on to
    `Extension.__new__`, which records them (`_spec_args`) to build each service's own copy. Python
    calls `__init__` with the ORIGINAL arguments whatever `__new__` passed on, so the extension
    declared on the class reads its header either way; only the per-service copy shows what was
    recorded. So the header is read from the bound service's extension."""
    issuer = _issuer()

    class Svc(CliffracerService):
        auth = AuthExtension(issuer, "X-Token")

    bound = Svc(ServiceConfig(name="svc", health_port=0)).auth

    assert (bound.header, bound.auth) == ("x-token", issuer)


def test_an_issuer_given_as_a_shared_dependency_is_the_issuer_used():
    issuer = _issuer()

    assert AuthExtension(SharedDependency(issuer)).auth is issuer


def test_a_token_factory_given_as_a_shared_dependency_is_the_factory_called():
    def factory() -> str:
        return "t"

    ext = AuthExtension(_issuer(), outbound_token_factory=SharedDependency(factory))

    assert ext.outbound_token_factory is factory


async def test_a_timer_without_a_token_or_a_default_user_runs_with_no_identity():
    ext = AuthExtension(_issuer())
    ctx = _timer()

    await ext.worker_setup(ctx)

    assert ctx.data == {}
    assert auth_context_var.get() is None


async def test_teardown_after_a_dispatch_that_set_no_identity_does_nothing():
    ext = AuthExtension(_issuer())
    ctx = _timer()
    await ext.worker_setup(ctx)

    await ext.worker_teardown(ctx)

    assert auth_context_var.get() is None
