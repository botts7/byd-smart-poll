"""BYD Smart Poll: polls a BYD car only when it is worth it.

Works alongside the BYD Vehicle integration (byd_vehicle) by setting its poll intervals and
asking it for polls. See controller.py.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .byd_entities import (
    BydEntitiesError,
    async_byd_version,
    car_device_id,
    device_vin,
    interval_entities,
    resolve,
    version_too_old,
)
from .charge_tracker import (
    DEFAULT_BAND_EDGE,
    DEFAULT_KWH_PER_PCT,
    kwh_per_pct_override,
    seed_kwh_per_pct,
)
from .const import (
    CONF_BATTERY_KWH,
    CONF_CAR_BLUETOOTH,
    CONF_CAR_BLUETOOTH_PARTIAL,
    CONF_CHARGER_CONNECTED,
    CONF_CHARGER_POWER,
    CONF_CHARGING_MIN_POWER,
    CONF_CHARGING_SAFETY,
    CONF_CHARGING_STOP_DELAY,
    CONF_DEVICE_ID,
    CONF_DRIVING_GPS,
    CONF_DRIVING_TELEMETRY,
    CONF_KWH_BAND_EDGE,
    CONF_KWH_PER_PCT,
    CONF_NO_HOME_CHARGER,
    CONF_PARKED_GPS,
    CONF_PARKED_HOME_TELEMETRY,
    CONF_PARKED_TELEMETRY,
    CONF_PHONE_BLUETOOTH,
    CONF_WAKE_HINTS,
    CONF_WAKE_WINDOW,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    MIN_BYD_VERSION,
)
from .controller import STORE_VERSION, SmartPollController
from .issues import async_delete_entry_issues, async_setup_failed, async_setup_succeeded
from .modes import BYD_DEFAULTS, Intervals, Settings
from .removal import async_restore_intervals, cancel_restore_retry
from .signals import device_for_typed_name, is_mac, paired_devices

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

type SmartPollConfigEntry = ConfigEntry[SmartPollController]


def settings_from_options(options: dict[str, Any]) -> Settings:
    defaults = Settings()
    return Settings(
        parked_telemetry=int(options.get(CONF_PARKED_TELEMETRY, defaults.parked_telemetry)),
        parked_home_telemetry=int(
            options.get(CONF_PARKED_HOME_TELEMETRY, defaults.parked_home_telemetry)
        ),
        parked_gps=int(options.get(CONF_PARKED_GPS, defaults.parked_gps)),
        driving_telemetry=int(options.get(CONF_DRIVING_TELEMETRY, defaults.driving_telemetry)),
        driving_gps=int(options.get(CONF_DRIVING_GPS, defaults.driving_gps)),
        charging_safety=int(options.get(CONF_CHARGING_SAFETY, defaults.charging_safety)),
        charging_min_power_w=float(
            options.get(CONF_CHARGING_MIN_POWER, defaults.charging_min_power_w)
        ),
        charging_stop_delay=int(
            options.get(CONF_CHARGING_STOP_DELAY, defaults.charging_stop_delay)
        ),
        # Minutes in the options.
        wake_window=round(float(options.get(CONF_WAKE_WINDOW, defaults.wake_window / 60)) * 60),
    )


def energy_options(options: dict[str, Any]) -> tuple[float, float]:
    """The kWh per percent to start learning from, and the band edge (see charge_tracker).

    The "energy per 1%" field overrides the battery size, which overrides the default.
    """
    return (
        seed_kwh_per_pct(options.get(CONF_KWH_PER_PCT), options.get(CONF_BATTERY_KWH)),
        float(options.get(CONF_KWH_BAND_EDGE, DEFAULT_BAND_EDGE)),
    )


def migrate_options(options: dict[str, Any], minor_version: int) -> dict[str, Any]:
    """Options from an older entry, in today's form.

    Before 1.3 the options form always saved "energy per 1%", with 0.6 when left alone. That
    is now an override of the battery size, so the untouched default is dropped, or a battery
    size entered later would do nothing. Any other value was typed in and stays.
    """
    options = dict(options)
    if minor_version < 3 and options.get(CONF_KWH_PER_PCT) == DEFAULT_KWH_PER_PCT:
        del options[CONF_KWH_PER_PCT]
    return options


def migrate_data(data: dict[str, Any], car_bluetooth: str, minor_version: int) -> dict[str, Any]:
    """Entry data from an older entry, in today's form.

    Before 1.3 a part of the car's Bluetooth name matched, like "BYD SEALION" for a
    "BYD SEALION 7". Such a name is marked so it keeps matching that way until setup can turn
    it into the car's MAC (see upgrade_car_bluetooth).
    """
    data = dict(data)
    car = car_bluetooth.strip()
    if minor_version < 3 and car and not is_mac(car):
        data[CONF_CAR_BLUETOOTH_PARTIAL] = car
    return data


def effective_options(entry: ConfigEntry) -> dict[str, Any]:
    """Setup data until the options have been saved once, then the options alone.

    The options form shows every field, so a cleared charger there must stay cleared.
    """
    return dict(entry.options) if entry.options else dict(entry.data)


def entry_vin(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The car's VIN: the entry's unique ID (set by the config flow), else from its device."""
    if entry.unique_id:
        return entry.unique_id
    try:
        return device_vin(hass, entry.data[CONF_DEVICE_ID])
    except BydEntitiesError:
        return None


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Brings an entry saved by an older Smart Poll up to today's form (1.1 or 1.2 to 1.3).

    1.2 was only used by two unreleased betas, each with one of the two changes, so both are
    made for it as well. For the released 1.1 each change is made once. On a 1.2 beta entry
    the other change is not always a no-op: a 0.6 typed on purpose on the charge-model beta
    is dropped (the seed then comes from the battery size, which restarts the learned bands),
    and a whole name typed on the setup beta is marked as a part again until the phones list
    their paired devices. Accepted for beta installs only.
    """
    if entry.version > 1:
        # Saved by a newer Smart Poll.
        return False
    if entry.minor_version < ENTRY_MINOR_VERSION:
        car = str(effective_options(entry).get(CONF_CAR_BLUETOOTH) or "")
        hass.config_entries.async_update_entry(
            entry,
            data=migrate_data(dict(entry.data), car, entry.minor_version),
            options=migrate_options(dict(entry.options), entry.minor_version),
            minor_version=ENTRY_MINOR_VERSION,
        )
        _LOGGER.info("Migrated the settings to version 1.%s", ENTRY_MINOR_VERSION)
    return True


def upgrade_car_bluetooth(
    hass: HomeAssistant, entry: ConfigEntry, options: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """The options with an old part of the car's name saved as the car's MAC, and whether
    the name still has to match as a part.

    Only for a name marked by async_migrate_entry. It becomes the MAC of the only paired device
    whose name holds it. It keeps matching as a part while the phones have not listed what
    they are paired with yet (often just after a restart) or when it fits two devices.
    """
    saved = str(entry.data.get(CONF_CAR_BLUETOOTH_PARTIAL) or "")
    if not saved:
        return options, False
    car = str(options.get(CONF_CAR_BLUETOOTH) or "").strip()
    phones = options.get(CONF_PHONE_BLUETOOTH) or []
    if car.casefold() != saved.casefold() or not phones:
        # Changed in the options since, or not used without phones: whole names from now on.
        _forget_partial(hass, entry, options)
        return options, False
    devices = paired_devices(
        (
            state.attributes.get("paired_devices") if state else None
            for state in map(hass.states.get, phones)
        ),
        every_name=True,
    )
    if not devices:
        # The phones have not said yet what they are paired with. Tried again next setup.
        return options, True
    device = device_for_typed_name(car, devices)
    if device is None:
        if any(car.casefold() in d.name.casefold() for d in devices):
            _LOGGER.debug("Car Bluetooth %r fits more than one paired device, left as is", car)
            return options, True
        # No paired device holds it, so matching a part would not catch anything either.
        _LOGGER.warning(
            "Car Bluetooth name %r is not part of the name of any device the phones are "
            "paired with, so drives are not caught from the phones. Pick the car in the options",
            car,
        )
        _forget_partial(hass, entry, options)
        return options, False
    if device.name.casefold() != car.casefold():
        _LOGGER.info("Car Bluetooth %r is part of %s, saving its MAC instead", car, device.label)
        options = {**options, CONF_CAR_BLUETOOTH: device.mac}
    _forget_partial(hass, entry, options)
    return options, False


def _forget_partial(hass: HomeAssistant, entry: ConfigEntry, options: dict[str, Any]) -> None:
    """Saves the options where effective_options reads them, without the old-name mark."""
    data = {k: v for k, v in entry.data.items() if k != CONF_CAR_BLUETOOTH_PARTIAL}
    if entry.options:
        hass.config_entries.async_update_entry(entry, data=data, options=options)
    else:
        if CONF_CAR_BLUETOOTH in options:
            data[CONF_CAR_BLUETOOTH] = options[CONF_CAR_BLUETOOTH]
        hass.config_entries.async_update_entry(entry, data=data)


async def async_setup_entry(hass: HomeAssistant, entry: SmartPollConfigEntry) -> bool:
    version = await async_byd_version(hass)
    if version_too_old(version):
        # A not-ready issue from earlier tries would point at the wrong cause.
        async_setup_succeeded(hass, entry.entry_id)
        # Retrying cannot help: updating BYD Vehicle needs a restart, which sets this up again.
        raise ConfigEntryError(
            f"Needs BYD Vehicle {MIN_BYD_VERSION} or newer, found {version}",
            translation_domain=DOMAIN,
            translation_key="byd_too_old",
            translation_placeholders={"version": str(version), "minimum": MIN_BYD_VERSION},
        )
    vin = entry_vin(hass, entry)
    # Removed and added again before a restart: the old entry's restore retry must not
    # write the original intervals over this one's.
    if vin:
        cancel_restore_retry(hass, vin)
    try:
        # Found by VIN, so BYD Vehicle removed and added again (a new device) still works.
        device_id = car_device_id(hass, vin, entry.data[CONF_DEVICE_ID])
        byd = resolve(hass, device_id)
    except BydEntitiesError as err:
        # The BYD integration may still be loading, or reloading after an entity was enabled.
        # After a few tries in a row, Repairs says what is wrong.
        async_setup_failed(hass, entry.entry_id, entry.title, str(err))
        raise ConfigEntryNotReady(f"BYD entities not available yet ({err})") from err
    async_setup_succeeded(hass, entry.entry_id)
    if device_id != entry.data[CONF_DEVICE_ID]:
        _LOGGER.info("Car found under a new BYD Vehicle device, updating the saved one")
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_ID: device_id}
        )

    options, car_bluetooth_partial = upgrade_car_bluetooth(hass, entry, effective_options(entry))
    kwh_per_pct, band_edge = energy_options(options)
    controller = SmartPollController(
        hass,
        entry.entry_id,
        byd,
        settings_from_options(options),
        charger_power=options.get(CONF_CHARGER_POWER) or None,
        charger_connected=options.get(CONF_CHARGER_CONNECTED) or None,
        wake_hints=list(options.get(CONF_WAKE_HINTS) or []),
        car_bluetooth=options.get(CONF_CAR_BLUETOOTH) or None,
        phone_bluetooth=list(options.get(CONF_PHONE_BLUETOOTH) or []),
        no_home_charger=bool(options.get(CONF_NO_HOME_CHARGER)),
        car_bluetooth_partial=car_bluetooth_partial,
        kwh_per_pct=kwh_per_pct,
        band_edge=band_edge,
        kwh_per_pct_override=kwh_per_pct_override(options.get(CONF_KWH_PER_PCT)),
    )
    entry.runtime_data = controller
    # Load the stored state first, so the entities show it from the start (not the defaults).
    await controller.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartPollConfigEntry) -> bool:
    # Disabling the entry leaves nothing to drive the intervals, so they go back as on removal.
    disabled = entry.disabled_by is not None
    await entry.runtime_data.async_stop(restore=disabled)
    if disabled:
        # Nothing checks them while it is disabled. A reload keeps them, so an ignored issue
        # stays ignored.
        async_delete_entry_issues(hass, entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: SmartPollConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Put the BYD poll intervals back to what they were before, and clear our Repairs issues."""
    async_delete_entry_issues(hass, entry.entry_id)
    store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"byd_smart_poll.{entry.entry_id}")
    stored = await store.async_load() or {}
    originals = stored.get("originals")
    restore = Intervals(originals["telemetry"], originals["gps"]) if originals else BYD_DEFAULTS
    vin = entry_vin(hass, entry)
    numbers = interval_entities(hass, vin) if vin else None
    if vin is None or numbers is None:
        _LOGGER.warning(
            "Could not put the BYD poll intervals back: the car or its poll interval numbers "
            "are gone. If you set BYD Vehicle up again, set them to %s s and %s s by hand",
            restore.telemetry,
            restore.gps,
        )
    else:
        # Retries on its own while Home Assistant runs, so the saved originals are not needed
        # after this: nothing reads this entry's store again.
        await async_restore_intervals(hass, numbers, restore, key=vin)
    await store.async_remove()
