"""A dict default is written with its keys sorted, so a client is the same bytes either way it is generated.

A description reaches the generator in two ways: in process from the class (`--class`), and as the
bytes of `{service}.describe` (`--service`), which are written with sorted keys. The class kept the
order a default's dict was declared in, so `emit` wrote `{"b": 1, "a": 2}` for one and
`{"a": 2, "b": 1}` for the other, `--check` compared bytes, and a client was reported stale
depending only on which way it was generated. The keys of a default's dict, at every depth, are
now written in sorted order, in each of the four places the emitter writes one.
"""

import json
import warnings

import pytest

from cliffracer import CliffracerService, rpc
from cliffracer.generate_client.cli import main
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import Description, canonical, describe
from tests.fixtures.model_defaults import (
    Aliasing,
    Awkward,
    Shapes,
    Stock,
    Strictness,
    Unions,
)

pytestmark = pytest.mark.unit

SERVICES = [Stock, Aliasing, Strictness, Unions, Shapes, Awkward]
INT = {"kind": "scalar", "name": "int"}
ITEM = {"kind": "model", "module": "tests.fixtures.model_defaults", "qualname": "Item"}


def _desc(params: list[dict]) -> Description:
    return Description.from_dict(
        {
            "service": "keys",
            "version": "1",
            "description_hash": "sha256:d",
            "methods": [
                {
                    "name": "m",
                    "doc": None,
                    "signature_hash": "sha256:m",
                    "params": params,
                    "returns": INT,
                }
            ],
        }
    )


def _wire(description: Description) -> Description:
    return Description.from_dict(json.loads(canonical(description.to_dict())))


def _described(service: type) -> Description:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return describe(service, service=service.__name__.lower(), version="1")


@pytest.mark.parametrize("service", SERVICES, ids=lambda service: service.__name__)
def test_a_client_generated_from_the_class_and_from_the_wire_is_the_same_bytes(service):
    in_process = _described(service)

    assert emit(in_process) == emit(_wire(in_process))


# One test for each place the emitter writes a dict of a default. Each writes a description whose
# dict is NOT in key order, as the class gives it, and holds it against the sorted order the wire gives.


def test_a_short_dict_default_is_written_by_key():
    unsorted = _desc(
        [
            {
                "name": "counts",
                "type": {"kind": "dict", "value": INT},
                "default": {"b": 1, "a": 2, "c": {"z": 1, "y": 2}},
            }
        ]
    )

    source = emit(unsorted)

    assert 'counts: dict[str, int] = {"a": 2, "b": 1, "c": {"y": 2, "z": 1}}' in source
    assert source == emit(_wire(unsorted))


def test_a_long_dict_default_that_is_split_is_written_by_key():
    keys = [f"key-number-{i}" for i in (7, 3, 9, 1, 5, 2, 8)]
    unsorted = _desc(
        [
            {
                "name": "counts",
                "type": {"kind": "dict", "value": INT},
                "default": {key: index for index, key in enumerate(keys)},
            }
        ]
    )

    source = emit(unsorted)

    written = [line.strip().split('"')[1] for line in source.splitlines() if "key-number-" in line]
    assert written == sorted(keys)
    assert source == emit(_wire(unsorted))


def test_the_dump_of_a_model_inside_a_default_is_written_by_key():
    unsorted = _desc(
        [
            {
                "name": "item",
                "type": ITEM,
                "default": {"name": "x", "color": "red", "tags": []},
                "rebuildable": True,
            }
        ]
    )

    source = emit(unsorted)

    assert '{"color": "red", "name": "x", "tags": []}' in source
    assert source == emit(_wire(unsorted))


def test_the_entries_of_a_dict_of_models_are_written_by_key():
    dump = {"name": "x", "color": "red", "tags": []}
    unsorted = _desc(
        [
            {
                "name": "items",
                "type": {"kind": "dict", "value": ITEM},
                "default": {"zeta": dump, "alpha": dump, "mid": dump},
                "rebuildable": True,
            }
        ]
    )

    source = emit(unsorted)

    assert source.index('"alpha"') < source.index('"mid"') < source.index('"zeta"')
    assert source == emit(_wire(unsorted))


def test_CONTROL_the_order_of_a_list_is_the_lists_own():
    unsorted = _desc([{"name": "xs", "type": {"kind": "list", "item": INT}, "default": [3, 1, 2]}])

    assert "xs: list[int] = [3, 1, 2]" in emit(unsorted)
    assert emit(unsorted) == emit(_wire(unsorted))


# --- --check: a client written with the old order is stale once --------------------------------------


class Counting(CliffracerService):
    @rpc
    async def tally(self, counts: dict[str, int] = {"b": 1, "a": 2}) -> int:  # noqa: B006
        return len(counts)


def test_a_client_written_in_the_old_order_is_reported_stale_once_and_regenerating_clears_it(
    tmp_path,
):
    target = tmp_path / "counting_client.py"
    args = ["--class", f"{__name__}:{Counting.__name__}", "--service", "counting", "--version", "1"]
    assert main([*args, "--out", str(target)]) == 0
    written = target.read_text()
    assert 'counts: dict[str, int] = {"a": 2, "b": 1}' in written

    target.write_text(written.replace('{"a": 2, "b": 1}', '{"b": 1, "a": 2}'))

    assert main([*args, "--out", str(target), "--check"]) == 8
    assert main([*args, "--out", str(target)]) == 0
    assert main([*args, "--out", str(target), "--check"]) == 0
