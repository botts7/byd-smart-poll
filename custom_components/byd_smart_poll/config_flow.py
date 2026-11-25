"""Config and options flow for BYD Smart Poll.

Setup asks one question per step: the car, how it charges at home, who drives it, and (with
phone sensors) which paired Bluetooth device is the car. The options flow repeats the last
three and adds the poll intervals.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from . import effective_options
from .byd_entities import (
    BydEntitiesError,
    async_byd_version,
    byd_entity_ids,
    device_name,
    device_vin,
    enable_vehicle_on,
    resolve,
    version_too_old,
)
from .charge_tracker import (
    DEFAULT_BAND_EDGE,
    MAX_BAND_EDGE,
    MAX_BATTERY_KWH,
    MAX_KWH_PER_PCT,
    MIN_BAND_EDGE,
    MIN_BATTERY_KWH,
    MIN_KWH_PER_PCT,
)
from .const import (
    BYD_DOMAIN,
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
    MAX_INTERVAL,
    MIN_BYD_VERSION,
    MIN_INTERVAL,
)
from .modes import Settings
from .signals import likely_car, paired_devices

# Option ranges. Below 50 W is a car plugged in but idle; even a slow 8 A charger on 120 V
# draws about 1 kW, so above 2 kW a slow charge would not count. A stop delay of 0 counts
# any dip as the end; an hour covers long solar or tariff pauses. A wake window past an hour
# of driving-rate polls costs more battery than a missed drive start.
THRESHOLD_MIN_W = 50
THRESHOLD_MAX_W = 2000
MAX_STOP_DELAY = 3600
MAX_WAKE_WINDOW_MIN = 60


def _seconds() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_INTERVAL,
            max=MAX_INTERVAL,
            step=1,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _number(low: float, high: float, step: float, unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=low,
            max=high,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _battery_kwh() -> selector.NumberSelector:
    return _number(MIN_BATTERY_KWH, MAX_BATTERY_KWH, 0.1, "kWh")


def _optional(key: str, value: Any) -> vol.Optional:
    return vol.Optional(key, description={"suggested_value": value} if value else None)


def _take(values: dict[str, Any], user_input: dict[str, Any], keys: Iterable[str]) -> None:
    """Copy one step's answers. A field cleared in the form is left out of the input."""
    for key in keys:
        if user_input.get(key):
            values[key] = user_input[key]
        else:
            values.pop(key, None)


_POWER_UNITS = {unit.value for unit in UnitOfPower}


def _looks_like_power(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether a sensor reads power (W, kW and so on), from its state or registry entry.

    A sensor that cannot be told (unavailable with no unit, say) is let through.
    """
    state = hass.states.get(entity_id)
    entry = er.async_get(hass).async_get(entity_id)
    attributes = state.attributes if state is not None else {}
    unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT) or (
        entry.unit_of_measurement if entry else None
    )
    if unit:
        return unit in _POWER_UNITS
    device_class = attributes.get(ATTR_DEVICE_CLASS) or (
        (entry.device_class or entry.original_device_class) if entry else None
    )
    return device_class in (None, SensorDeviceClass.POWER)


def _charger_power_choices(hass: HomeAssistant, current: str | None) -> list[str]:
    """Sensors for the charger power picker: ones that read power or cannot be told.

    The power device class alone would leave out template and charger sensors that have
    none, so the unit is looked at instead. BYD Vehicle's own are left out, the saved one kept.
    """
    registry = er.async_get(hass)
    found = set(hass.states.async_entity_ids("sensor")) | {
        entry.entity_id
        for entry in registry.entities.values()
        if entry.domain == "sensor" and not entry.disabled_by
    }
    found -= set(byd_entity_ids(hass, "sensor"))
    choices = {entity_id for entity_id in found if _looks_like_power(hass, entity_id)}
    if current:
        choices.add(current)
    return sorted(choices)


def _charging_errors(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    power = user_input.get(CONF_CHARGER_POWER)
    if power and not _looks_like_power(hass, power):
        errors[CONF_CHARGER_POWER] = "not_power"
    if user_input.get(CONF_NO_HOME_CHARGER) and (power or user_input.get(CONF_CHARGER_CONNECTED)):
        errors["base"] = "home_charger_and_sensors"
    return errors


class _SignalSteps:
    """The charging, drivers and car Bluetooth steps, shared by setup and options."""

    hass: HomeAssistant
    _values: dict[str, Any]
    _device_id: str

    if TYPE_CHECKING:

        def async_show_form(self, **kwargs: Any) -> ConfigFlowResult: ...

    async def _async_signals_done(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_charging(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        keys = (CONF_CHARGER_POWER, CONF_CHARGER_CONNECTED, CONF_NO_HOME_CHARGER)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _charging_errors(self.hass, user_input)
            if not errors:
                _take(self._values, user_input, keys)
                return await self.async_step_drivers()
            # Shown again with what was entered, not what was saved before.
            self._values = {
                **{k: v for k, v in self._values.items() if k not in keys},
                **{k: v for k, v in user_input.items() if k in keys and v},
            }
        choices = _charger_power_choices(self.hass, self._values.get(CONF_CHARGER_POWER))
        power_picker = (
            selector.EntitySelectorConfig(domain="sensor", include_entities=choices)
            # Nothing found (no states yet, say): every sensor but BYD's own.
            if choices
            else selector.EntitySelectorConfig(
                domain="sensor", exclude_entities=byd_entity_ids(self.hass, "sensor")
            )
        )
        schema = vol.Schema(
            {
                _optional(CONF_CHARGER_POWER, self._values.get(CONF_CHARGER_POWER)): (
                    selector.EntitySelector(power_picker)
                ),
                _optional(CONF_CHARGER_CONNECTED, self._values.get(CONF_CHARGER_CONNECTED)): (
                    selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="binary_sensor",
                            exclude_entities=byd_entity_ids(self.hass, "binary_sensor"),
                        )
                    )
                ),
                vol.Optional(
                    CONF_NO_HOME_CHARGER, default=bool(self._values.get(CONF_NO_HOME_CHARGER))
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="charging", data_schema=schema, errors=errors)

    async def async_step_drivers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        keys = (CONF_PHONE_BLUETOOTH, CONF_WAKE_HINTS)
        if user_input is not None:
            _take(self._values, user_input, keys)
            if self._values.get(CONF_PHONE_BLUETOOTH):
                return await self.async_step_car_bluetooth()
            # Only used with phone sensors, so it would be a hidden leftover.
            self._values.pop(CONF_CAR_BLUETOOTH, None)
            return await self._async_signals_done()
        schema = vol.Schema(
            {
                _optional(CONF_PHONE_BLUETOOTH, self._values.get(CONF_PHONE_BLUETOOTH)): (
                    selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", multiple=True)
                    )
                ),
                _optional(CONF_WAKE_HINTS, self._values.get(CONF_WAKE_HINTS)): (
                    selector.EntitySelector(selector.EntitySelectorConfig(multiple=True))
                ),
            }
        )
        return self.async_show_form(step_id="drivers", data_schema=schema)

    async def async_step_car_bluetooth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            _take(self._values, user_input, (CONF_CAR_BLUETOOTH,))
            return await self._async_signals_done()

        current = self._values.get(CONF_CAR_BLUETOOTH)
        devices = paired_devices(
            state.attributes.get("paired_devices") if state else None
            for state in map(self.hass.states.get, self._values[CONF_PHONE_BLUETOOTH])
        )
        if not devices:
            # The phones have not reported any (sensor off, or never connected to the car).
            return self.async_show_form(
                step_id="car_bluetooth_manual",
                data_schema=vol.Schema(
                    {_optional(CONF_CAR_BLUETOOTH, current): selector.TextSelector()}
                ),
            )
        if not current:
            car = likely_car(devices, device_name(self.hass, self._device_id))
            current = car.mac if car else None
        options = [selector.SelectOptionDict(value=d.mac, label=d.label) for d in devices]
        schema = vol.Schema(
            {
                _optional(CONF_CAR_BLUETOOTH, current): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="car_bluetooth", data_schema=schema)

    async def async_step_car_bluetooth_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_car_bluetooth(user_input)


class SmartPollConfigFlow(_SignalSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1
    # See async_migrate_entry.
    MINOR_VERSION = ENTRY_MINOR_VERSION

    _vin: str

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        version = await async_byd_version(self.hass)
        if version_too_old(version):
            return self.async_abort(
                reason="byd_too_old",
                description_placeholders={"version": str(version), "minimum": MIN_BYD_VERSION},
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            try:
                vin = device_vin(self.hass, device_id)
                await self.async_set_unique_id(vin)
                self._abort_if_unique_id_configured()
                try:
                    resolve(self.hass, device_id)
                except BydEntitiesError:
                    # The usual cause: "Vehicle on" is disabled by default in the BYD
                    # integration. Enabling it reloads that integration, after which setup
                    # here retries on its own.
                    if not enable_vehicle_on(self.hass, device_id):
                        raise
                self._vin = vin
                self._device_id = device_id
                self._values = {CONF_DEVICE_ID: device_id}
                _take(self._values, user_input, (CONF_BATTERY_KWH,))
                return await self.async_step_charging()
            except BydEntitiesError as err:
                errors["base"] = str(err)

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): selector.DeviceSelector(
                    selector.DeviceSelectorConfig(integration=BYD_DOMAIN)
                ),
                vol.Optional(CONF_BATTERY_KWH): _battery_kwh(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_signals_done(self) -> ConfigFlowResult:
        return self.async_create_entry(title=f"BYD Smart Poll {self._vin[-6:]}", data=self._values)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SmartPollOptionsFlow()


class SmartPollOptionsFlow(_SignalSteps, OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        self._device_id = entry.data[CONF_DEVICE_ID]
        # Setup data until the options were saved once: not its device or the marker for an
        # old part of the car's name (kept in the entry data only, see _forget_partial).
        self._values = {
            key: value
            for key, value in effective_options(entry).items()
            if key not in (CONF_DEVICE_ID, CONF_CAR_BLUETOOTH_PARTIAL)
        }
        return await self.async_step_charging()

    async def _async_signals_done(self) -> ConfigFlowResult:
        return await self.async_step_intervals()

    async def async_step_intervals(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        optional = (CONF_BATTERY_KWH, CONF_KWH_PER_PCT)
        if user_input is not None:
            self._values.update(user_input)
            # Cleared in the form: left out of the input.
            _take(self._values, user_input, optional)
            return self.async_create_entry(data=self._values)

        current = self._values
        defaults = Settings()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PARKED_TELEMETRY,
                    default=current.get(CONF_PARKED_TELEMETRY, defaults.parked_telemetry),
                ): _seconds(),
                vol.Required(
                    CONF_PARKED_HOME_TELEMETRY,
                    default=current.get(CONF_PARKED_HOME_TELEMETRY, defaults.parked_home_telemetry),
                ): _seconds(),
                vol.Required(
                    CONF_PARKED_GPS, default=current.get(CONF_PARKED_GPS, defaults.parked_gps)
                ): _seconds(),
                vol.Required(
                    CONF_DRIVING_TELEMETRY,
                    default=current.get(CONF_DRIVING_TELEMETRY, defaults.driving_telemetry),
                ): _seconds(),
                vol.Required(
                    CONF_DRIVING_GPS, default=current.get(CONF_DRIVING_GPS, defaults.driving_gps)
                ): _seconds(),
                vol.Required(
                    CONF_CHARGING_SAFETY,
                    default=current.get(CONF_CHARGING_SAFETY, defaults.charging_safety),
                ): _seconds(),
                _optional(CONF_BATTERY_KWH, current.get(CONF_BATTERY_KWH)): _battery_kwh(),
                _optional(CONF_KWH_PER_PCT, current.get(CONF_KWH_PER_PCT)): _number(
                    MIN_KWH_PER_PCT, MAX_KWH_PER_PCT, 0.01, "kWh"
                ),
                vol.Required(
                    CONF_KWH_BAND_EDGE,
                    default=current.get(CONF_KWH_BAND_EDGE, DEFAULT_BAND_EDGE),
                ): _number(MIN_BAND_EDGE, MAX_BAND_EDGE, 1, "%"),
                vol.Required(
                    CONF_CHARGING_MIN_POWER,
                    default=current.get(CONF_CHARGING_MIN_POWER, defaults.charging_min_power_w),
                ): _number(THRESHOLD_MIN_W, THRESHOLD_MAX_W, 10, "W"),
                vol.Required(
                    CONF_CHARGING_STOP_DELAY,
                    default=current.get(CONF_CHARGING_STOP_DELAY, defaults.charging_stop_delay),
                ): _number(0, MAX_STOP_DELAY, 1, "s"),
                vol.Required(
                    CONF_WAKE_WINDOW,
                    default=current.get(CONF_WAKE_WINDOW, defaults.wake_window // 60),
                ): _number(1, MAX_WAKE_WINDOW_MIN, 1, "min"),
            }
        )
        return self.async_show_form(step_id="intervals", data_schema=schema)
