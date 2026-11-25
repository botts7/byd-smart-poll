"""Find the BYD integration's entities for a car, by unique ID rather than entity ID."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from awesomeversion import AwesomeVersion
from awesomeversion.exceptions import AwesomeVersionException
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .const import BYD_DOMAIN, BYD_ENTITY_SUFFIXES, BYD_REQUIRED, MIN_BYD_VERSION

_LOGGER = logging.getLogger(__name__)

# Major, minor and patch of a version, without any prerelease part.
_RELEASE = re.compile(r"v?\d+(?:\.\d+){0,2}")


@dataclass(frozen=True)
class BydEntities:
    vin: str
    car_name: str
    telemetry_interval: str
    gps_interval: str
    vehicle_on: str
    soc: str
    polling_disabled: str | None = None
    charging: str | None = None
    battery_power: str | None = None
    tracker: str | None = None
    odometer: str | None = None
    last_updated: str | None = None
    gps_last_updated: str | None = None


class BydEntitiesError(Exception):
    """The device is not a usable BYD car. The message is a config flow error key."""


def device_vin(hass: HomeAssistant, device_id: str) -> str:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise BydEntitiesError("device_not_found")
    for domain, identifier in device.identifiers:
        if domain == BYD_DOMAIN:
            return identifier
    raise BydEntitiesError("not_byd_device")


def car_device_id(hass: HomeAssistant, vin: str | None, device_id: str) -> str:
    """The car's device ID now, found by its VIN.

    Removing BYD Vehicle and adding it again makes a new device for the same car, so the
    device ID saved at setup no longer exists. Falls back to that saved ID without a VIN.
    Raises BydEntitiesError("device_not_found") if neither finds the car.
    """
    registry = dr.async_get(hass)
    if vin:
        device = _device_by_vin(hass, registry, vin)
        if device is not None:
            return device.id
    if registry.async_get(device_id) is None:
        raise BydEntitiesError("device_not_found")
    return device_id


def _device_by_vin(hass: HomeAssistant, registry: Any, vin: str) -> Any:
    """The BYD Vehicle device for a VIN, or None.

    Newer Home Assistant looks devices up per config entry (async_get_device_by_identifier);
    async_get_device by identifiers is deprecated there and stops working in 2027.8. Older
    versions only have async_get_device.
    """
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is None:
        return registry.async_get_device(identifiers={(BYD_DOMAIN, vin)})
    for entry in hass.config_entries.async_entries(BYD_DOMAIN):
        device = lookup((BYD_DOMAIN, vin), entry.entry_id)
        if device is not None:
            return device
    return None


def version_too_old(version: str | None) -> bool:
    """Whether a BYD Vehicle version is below MIN_BYD_VERSION. Unknown or odd ones are not.

    Only the release part counts, so a beta of the minimum (0.0.88b1) is let through.
    """
    release = _RELEASE.match(version or "")
    if release is None:
        return False
    try:
        return AwesomeVersion(release.group(0)) < AwesomeVersion(MIN_BYD_VERSION)
    except AwesomeVersionException:
        return False


async def async_byd_version(hass: HomeAssistant) -> str | None:
    """The installed BYD Vehicle version, or None if it is not installed or has none."""
    try:
        integration = await async_get_integration(hass, BYD_DOMAIN)
    except IntegrationNotFound:
        return None
    return str(integration.version) if integration.version else None


def resolve(hass: HomeAssistant, device_id: str) -> BydEntities:
    """Return the car's entity IDs. Raises BydEntitiesError if a required one is missing.

    The error is "entities_disabled" when the required ones all exist but some are
    disabled, "missing_entities" when one does not exist at all.
    """
    vin = device_vin(hass, device_id)
    registry = er.async_get(hass)
    by_unique_id = {
        e.unique_id: e
        for e in er.async_entries_for_device(registry, device_id, include_disabled_entities=True)
        if e.platform == BYD_DOMAIN
    }
    found: dict[str, str] = {}
    disabled: list[str] = []
    for key, suffix in BYD_ENTITY_SUFFIXES.items():
        entry = by_unique_id.get(f"{vin}{suffix}")
        if entry is None:
            continue
        if entry.disabled_by is None:
            found[key] = entry.entity_id
        else:
            disabled.append(key)
    missing = [key for key in BYD_REQUIRED if key not in found]
    if missing:
        _LOGGER.debug("BYD entities missing or disabled for %s: %s", vin[-6:], missing)
        raise BydEntitiesError(
            "entities_disabled" if all(k in disabled for k in missing) else "missing_entities"
        )
    return BydEntities(vin=vin, car_name=device_name(hass, device_id) or f"BYD {vin[-6:]}", **found)


def interval_entities(hass: HomeAssistant, vin: str) -> tuple[str, str] | None:
    """The car's telemetry and GPS poll interval numbers, or None if either is gone.

    Found on their own by unique ID, so a disabled sensor that resolve() needs, or a BYD
    device made again, does not stop the original intervals going back when Smart Poll is
    removed.
    """
    registry = er.async_get(hass)
    found = [
        registry.async_get_entity_id("number", BYD_DOMAIN, f"{vin}{BYD_ENTITY_SUFFIXES[key]}")
        for key in ("telemetry_interval", "gps_interval")
    ]
    telemetry, gps = found
    if telemetry is None or gps is None:
        return None
    return telemetry, gps


def byd_entity_ids(hass: HomeAssistant, domain: str) -> list[str]:
    """Every BYD Vehicle entity in a domain, for any car.

    Left out of the charger pickers: the car's own sensors only change when it is polled,
    and its battery power is not a charger.
    """
    return sorted(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == BYD_DOMAIN and e.domain == domain
    )


def device_name(hass: HomeAssistant, device_id: str) -> str | None:
    device = dr.async_get(hass).async_get(device_id)
    return (device.name_by_user or device.name) if device else None


def enable_vehicle_on(hass: HomeAssistant, device_id: str) -> bool:
    """Enable the BYD "Vehicle on" sensor, which the BYD integration disables by default.

    Returns True if it was disabled. Home Assistant reloads the BYD integration about 30 s
    later, after which the entity exists.
    """
    vin = device_vin(hass, device_id)
    registry = er.async_get(hass)
    unique_id = f"{vin}{BYD_ENTITY_SUFFIXES['vehicle_on']}"
    entity_id = registry.async_get_entity_id("binary_sensor", BYD_DOMAIN, unique_id)
    if entity_id is None:
        return False
    entry = registry.async_get(entity_id)
    if entry is None or entry.disabled_by is None:
        return False
    registry.async_update_entity(entity_id, disabled_by=None)
    _LOGGER.info("Enabled %s, needed to tell when the car is on", entity_id)
    return True
