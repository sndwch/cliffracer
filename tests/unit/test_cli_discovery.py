import pytest

from cliffracer.cli.discovery import DiscoveryError, resolve_targets
from tests.unit.cli_fixtures.sample_services import AlphaService, BetaService

pytestmark = pytest.mark.unit

MOD = "tests.unit.cli_fixtures.sample_services"


def test_resolve_explicit_class():
    assert resolve_targets([f"{MOD}:AlphaService"]) == [AlphaService]


def test_resolve_bare_module_discovers_only_own_services():
    result = resolve_targets([MOD])
    assert set(result) == {AlphaService, BetaService}  # NotAService excluded
    assert len(result) == 2


def test_bare_module_discovery_skips_a_service_the_module_only_imports():
    """`cliffracer run mypkg.services` must not also start every service class
    that module imports. `sample_services` cannot show it: the only service it
    imports is `CliffracerService`, which a separate check already excludes."""
    from tests.unit.cli_fixtures.foreign_service import ForeignService
    from tests.unit.cli_fixtures.imports_a_service import OwnService

    module = "tests.unit.cli_fixtures.imports_a_service"

    assert resolve_targets([module]) == [OwnService]
    assert resolve_targets([f"{module}:ForeignService"]) == [ForeignService]


def test_dedup_preserves_order():
    result = resolve_targets([f"{MOD}:BetaService", MOD, f"{MOD}:BetaService"])
    assert result[0] is BetaService
    assert result.count(BetaService) == 1
    assert AlphaService in result


def test_unknown_module_raises():
    with pytest.raises(DiscoveryError, match="could not import"):
        resolve_targets(["nope.does.not.exist"])


def test_missing_attribute_raises():
    with pytest.raises(DiscoveryError, match="has no attribute 'Ghost'"):
        resolve_targets([f"{MOD}:Ghost"])


def test_non_service_attribute_raises():
    with pytest.raises(DiscoveryError, match="not a Cliffracer service"):
        resolve_targets([f"{MOD}:NotAService"])


def test_empty_discovery_raises():
    with pytest.raises(DiscoveryError, match="no Cliffracer services"):
        resolve_targets(["cliffracer.core.service_config"])  # module with no service classes


def test_module_with_broken_import_raises_discovery_error():
    with pytest.raises(DiscoveryError, match="could not import"):
        resolve_targets(["tests.unit.cli_fixtures.broken_import"])


def test_a_module_that_raises_at_import_raises_discovery_error_naming_the_exception():
    """Module-level code that raises -- a missing environment variable, a config
    parsed at import -- is the common way a service module fails to import, and
    it is not an ImportError."""
    with pytest.raises(DiscoveryError, match="could not import.*ValueError: module-level config"):
        resolve_targets(["tests.unit.cli_fixtures.raises_at_import"])
