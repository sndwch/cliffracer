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
