"""Shipment progress contracts with batch and per-order routing values."""

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cliffracer import CliffracerService, Output, ServiceConfig, rpc
from cliffracer.client import ServiceClient
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import describe
from cliffracer.runners import ServiceTemplate


class BatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse: str
    batch: str = Field(default_factory=lambda: os.environ.get("SHIPMENT_BATCH", "north"))
    internal_note: str = "warehouse-access-canary"


class ShipmentProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(gt=0)
    stage: Literal["packed", "sent"]


class BatchSummary(BaseModel):
    shipped: int = Field(ge=0)


class ShipmentWorker(CliffracerService):
    progress = Output(
        ShipmentProgress,
        "batches.{batch}.orders.{order_id}.progress",
        settings=("batch",),
        parameters=("order_id",),
    )
    summary = Output(BatchSummary, "batches.{batch}.closed", settings=("batch",))

    def __init__(self, settings: BatchSettings, runtime: ServiceConfig):
        super().__init__(runtime)
        self.settings = settings
        self.shipped = 0

    @rpc
    async def ship(self, order_id: str, quantity: int) -> int:
        await self.progress.publish(
            ShipmentProgress(quantity=quantity, stage="sent"),
            parameters={"order_id": order_id},
        )
        self.shipped += quantity
        return self.shipped

    @rpc
    async def finish(self) -> int:
        await self.summary.publish(BatchSummary(shipped=self.shipped))
        return self.shipped


def shipment_output_template(**overrides) -> ServiceTemplate[BatchSettings]:
    return ServiceTemplate(
        **{
            "name": "shipments",
            "revision": "shipping-a",
            "service_class": ShipmentWorker,
            "settings_model": BatchSettings,
            "factory": ShipmentWorker,
            **overrides,
        }
    )


def shipment_output_client_class() -> type[ServiceClient]:
    namespace = {"__name__": "generated_shipping_client"}
    exec(
        compile(
            emit(describe(ShipmentWorker, service="shipment_worker")),
            "shipment_output_client.py",
            "exec",
        ),
        namespace,
    )
    return namespace["ShipmentWorkerClient"]
