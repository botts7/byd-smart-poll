"""Sensors: polling mode, estimated SoC while charging, learned kWh per percent."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SmartPollConfigEntry
from .entity import SmartPollEntity
from .modes import Mode

OFF = "off"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPollConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    controller = entry.runtime_data
    async_add_entities(
        [
            ModeSensor(controller, "mode"),
            EstimatedSocSensor(controller, "estimated_soc"),
            KwhPerPctSensor(controller, "kwh_per_pct"),
            PollsSensor(controller, "polls_24h"),
            AwakeShareSensor(controller, "parked_awake_share"),
            ParkedDrainSensor(controller, "parked_drain"),
        ]
    )


class ModeSensor(SmartPollEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [*(m.value for m in Mode), OFF]

    @property
    def native_value(self) -> str | None:
        if not self.controller.enabled:
            return OFF
        return self.controller.mode.value if self.controller.mode else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tracker = self.controller.tracker
        missed = self.controller.last_missed_drive
        attrs: dict[str, Any] = {
            "reason": self.controller.reason,
            "at_home": self.controller.home,
            "last_missed_drive": missed.isoformat() if missed else None,
            "signals_in_use": self.controller.signals_in_use(),
        }
        if self.controller.mode is Mode.CHARGING:
            due = self.controller.seconds_to_next_charge_poll()
            attrs["charge_energy_kwh"] = round(tracker.energy, 3)
            attrs["charge_power_kw"] = round(tracker.power_kw, 2)
            attrs["next_poll_in_s"] = None if due is None else round(due)
        return attrs


class EstimatedSocSensor(SmartPollEntity, SensorEntity):
    """The car's SoC, filled in between readings from the energy added while charging."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        value = self.controller.estimated_soc()
        return None if value is None else round(value, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.controller.mode is not Mode.CHARGING:
            source = "car"
        elif self.controller.data_stale:
            source = "estimate, car not answering"
        else:
            source = "estimate"
        return {"source": source}


class KwhPerPctSensor(SmartPollEntity, SensorEntity):
    """kWh per percent in use for the battery level now, with both bands as attributes."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    @property
    def native_value(self) -> float:
        return round(self.controller.kwh_per_pct, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        bands = self.controller.kwh_per_pct_bands
        return {
            "below_band_edge": round(bands.below, 4),
            "above_band_edge": round(bands.above, 4),
            "band_edge": bands.edge,
        }


class PollsSensor(SmartPollEntity, SensorEntity):
    """Readings that reached the car in the last 24 h, including the BYD integration's own."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "polls"

    @property
    def native_value(self) -> int:
        return self.controller.polls_24h()


class AwakeShareSensor(SmartPollEntity, SensorEntity):
    """Share of parked time in the last 24 h the car was found drawing power (see DrainMeter)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        share = self.controller.awake_share()
        return None if share is None else round(share * 100, 1)


class ParkedDrainSensor(SmartPollEntity, SensorEntity):
    """SoC lost per day while parked and not charging, over the last 72 h."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%/d"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        drain = self.controller.parked_drain_per_day()
        return None if drain is None else round(drain, 2)
