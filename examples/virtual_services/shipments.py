"""One fixed shipment contract, configured independently for each batch."""

from pydantic import BaseModel, ConfigDict, Field

from cliffracer import CliffracerService, Output, ServiceConfig, rpc
from cliffracer.runners import ServiceTemplate


class ShipmentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse: str
    batch: str


class Parcel(BaseModel):
    sku: str
    quantity: int = Field(gt=0)


class Receipt(BaseModel):
    warehouse: str
    total: int


class Shipments(CliffracerService):
    progress = Output(Receipt, "shipments.progress.{batch}", settings=("batch",))

    def __init__(self, settings: ShipmentSettings, runtime: ServiceConfig):
        super().__init__(runtime)
        self.settings = settings
        self.total = 0

    @rpc
    async def ship(self, parcel: Parcel) -> Receipt:
        self.total += parcel.quantity
        receipt = Receipt(warehouse=self.settings.warehouse, total=self.total)
        await self.progress.publish(receipt)
        return receipt


def template() -> ServiceTemplate[ShipmentSettings]:
    return ServiceTemplate(
        name="shipments",
        revision="warehouse-a",
        service_class=Shipments,
        settings_model=ShipmentSettings,
        factory=Shipments,
        startup_timeout=5,
        cleanup_timeout=2,
        max_rpc_concurrency=4,
    )
