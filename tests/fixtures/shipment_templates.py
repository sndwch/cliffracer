"""Shipment settings and service contracts shared by template acceptance cases."""

from pydantic import BaseModel, ConfigDict, Field

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import ServiceClient
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import describe
from cliffracer.runners import ServiceTemplate


class ShipmentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse: str
    destinations: list[str]
    quantities: dict[str, int] = Field(default_factory=dict)


class Parcel(BaseModel):
    sku: str
    quantity: int = Field(gt=0)


class ShipmentReceipt(BaseModel):
    warehouse: str
    destination: str
    quantity: int


class Shipments(CliffracerService):
    def __init__(self, settings: ShipmentSettings, runtime: ServiceConfig):
        super().__init__(runtime)
        self.settings = settings
        self.shipped = 0
        self.stops = 0

    @rpc
    async def ship(self, parcel: Parcel) -> ShipmentReceipt:
        self.shipped += parcel.quantity
        return ShipmentReceipt(
            warehouse=self.settings.warehouse,
            destination=self.settings.destinations[0],
            quantity=self.shipped,
        )

    async def on_shutdown(self) -> None:
        self.stops += 1


def make_shipments(settings: ShipmentSettings, runtime: ServiceConfig) -> Shipments:
    return Shipments(settings, runtime)


def shipment_template(**overrides) -> ServiceTemplate[ShipmentSettings]:
    return ServiceTemplate(
        **{
            "name": "shipments",
            "revision": "warehouse-a",
            "service_class": Shipments,
            "settings_model": ShipmentSettings,
            "factory": make_shipments,
            **overrides,
        }
    )


def shipment_client_class() -> type[ServiceClient]:
    namespace = {"__name__": "generated_shipments_client"}
    exec(
        compile(emit(describe(Shipments), namespace="template"), "shipments_client.py", "exec"),
        namespace,
    )
    return namespace["ShipmentsClient"]
