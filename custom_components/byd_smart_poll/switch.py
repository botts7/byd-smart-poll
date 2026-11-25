"""Switch to turn smart polling on and off. Off puts the original BYD intervals back."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([EnabledSwitch(entry.runtime_data, "enabled")])


class EnabledSwitch(SmartPollEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return self.controller.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.controller.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.controller.async_set_enabled(False)
