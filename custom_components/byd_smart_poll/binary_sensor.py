"""Binary sensor: the car's data is stale (it, BYD or the internet is not answering)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SmartPollConfigEntry
from .entity import SmartPollEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartPollConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CarDataStaleSensor(entry.runtime_data, "car_data_stale")])


class CarDataStaleSensor(SmartPollEntity, BinarySensorEntity):
    """On after two of Smart Poll's own polls in a row bring back the car's previous reading.

    Not counted while the car is parked and not charging (a sleeping car can answer with its
    last reading then).
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.controller.data_stale

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.controller.byd.last_updated
        state = self.controller.hass.states.get(last) if last else None
        return {
            "stale_refreshes": self.controller.stale_refreshes,
            "last_reading": state.state if state is not None else None,
        }
