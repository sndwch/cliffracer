"""`description_hash` is the hash of the whole description, less what the configuration decides.

It was taken over the methods alone, so a change to a listener's payload model, to which subject
a listener reads, or to an output's contract left it where it was, and a client generated before
the change looked current. It now covers the methods, the listeners, the models and the outputs.
The streams, and a listener's effective subject, durable and queue group, come from the
configuration, so they are left out: the hash is then the same from `describe(cls)` and from a
running service, which is what lets a client generated from either carry the same
`DESCRIPTION_HASH`.
"""

import pytest
from pydantic import BaseModel, create_model

from cliffracer import CliffracerService, ServiceConfig, listener, rpc, validated_listener
from cliffracer.core.jetstream import StreamSpec
from cliffracer.introspect import _CONFIG_DEPENDENT_LISTENER_KEYS, describe

pytestmark = pytest.mark.unit


def _payload(**extra):
    return create_model("Payload", order_id=(str, ...), **dict.fromkeys(extra, (int, 0)))


class Out(BaseModel):
    n: int


def _service(
    *,
    payload=None,
    pattern="orders.placed",
    durable="placed_worker",
    fanout_listener="audit.all",
    method_doc="Place an order.",
    listener_doc="A placed order.",
    fanout=False,
    durable_with_fanout=False,
    output_subject=None,
    rpc_model=None,
):
    payload = payload or _payload()

    class S(CliffracerService):
        @rpc
        async def place(self, sku: str) -> int:
            return 1

        place.__doc__ = method_doc  # type: ignore[attr-defined]

        @validated_listener(
            pattern,
            payload,
            durable=durable if (durable_with_fanout or not fanout) else None,
            fanout=fanout,
        )
        async def on_placed(self, event: payload) -> None:  # type: ignore[valid-type]
            pass

        on_placed.__doc__ = listener_doc

        @listener(fanout_listener, fanout=True, cross_namespace=True)
        async def on_audit(self, subject: str) -> None: ...

    if rpc_model is not None:

        class T(S):
            @rpc
            async def take(self, thing: rpc_model) -> int:  # type: ignore[valid-type]
                return 1

        return T
    if output_subject is not None:
        from cliffracer import Output

        class U(S):
            progress = Output(Out, output_subject)

        return U
    return S


def _hash(cls, **config):
    return describe(cls, service="s", version="1", **config).description_hash


def test_the_hash_is_stable_for_one_class():
    assert _hash(_service()) == _hash(_service())


@pytest.mark.parametrize(
    ("label", "changed"),
    [
        ("a field added to a listener's payload model", {"payload": _payload(extra=1)}),
        ("the subject a listener reads", {"pattern": "orders.accepted"}),
        ("the subject of a fanout listener", {"fanout_listener": "audit.some"}),
        ("a method's docstring", {"method_doc": "Place an order, then say so."}),
        ("a listener's docstring", {"listener_doc": "A placed order, accepted."}),
        ("a listener's delivery kind", {"fanout": True}),
        ("an output's subject", {"output_subject": "orders.progress"}),
        ("a model an rpc takes", {"rpc_model": _payload()}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_change_to_the_contract_moves_the_hash(label, changed):
    assert _hash(_service(**changed)) != _hash(_service()), label


def test_a_change_to_a_field_of_a_model_an_rpc_takes_moves_the_hash():
    assert _hash(_service(rpc_model=_payload(extra=1))) != _hash(_service(rpc_model=_payload()))


def test_two_outputs_with_different_subjects_hash_differently():
    assert _hash(_service(output_subject="orders.a")) != _hash(_service(output_subject="orders.b"))


def _config(**overrides):
    stream = [StreamSpec(name="ORDERS", subjects=["shop.orders.placed"])]
    return ServiceConfig(
        name="s", health_port=0, namespace="shop", jetstream_streams=stream, **overrides
    )


def _cases():
    """A class and the configurations it is valid under. A durable is refused with fanout when
    JetStream is on and refused without it when JetStream is off, so no one class is both."""
    on = {"config": _config(jetstream_enabled=True)}
    off = {"config": _config(jetstream_enabled=False)}
    return [
        ("a durable listener", _service(), {"none": {}, "jetstream on": on}),
        ("a fanout listener", _service(fanout=True), {"none": {}, "on": on, "off": off}),
        (
            "a fanout listener that names a durable",
            _service(fanout=True, durable_with_fanout=True),
            {"none": {}, "off": off},
        ),
    ]


def test_the_hash_is_the_same_whatever_the_configuration():
    for label, cls, configs in _cases():
        hashes = {name: _hash(cls, **config) for name, config in configs.items()}

        assert len(set(hashes.values())) == 1, (label, hashes)


def test_the_durable_name_is_not_part_of_the_hash():
    assert _hash(_service(durable="other_name")) == _hash(_service())


def test_every_listener_key_the_configuration_changes_is_left_out_of_the_hash():
    """The exclusion list is checked against what actually varies, so a key that starts to
    depend on the configuration is noticed here rather than left in a hash that then differs
    between the offline description and a running service's."""
    varying: set[str] = set()
    for _, cls, configs in _cases():
        described = [
            [item.to_dict() for item in describe(cls, service="s", version="1", **config).listeners]
            for config in configs.values()
        ]
        for key in described[0][0]:
            if len({repr([entry[key] for entry in listeners]) for listeners in described}) > 1:
                varying.add(key)

    assert {"effective_subject", "durable"} <= varying, "the probe saw no variation"
    assert varying <= set(_CONFIG_DEPENDENT_LISTENER_KEYS), varying
