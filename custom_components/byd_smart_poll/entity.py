"""Base entity, on a service device of its own named after the car.

Home Assistant keeps devices separate per integration, so these cannot sit on the BYD
integration's own device for the car.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .controller import SmartPollController


class SmartPollEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: SmartPollController, key: str) -> None:
        self.controller = controller
        byd = controller.byd
        self._attr_unique_id = f"{byd.vin}_smart_poll_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, byd.vin)},
            name=f"{byd.car_name} Smart Poll",
            manufacturer="BYD Smart Poll",
            model="Polling manager",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.controller.add_listener(self.async_write_ha_state))
