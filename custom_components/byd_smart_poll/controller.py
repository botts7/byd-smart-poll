"""Watches the car, charger and phones, and drives the BYD integration's polling.

It never talks to the BYD cloud itself. It only:
- writes the BYD integration's two poll interval numbers when the target changes
- asks for a poll (homeassistant.update_entity on a BYD entity), which runs a normal BYD
  refresh and restarts its poll timer, so a faster rate takes effect straight away
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from . import signal_health
from .byd_entities import BydEntities
from .charge_tracker import (
    DEFAULT_BAND_EDGE,
    LEGACY_SETTING_KEY,
    OVERRIDE_KEY,
    RESUME_WINDOW,
    Bands,
    ChargeTracker,
    starting_kwh_per_pct,
    stored_kwh_per_pct,
    stored_override,
)
from .const import CHARGE_CHECK_INTERVAL, MIN_POLL_GAP
from .issues import IssueWatcher
from .modes import (
    BYD_DEFAULTS,
    MAX_WRITE_MISSES,
    Intervals,
    IntervalWrites,
    Mode,
    Settings,
    WakeWindow,
    counts_stale_polls,
    decide_mode,
    drive_ended,
    just_parked_poll_wanted,
    just_parked_polls,
    keeps_just_parked_polls,
    pick_originals,
    poll_action,
    poll_wait,
    stale_backoff,
    stale_count_restarts,
)
from .signals import (
    PRESS_DOMAINS,
    STALE_REFRESHES,
    CableCheck,
    ChargerCheck,
    ChargerSign,
    DrainMeter,
    NewestReading,
    OdometerWatch,
    Sample,
    at_home,
    car_charge_data_stale,
    car_data_changed,
    car_says_charging,
    charge_poll_due,
    charge_power_w,
    charger_unknown_hold_left,
    charger_unknown_holds,
    charging_shows_negative,
    charging_signal,
    data_stale,
    implausible_for,
    is_wake_hint,
    kept_stale_charge,
    phone_connected_to_car,
    poll_brought_nothing,
    vehicle_on_keep_left,
    vehicle_on_or_keep,
    wake_allowed,
    wake_hint_on,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
_MISSING = (None, STATE_UNAVAILABLE, STATE_UNKNOWN)
# The learned kWh per percent in the store: the one value from before the bands, then each band.
_KWH_PER_PCT_KEYS = ("kwh_per_pct", "kwh_per_pct_below", "kwh_per_pct_above")


def _number(state: State | None) -> float | None:
    if state is None or state.state in _MISSING:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _power_w(state: State | None) -> float | None:
    """A power sensor's value in W, from its unit (W, kW, MW and the rest Home Assistant knows).

    No unit, or one that is not a power unit, is taken as W, as most chargers report.
    """
    value = _number(state)
    if value is None or state is None:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit in PowerConverter.VALID_UNITS and unit != UnitOfPower.WATT:
        return PowerConverter.convert(value, unit, UnitOfPower.WATT)
    return value


def _on_off(state: State | None) -> bool | None:
    if state is None or state.state in _MISSING:
        return None
    return state.state == STATE_ON


class SmartPollController:
    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        byd: BydEntities,
        settings: Settings,
        *,
        charger_power: str | None,
        charger_connected: str | None,
        wake_hints: list[str],
        car_bluetooth: str | None,
        phone_bluetooth: list[str],
        kwh_per_pct: float,
        no_home_charger: bool = False,
        car_bluetooth_partial: bool = False,
        band_edge: float = DEFAULT_BAND_EDGE,
        kwh_per_pct_override: float | None = None,
    ) -> None:
        self.hass = hass
        self.byd = byd
        self.settings = settings
        self._charger_power = charger_power
        self._charger_connected = charger_connected
        self._wake_hints = wake_hints
        self._car_bluetooth = (car_bluetooth or "").strip()
        self._phone_bluetooth = phone_bluetooth if self._car_bluetooth else []
        # A part of the name saved before whole names were needed, until setup upgrades it.
        self._car_bluetooth_partial = car_bluetooth_partial
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"byd_smart_poll.{entry_id}"
        )
        self._stored: dict[str, Any] = {}
        # The configured kWh per percent (the setting, or from the battery size) for both bands.
        self._default_kwh_per_pct = kwh_per_pct
        # The "energy per 1%" setting typed in, or None: only a change of it restarts learning.
        self._kwh_per_pct_override = kwh_per_pct_override
        self._band_edge = band_edge
        self.tracker = ChargeTracker(Bands.single(kwh_per_pct, band_edge))
        self.meter = DrainMeter()
        self._odometer = OdometerWatch()
        self._newest = NewestReading()
        self.charger_check = ChargerCheck()
        self._charger_sign = ChargerSign()
        # Whether the charger last read below the charging threshold, and when it was last
        # seen going from charging power to idle (-inf: not since start).
        self._charger_idle: bool | None = None
        self._charger_idle_at = -math.inf
        # When the car's own charging state or battery power last changed, and when a reading
        # newer than any before arrived (not the same one back after the cloud dropped out).
        self._car_charge_data_at = -math.inf
        self._reading_at = -math.inf
        self._cable = CableCheck()

        self.enabled = True
        # Set once the entry unloads. Handlers still running then (awaiting a poll) must not
        # start timers or polls on a controller nothing will stop again.
        self._stopped = False
        # None until the first evaluation, so the first target is always applied.
        self.mode: Mode | None = None
        self.home: bool | None = None
        self.reason = "starting"
        self.last_missed_drive: datetime | None = None
        self._writes = IntervalWrites()
        # Above 0 while writing the BYD numbers, so their state changes from it are not taken
        # as someone else changing them (the write reads them back itself).
        self._writing = 0
        # Set when turning smart polling off could not put the original intervals back (the
        # numbers were unavailable), so they are written when the numbers come back.
        self._restore_pending = False
        self._charging = False
        # Whether the charger itself showed charging power when charging was last seen. Only
        # then did the charger see the stop, so a reading for the final SoC is worth a poll.
        self._charger_charged = False
        # Set while a paused charge waits to see if it resumes (see ChargeTracker.resume),
        # and whether its end then needs a poll.
        self._cancel_charge_end: Callable[[], None] | None = None
        self._end_poll_wanted = False
        self._moved_since_fix = False
        self._phone_in_car: dict[str, bool] = {}
        self._wake = WakeWindow()
        # The wake hints that started the running "expecting a drive" window.
        self._wake_hints_in = set[str]()
        self._last_poll = -math.inf
        # Charge polls that brought back no new reading, and when the next may go out.
        self._stale_charge_polls = 0
        self._charge_poll_after = -math.inf
        # BYD refreshes in a row that brought back the car's previous reading.
        self.stale_refreshes = 0
        self._fresh_at = -math.inf
        # When async_start ran, for sensors that do not exist yet just after a restart.
        self._started_utc: datetime | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._cancel_stop: Callable[[], None] | None = None
        self._cancel_wake: Callable[[], None] | None = None
        self._cancel_charge_check: Callable[[], None] | None = None
        self._cancel_deferred: Callable[[], None] | None = None
        # Whether the put off poll is only there to start a faster rate.
        self._deferred_for_rate = False
        self._cancel_just_parked: list[Callable[[], None]] = []
        self._cancel_keep: Callable[[], None] | None = None
        self._cancel_charger_hold: Callable[[], None] | None = None
        self._listeners: list[Callable[[], None]] = []
        self.issues = IssueWatcher(
            hass,
            entry_id,
            byd.car_name,
            signal_health.Configured(
                charger_power=charger_power,
                charger_connected=charger_connected,
                phone_bluetooth=tuple(self._phone_bluetooth),
                wake_hints=tuple(wake_hints),
                no_home_charger=no_home_charger,
            ),
            self._notify,
        )

    # ---------- lifecycle ----------

    async def async_start(self) -> None:
        self._started_utc = dt_util.utcnow()
        self._stored = await self._store.async_load() or {}
        self.enabled = self._stored.get("enabled", True)
        self.tracker = ChargeTracker(self._starting_kwh_per_pct())
        self.meter = DrainMeter(Sample(*s) for s in self._stored.get("samples", []))
        missed = self._stored.get("last_missed_drive")
        self.last_missed_drive = dt_util.parse_datetime(missed) if missed else None
        # Kept across restarts, for when the car's position cannot be told at startup.
        self.home = self._stored.get("home")
        # Stored as the charger's entity id: a newly picked sensor starts afresh.
        self.charger_check = ChargerCheck(
            implausible_for(self._stored.get("charger_implausible"), self._charger_power)
        )
        self.issues.charger_implausible = self.charger_check.implausible
        self._charger_sign = ChargerSign(
            implausible_for(self._stored.get("charger_negative"), self._charger_power),
            implausible_for(self._stored.get("charger_positive"), self._charger_power),
        )
        if "originals" not in self._stored:
            current = self._current_intervals()
            originals = pick_originals(current, self.settings)
            self._stored["originals"] = {"telemetry": originals.telemetry, "gps": originals.gps}
            # Saved straight away: these must survive a restart before any other save.
            await self._store.async_save(self._stored)
            _LOGGER.info(
                "Saved original BYD intervals %s s / %s s (current %s)",
                originals.telemetry,
                originals.gps,
                current,
            )

        self._odometer.update(_number(self._state(self.byd.odometer)), parked=False)
        # A cable sensor found wrong stays ignored across restarts until it next turns off.
        # Only for the same sensor: picking another one in the settings starts afresh.
        cable_wrong = self._charger_connected is not None and (
            self._stored.get("cable_implausible") == self._charger_connected
        )
        self._cable = CableCheck(implausible=cable_wrong)
        self._cable.cable(_on_off(self._state(self._charger_connected)), self._epoch())
        # The sensor may have turned off while Home Assistant was down.
        self.issues.cable_implausible = self._cable.implausible
        if not self._cable.implausible and self._stored.pop("cable_implausible", None):
            self._save()
        # Turning smart polling off could not put the originals back before the restart.
        self._restore_pending = not self.enabled and self._stored.get("restore_pending", False)
        for phone in self._phone_bluetooth:
            self._phone_in_car[phone] = self._phone_connected(self._state(phone))
        # The car's reading already there (BYD Vehicle loaded first, or a reload of ours) is
        # not news: the same one back after its entities were unavailable is not a new one.
        start_time = self._car_time(self._state(self.byd.last_updated))
        if start_time is not None:
            self._newest.note(start_time, self._reported_vehicle_on())

        b = self.byd
        watched = [
            b.vehicle_on,
            b.soc,
            b.charging,
            b.battery_power,
            b.polling_disabled,
            b.tracker,
            b.last_updated,
            b.gps_last_updated,
            self._charger_power,
            self._charger_connected,
            *self._wake_hints,
            *self._phone_bluetooth,
        ]
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [e for e in watched if e], self._async_state_changed
            )
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [b.telemetry_interval, b.gps_interval], self._async_interval_changed
            )
        )
        if b.last_updated:
            self._unsubs.append(
                async_track_state_report_event(
                    self.hass, [b.last_updated], self._async_reading_repeated
                )
            )
        self._note_charger_power()
        # The car's last reading is not known to be old yet: its age counts from now. Unless a
        # charge poll before the restart found it stale (see _keep_stale_charge).
        self._fresh_at = self._now()
        last = self._state(b.last_updated)
        kept = kept_stale_charge(
            self._stored.get("stale_charge"), last.state if last else None, self._epoch()
        )
        if kept is not None:
            age, self._stale_charge_polls = kept
            self._fresh_at = self._now() - age
        # Unknown (the charger not back yet after a restart, at home) starts not charging:
        # the charger says so within seconds once it is back.
        self._set_charging(self._charging_signal() or False)
        self._watch_charger_hold()
        if self._restore_pending and self._current_intervals() is not None:
            # Back already, so no "came back" change will come for them.
            await self.async_restore_originals()
        await self.async_evaluate("startup")
        self.issues.async_start()

    def _starting_kwh_per_pct(self) -> Bands:
        """The learned kWh per percent, or the setting if one was typed in since (see
        starting_kwh_per_pct). The battery size is only the guess before anything is learned.
        """
        configured = self._default_kwh_per_pct
        override = self._kwh_per_pct_override
        learned = stored_kwh_per_pct(self._stored)
        override_from = stored_override(self._stored)
        if learned is not None and not (
            OVERRIDE_KEY in self._stored or LEGACY_SETTING_KEY in self._stored
        ):
            # Learned by an early 0.2.0 that kept no setting: take the setting as unchanged.
            override_from = override
        below, above = starting_kwh_per_pct(learned, override_from, override, configured)
        value = Bands(below, above, self._band_edge)
        if learned is not None and (below, above) != learned:
            _LOGGER.info(
                "Energy per 1%% set to %s kWh in the settings (was %s): starting again from "
                "it instead of the learned %s / %s kWh (below / above %s%%)",
                override,
                override_from,
                learned[0],
                learned[1],
                self._band_edge,
            )
        # Also moves a store from before the two bands over to them, or the one value an
        # older version learned after a downgrade (see stored_kwh_per_pct).
        kept = tuple(self._stored.get(k) for k in _KWH_PER_PCT_KEYS)
        if learned is not None and kept != (below, below, above):
            self._store_kwh_per_pct(value)
        # The older key goes: 0.2.0 compares it with its own setting, so after a downgrade
        # its absence keeps what was learned.
        if (
            OVERRIDE_KEY not in self._stored
            or self._stored[OVERRIDE_KEY] != override
            or LEGACY_SETTING_KEY in self._stored
        ):
            self._stored[OVERRIDE_KEY] = override
            self._stored.pop(LEGACY_SETTING_KEY, None)
            self._save()
        return value

    def _store_kwh_per_pct(self, bands: Bands) -> None:
        """Keep the kWh per percent below and above the band edge for the next session.

        The one value versions before the bands read is kept too, as the lower band, so going
        back to one of them keeps what was learned.
        """
        self._stored["kwh_per_pct"] = bands.below
        self._stored["kwh_per_pct_below"] = bands.below
        self._stored["kwh_per_pct_above"] = bands.above
        self._save()

    async def async_stop(self, *, restore: bool = False) -> None:
        """Stop for an unload. `restore` puts the original intervals back first."""
        self._stopped = True
        self.issues.async_stop()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._cancel_charge_end is not None:
            # A paused charge: what it learned is kept, as when the pause runs out.
            self._finish_charge_session()
        self._cancel_mode_timers()
        for cancel in (self._cancel_wake, self._cancel_keep, self._cancel_charger_hold):
            if cancel is not None:
                cancel()
        self._cancel_wake = self._cancel_keep = self._cancel_charger_hold = None
        if restore:
            await self.async_restore_originals()
        # Saves wait 30 s, and a reload (saving the options does one) reads the file at once.
        # Writing now also drops the delayed save, so it cannot land on top later.
        await self._store.async_save(self._stored)

    @property
    def _active(self) -> bool:
        """Whether polls and timers may go out: smart polling on and the entry not unloaded."""
        return self.enabled and not self._stopped

    def _cancel_mode_timers(self) -> None:
        """Cancel the timers that poll or change the mode on their own."""
        self._cancel_stop_delay()
        self._stop_charge_check()
        self._cancel_deferred_poll()
        self._cancel_just_parked_polls()
        if self._cancel_charge_end is not None:
            self._cancel_charge_end()
            self._cancel_charge_end = None

    async def async_set_enabled(self, enabled: bool) -> None:
        if enabled == self.enabled:
            # An automation turning it on while on would otherwise start the mode afresh,
            # dropping a running charge session and polling for it.
            return
        self.enabled = enabled
        self._stored["enabled"] = enabled
        self._save()
        if enabled:
            self.mode = None
            self._set_restore_pending(False)
            self._writes.reset()
            # The stop delay was cancelled when it was turned off, so start from what the
            # signals say now.
            signal = self._charging_signal()
            if signal is not None:
                self._set_charging(signal)
            await self.async_evaluate("enabled")
        else:
            if self.mode is Mode.CHARGING or self._cancel_charge_end is not None:
                self._finish_charge_session()
            # Nothing may poll while it is off: a charge check left running polled every
            # minute, and turning it on again started a second one beside it.
            self._cancel_mode_timers()
            self.mode = None
            await self.async_restore_originals()
            self._writes.reset()
            self.reason = "disabled"
            self._notify()

    async def async_restore_originals(self) -> None:
        originals = self._stored.get("originals")
        restore = Intervals(originals["telemetry"], originals["gps"]) if originals else BYD_DEFAULTS
        _LOGGER.info("Restoring BYD intervals %s s / %s s", restore.telemetry, restore.gps)
        self._set_restore_pending(not await self._set_intervals(restore))
        if self._restore_pending:
            _LOGGER.info(
                "BYD poll intervals did not take (they show %s): restoring when they come back",
                self._current_intervals(),
            )

    def _set_restore_pending(self, pending: bool) -> None:
        """Kept across restarts, so the originals still go back if BYD returns after one."""
        self._restore_pending = pending
        if pending:
            self._stored["restore_pending"] = True
        elif not self._stored.pop("restore_pending", False):
            return
        self._save()

    # ---------- entity updates ----------

    @callback
    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    @property
    def kwh_per_pct(self) -> float:
        """kWh per percent in use now: the band of the (estimated) battery level."""
        return self.tracker.bands.at(self.estimated_soc())

    @property
    def kwh_per_pct_bands(self) -> Bands:
        return self.tracker.bands

    def estimated_soc(self) -> float | None:
        actual = _number(self._state(self.byd.soc))
        if self.mode is not Mode.CHARGING:
            return actual
        estimate = self.tracker.estimated_soc(self._now(), stale=self.data_stale)
        return estimate if estimate is not None else actual

    def seconds_to_next_charge_poll(self) -> float | None:
        if self.mode is not Mode.CHARGING:
            return None
        return self.tracker.seconds_until_due(self._now())

    def polls_24h(self) -> int:
        return self.meter.polls(self._epoch())

    def awake_share(self) -> float | None:
        # A parked reading stands for at most one parked poll interval.
        longest = max(self.settings.parked_telemetry, self.settings.parked_home_telemetry)
        return self.meter.awake_share(self._epoch(), longest)

    def parked_drain_per_day(self) -> float | None:
        return self.meter.parked_drain_per_day()

    def signals_in_use(self) -> list[str]:
        numeric = {self._charger_power, self.byd.odometer, self.byd.battery_power}

        def usable(entity_id: str | None) -> bool:
            state = self._state(entity_id)
            if state is not None and state.domain in PRESS_DOMAINS:
                # A button never pressed is unknown, and its first press still counts.
                return state.state != STATE_UNAVAILABLE
            return state is not None and signal_health.usable_state(
                state.state, numeric=entity_id in numeric
            )

        return signal_health.signals_in_use(
            self.issues.configured,
            self.issues.statuses,
            odometer=usable(self.byd.odometer),
            car_battery_power=usable(self.byd.battery_power),
            usable={e for e in self.issues.configured.roles() if usable(e)},
        )

    # ---------- signals ----------

    def _now(self) -> float:
        return self.hass.loop.time()

    @staticmethod
    def _epoch() -> float:
        return dt_util.utcnow().timestamp()

    def _car_time(self, state: State | None) -> float | None:
        """The car's own time of a BYD timestamp state, in epoch seconds."""
        if state is None or state.state in _MISSING:
            return None
        parsed = dt_util.parse_datetime(state.state)
        return parsed.timestamp() if parsed else None

    def _reported_vehicle_on(self) -> bool:
        """What the BYD sensor says, with unavailable or unknown keeping the current mode."""
        state = self._state(self.byd.vehicle_on)
        return vehicle_on_or_keep(
            _on_off(state),
            driving=self.mode is Mode.DRIVING,
            unknown_for=self._unknown_for(state),
        )

    @staticmethod
    def _unknown_for(state: State | None) -> float | None:
        """How long a state has had its current value, or None if the entity is gone."""
        return (dt_util.utcnow() - state.last_changed).total_seconds() if state else None

    @callback
    def _watch_keep_end(self) -> None:
        """While an unknown "car on" sensor keeps a drive, look again when the keep runs out."""
        if self.mode is not Mode.DRIVING or self._cancel_keep is not None:
            return
        state = self._state(self.byd.vehicle_on)
        if _on_off(state) is not None:
            return
        left = vehicle_on_keep_left(self._unknown_for(state))
        if left is not None:
            self._cancel_keep = async_call_later(self.hass, left + 1, self._async_keep_ended)

    async def _async_keep_ended(self, _now: datetime) -> None:
        self._cancel_keep = None
        await self.async_evaluate("car on sensor still unknown")

    def _vehicle_on(self) -> bool:
        """Whether the car is on, ignoring a reading older than one already seen."""
        reported = self._reported_vehicle_on()
        if not self.byd.last_updated:
            return reported
        return self._newest.vehicle_on_now(
            reported, self._car_time(self._state(self.byd.last_updated))
        )

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _polling_disabled(self) -> bool:
        return _on_off(self._state(self.byd.polling_disabled)) is True

    def _phone_connected(self, state: State | None) -> bool:
        if state is None or state.state in _MISSING:
            return False
        return phone_connected_to_car(
            state.attributes.get("connected_paired_devices"),
            self._car_bluetooth,
            partial=self._car_bluetooth_partial,
        )

    def _trusted_charger_w(self) -> float | None:
        """The charger's power, or None without one, while unavailable or not trusted."""
        if not self._charger_power or not self.charger_check.trusted:
            return None
        return self._charger_w()

    def _charger_w(self) -> float | None:
        """The charger's power in W, trusted or not, with power into the car positive."""
        return self._charger_sign.apply(_power_w(self._state(self._charger_power)))

    def _car_says_charging(self) -> bool | None:
        return car_says_charging(
            _on_off(self._state(self.byd.charging)),
            _power_w(self._state(self.byd.battery_power)),
            self._vehicle_on(),
            self.settings.charging_min_power_w,
        )

    def _charging_signal(self) -> bool | None:
        """Whether the car is charging right now, or None if unknown.

        The charger is preferred: it reports straight away without waking the car. The car's
        own sensors only change when it is polled, and count when the charger is idle (public
        charging) or not trusted (see signals.charging_signal).
        """
        return charging_signal(
            self._trusted_charger_w(),
            self.settings.charging_min_power_w,
            cable_connected=self._cable_connected(),
            car_charging=self._car_says_charging(),
            car_reading_since_idle=(
                self._charger_idle_at == -math.inf
                or max(self._reading_at, self._car_charge_data_at) > self._charger_idle_at
            ),
            home=self._at_home(),
            charger_unknown=self._charger_unknown(),
            car_data_stale=car_charge_data_stale(
                self._now() - self._fresh_at, self._stale_charge_polls
            ),
        )

    def _charger_unknown(self) -> bool:
        """A trusted charger that has had no reading for a short while (a restart, say)."""
        if self._charger_power is None or not self.charger_check.trusted:
            return False
        state = self._state(self._charger_power)
        return _power_w(state) is None and charger_unknown_holds(self._charger_unknown_for(state))

    def _charger_unknown_for(self, state: State | None) -> float | None:
        """How long the charger has had no reading, counting from our start if it has no state.

        On a restart the charger's entities (MQTT discovery, say) can be added after Smart Poll
        starts. Missing then is not "removed": it only counts as no charger once the hold from
        our start has run out.
        """
        if state is not None:
            return self._unknown_for(state)
        if self._started_utc is None:
            return None
        return (dt_util.utcnow() - self._started_utc).total_seconds()

    @callback
    def _watch_charger_hold(self) -> None:
        """While a charger with no reading holds the charging state, look again when it ends.

        Otherwise a gateway down for longer than the hold, with the car really charging, left
        it parked until the next parked poll (the charger's age counts from a restart).
        """
        if self._stopped or self._cancel_charger_hold is not None or not self._charger_unknown():
            return
        left = charger_unknown_hold_left(
            self._charger_unknown_for(self._state(self._charger_power))
        )
        if left is not None:
            self._cancel_charger_hold = async_call_later(
                self.hass, left + 1, self._async_charger_hold_ended
            )

    async def _async_charger_hold_ended(self, _now: datetime) -> None:
        self._cancel_charger_hold = None
        self._update_charging()
        await self.async_evaluate("charger still unknown")

    def _charge_power_kw(self) -> float:
        """Power going into the battery for the charge tracker (see signals.charge_power_w)."""
        return (
            charge_power_w(
                self._trusted_charger_w(),
                _power_w(self._state(self.byd.battery_power)),
                cable_connected=self._cable_connected(),
                car_says=self._car_says_charging(),
                min_power_w=self.settings.charging_min_power_w,
                charging_negative=self._stored.get("charging_negative"),
            )
            / 1000
        )

    @callback
    def _note_charger_power(self) -> None:
        """Keep track of when the charger went idle, and feed the charger check."""
        seen_positive = self._charger_sign.seen_positive
        if self._charger_sign.charger_power(
            _power_w(self._state(self._charger_power)), self.settings.charging_min_power_w
        ):
            _LOGGER.info("%s shows positive power again: no longer negated", self._charger_power)
            self._store_charger_sign()
        elif self._charger_sign.seen_positive != seen_positive:
            self._store_charger_sign()
        power = self._charger_w()
        if power is None:
            return
        idle = power < self.settings.charging_min_power_w
        # Only a charger seen going from charging power to idle. The first reading after a
        # restart (or after its entities were added late) is no news: taken as "went idle
        # now", it dropped a public charge seen in the car's data (the car's reading was
        # older than that), and the car then waited for the parked rate.
        if idle and self._charger_idle is False:
            self._charger_idle_at = self._now()
        self._charger_idle = idle
        self.charger_check.charger_power(
            self._now(),
            power,
            self.settings.charging_min_power_w,
            _number(self._state(self.byd.soc)),
        )

    @callback
    def _check_charger(self, soc: float | None, power: float | None) -> None:
        """A fresh car reading while charging: is the charger really charging this car?"""
        if self.mode is not Mode.CHARGING or not self._charger_power:
            return
        # Its own power, trusted or not: an implausible charger is trusted again once a
        # reading shows it charging this car.
        charger = self._charger_w()
        if charger is None or charger < self.settings.charging_min_power_w:
            return
        check = self.charger_check
        was_trusted, was_implausible = check.trusted, check.implausible
        was_full, was_drawing = check.full, check.drawing
        flag = _on_off(self._state(self.byd.charging))
        check.reading(
            self._now(),
            soc=soc,
            car_charging=flag,
            car_power_w=power,
            min_power_w=self.settings.charging_min_power_w,
            kwh_per_pct=self.kwh_per_pct,
            away=self._at_home() is False,
            cable_in=self._cable_connected() is True,
        )
        if check.implausible and not was_implausible:
            # Enough in the log to tell a false doubt (a car drawing from the cable with its
            # flag off, say for battery heating) from a shared charger.
            _LOGGER.warning(
                "%s shows %s W but the car says it is not charging (flag %s, battery %s W, "
                "SoC %s, %.2f kWh from the charger with no rise): ignoring it until it is seen "
                "charging this car",
                self._charger_power,
                charger,
                flag,
                power,
                soc,
                check.energy_kwh,
            )
        elif check.full and not was_full:
            _LOGGER.info(
                "%s shows %s W but the car is full and says it is not charging: ignoring it "
                "until it goes idle",
                self._charger_power,
                charger,
            )
        elif check.drawing and not was_drawing:
            _LOGGER.info(
                "%s shows %s W with the cable in but the car says it is not charging (battery "
                "%s W, SoC %s): taken as the car heating or cooling on the cable, ignoring it "
                "until it goes idle",
                self._charger_power,
                charger,
                power,
                soc,
            )
        elif was_implausible and not check.implausible:
            _LOGGER.info("%s is charging this car: using it again", self._charger_power)
        if was_trusted and not check.trusted:
            # A decision, not a solar dip, so no stop delay.
            if self._charging_signal() is not True:
                self._charging_over_now()
        stored = self._charger_power if self.charger_check.implausible else None
        if self._stored.get("charger_implausible") != stored:
            self._stored["charger_implausible"] = stored
            self._save()
        self.issues.async_set_charger_implausible(self.charger_check.implausible)

    @callback
    def _check_charger_sign(self) -> None:
        """A fresh car reading: does the charger show the power going in as negative?"""
        if not self._charger_power:
            return
        raw = _power_w(self._state(self._charger_power))
        if not self._charger_sign.car_reading(
            raw,
            car_charging=self._car_says_charging(),
            min_power_w=self.settings.charging_min_power_w,
        ):
            return
        _LOGGER.info(
            "%s shows %s W while the car says it is charging: taking its power as negated",
            self._charger_power,
            raw,
        )
        self._store_charger_sign()
        self._note_charger_power()

    def _store_charger_sign(self) -> None:
        sign = self._charger_sign
        stored = {
            "charger_negative": self._charger_power if sign.negative else None,
            "charger_positive": self._charger_power if sign.seen_positive else None,
        }
        if any(self._stored.get(key) != value for key, value in stored.items()):
            self._stored.update(stored)
            self._save()

    def _at_home(self) -> bool | None:
        tracker = self._state(self.byd.tracker)
        zone_home = (
            None if tracker is None or tracker.state in _MISSING else tracker.state == "home"
        )
        return at_home(
            cable_connected=self._cable_connected(),
            car_zone_home=zone_home,
            moved_since_fix=self._moved_since_fix,
            previous=self.home,
        )

    def _cable_connected(self) -> bool | None:
        """The cable sensor, or None while it is not believed (see CableCheck)."""
        return self._cable.trusted(_on_off(self._state(self._charger_connected)))

    def _current_intervals(self) -> Intervals | None:
        telemetry = _number(self._state(self.byd.telemetry_interval))
        gps = _number(self._state(self.byd.gps_interval))
        if telemetry is None or gps is None:
            return None
        return Intervals(int(telemetry), int(gps))

    # ---------- events ----------

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        old = event.data["old_state"]
        new = event.data["new_state"]
        if new is None:
            return

        if entity_id in self._phone_bluetooth:
            connected = self._phone_connected(new)
            was = self._phone_in_car.get(entity_id, False)
            self._phone_in_car[entity_id] = connected
            if connected and not was and self._wake_allowed():
                await self._async_wake(f"{entity_id} connected to the car")
            return
        if entity_id == self._charger_connected:
            self._cable.cable(_on_off(new), self._epoch())
            if not self._cable.implausible and self._stored.pop("cable_implausible", None):
                self._save()
            self.issues.async_set_cable_implausible(self._cable.implausible)
            if self._cable_connected() is True:
                # Plugged in: the car cannot be driven now, so "expecting a drive" is over
                # rather than polling at the driving rate for the rest of its window.
                self._end_wake()
        if entity_id in self._wake_hints and self._is_wake(old, new):
            if self._wake_allowed():
                await self._async_wake(f"wake hint {entity_id}", hint=entity_id)
            return
        if entity_id == self._charger_connected and new.state == "off":
            if old is not None and old.state == STATE_ON:
                # The cable is out, so charging is over: the stop delay is only there to ride
                # out solar dips, and it held off "expecting a drive" for 2 min (2026-10-23).
                self._charging_over_now()
                if self._cancel_charge_end is not None:
                    # A paused charge cannot resume now. The unplugged poll reads the SoC.
                    self._finish_charge_session()
                # Unplugging does not always mean a drive. One reading is cheap: the car is
                # usually still awake from charging. The mode still says charging until the
                # evaluation below, but this is not a charge poll: a cloud reading that has
                # not moved yet is not stale data.
                await self._async_poll("unplugged", count_stale=False)
                # A phone that connected while the cable was in did not count then.
                if any(self._phone_in_car.values()) or self._wake_hint_on():
                    await self._async_wake("unplugged with a phone in the car", ends_early=False)

        if entity_id == self._charger_power:
            self._note_charger_power()
        elif entity_id in (self.byd.charging, self.byd.battery_power) and car_data_changed(
            old.state if old else None, new.state
        ):
            self._car_charge_data_at = self._now()
        if entity_id in (self._charger_power, self.byd.charging, self.byd.battery_power):
            self._update_charging()
            if self.mode is Mode.CHARGING:
                self.tracker.set_power(self._now(), self._charge_power_kw())
        if entity_id == self.byd.gps_last_updated and new.state not in _MISSING:
            self._moved_since_fix = False
        if entity_id == self.byd.last_updated and new.state not in _MISSING:
            car_time = self._car_time(new)
            newer = self._newest.is_newer(car_time)
            if newer:
                self._reading_at = self._now()
            reported_on = self._reported_vehicle_on()
            if car_time is None or self._newest.note(car_time, reported_on):
                # The same reading back after the BYD entities were unavailable (a reload, an
                # auth error) is not a new one: it must not undo the stale charge backoff or
                # count twice in the charger check.
                if car_time is None or newer:
                    await self._async_car_reading()
            else:
                # Not a new reading: the cloud sent an older one after a newer one.
                _LOGGER.info(
                    "Ignoring an older car reading from %s (newest %s)",
                    new.state,
                    dt_util.utc_from_timestamp(self._newest.time or 0).isoformat(),
                )
        elif (
            entity_id == self.byd.soc
            and self.byd.last_updated is None
            and car_data_changed(old.state if old else None, new.state)
            and _number(new) != _number(old)
        ):
            # Older BYD versions without the timestamp sensor: only changed SoC is visible.
            self._reading_at = self._now()
            await self._async_car_reading()

        await self.async_evaluate(f"{entity_id} changed")
        if self.mode is Mode.CHARGING:
            await self._async_check_charge()

    def _learn_charging_sign(self, power: float | None, vehicle_on: bool) -> None:
        """Keep which sign the car gives its battery power while charging (see charge_power_w)."""
        known = self._stored.get("charging_negative")
        # Its own power, trusted or not: an idle charger says nothing is going in either way.
        charger = self._charger_w() if self._charger_power else None
        if charger is None and (self._charger_power or self._at_home() is True):
            # The charger unavailable or unknown (after every restart, a gateway outage), or
            # at home with no charger reading: a solar pause with the car drawing from its
            # battery cannot be told from charging, so nothing is learned.
            return
        # Idle unless the cable is known to be out: the cable sensor is unavailable after
        # every restart while the charger already reads 0 W in a solar pause.
        cable = self._cable_connected()
        charger_idle = (
            charger is not None
            and charger < self.settings.charging_min_power_w
            and (cable is True or (cable is None and self._at_home() is not False))
        )
        negative = charging_shows_negative(
            known,
            _on_off(self._state(self.byd.charging)),
            power,
            vehicle_on,
            charger_idle=charger_idle,
        )
        if negative is None or negative == known:
            return
        _LOGGER.info(
            "The car reports charging as %s battery power", "negative" if negative else "positive"
        )
        self._stored["charging_negative"] = negative
        self._save()

    async def _async_car_reading(self) -> None:
        """A new reading arrived from the car (a poll reached it, or it pushed one)."""
        self.stale_refreshes = 0
        self._fresh_at = self._now()
        self._stale_charge_polls = 0
        self._charge_poll_after = -math.inf
        if self._stored.pop("stale_charge", None) is not None:
            self._save()
        soc = _number(self._state(self.byd.soc))
        power = _power_w(self._state(self.byd.battery_power))
        self.charger_check.car_soc(soc)
        self._check_charger_sign()
        self._check_charger(soc, power)
        # The car's charging state can change before its timestamp does, and then did not yet
        # count as a reading taken since the charger went idle.
        self._update_charging()
        vehicle_on = self._vehicle_on()
        self._learn_charging_sign(power, vehicle_on)
        charging = self._charging_signal() is True
        if soc is not None:
            self.meter.add(
                Sample(self._epoch(), soc, power, parked=not vehicle_on and not charging)
            )
            self._stored["samples"] = [
                [s.ts, s.soc, s.power_w, s.parked] for s in self.meter.samples
            ]
            self._save()
            if self.mode is Mode.CHARGING:
                self.tracker.on_reading(self._now(), soc)

        odometer = _number(self._state(self.byd.odometer))
        # Home Assistant's clock, like the cable's: the car's own clock is not in step with it.
        if self._cable.reading(
            vehicle_on=vehicle_on, odometer=odometer, time=self._epoch(), charging=charging
        ):
            # Ignored for wakes and "at home" until it next turns off, see CableCheck.
            _LOGGER.warning(
                "%s said the cable was connected while the car was on or moving. "
                "Not using it until it turns off",
                self._charger_connected,
            )
            self._stored["cable_implausible"] = self._charger_connected
            self._save()
            self.issues.async_set_cable_implausible(True)
        await self._async_wake_poll(vehicle_on)
        missed = self._odometer.update(
            odometer,
            parked=self.mode is not Mode.DRIVING and not vehicle_on,
        )
        if missed:
            # The car was driven between polls and nothing reported it. Its GPS fix is out
            # of date, so it can no longer be trusted for "at home".
            self.last_missed_drive = dt_util.utcnow()
            self._stored["last_missed_drive"] = self.last_missed_drive.isoformat()
            self._save()
            self._moved_since_fix = True
            _LOGGER.info("The car moved since the last reading: drive was missed")
            if self.byd.tracker:
                await self._async_update_entity(self.byd.tracker)
                # The GPS timestamp is not always sent (BYD has replied with 0), so a fix that
                # came back is taken as fresh rather than waiting for the timestamp to change.
                tracker = self._state(self.byd.tracker)
                if tracker is not None and tracker.state not in _MISSING:
                    self._moved_since_fix = False

    async def _async_reading_repeated(self, event: Event[EventStateReportedData]) -> None:
        """The BYD integration refreshed but the car's timestamp did not change.

        Any refresh: BYD's own timer, an HVAC reconcile or an update from elsewhere. Only
        used to end "expecting a drive" early. Stale car data counts only Smart Poll's own
        polls (see _note_poll).
        """
        # The car's reply updates the entities first and the refresh then writes them again,
        # so a repeat just after a fresh reading is that same refresh.
        if self._now() - self._fresh_at < 15:
            return
        interval = self._state(self.byd.telemetry_interval)
        # Setting an interval rewrites every BYD entity without a refresh, so those writes
        # are not polls. last_changed, not last_reported: the interval number is on the same
        # BYD coordinator, so every refresh writes it again just before this runs, and only
        # a write of ours changes its value.
        if interval is not None and (dt_util.utcnow() - interval.last_changed).total_seconds() < 5:
            return
        # The car did not answer, so it was not turned on.
        await self._async_wake_poll(self._vehicle_on())

    def _note_poll(
        self,
        before: str | None,
        before_reported: float | None,
        after: str | None,
        after_reported: float | None,
    ) -> None:
        """One of our polls is done: count it towards stale data if it brought nothing new.

        The car's timestamp state and its last_reported either side of the poll. Only while
        driving or charging (see counts_stale_polls).
        """
        if not counts_stale_polls(self.mode):
            return
        if not poll_brought_nothing(before, before_reported, after, after_reported):
            return
        self.stale_refreshes += 1
        if self.stale_refreshes == STALE_REFRESHES:
            _LOGGER.info("Car data is stale: the car's last reading keeps coming back")
        self._notify()

    @property
    def data_stale(self) -> bool:
        return data_stale(self.stale_refreshes)

    def _wake_allowed(self) -> bool:
        return wake_allowed(self._cable_connected())

    def _wake_hint_on(self) -> bool:
        return any(
            (state := self._state(e)) is not None and wake_hint_on(state.domain, state.state)
            for e in self._wake_hints
        )

    @staticmethod
    def _is_wake(old: State | None, new: State) -> bool:
        return is_wake_hint(new.domain, old.state if old else None, new.state)

    @callback
    def _update_charging(self) -> None:
        signal = self._charging_signal()
        if signal is None:
            self._watch_charger_hold()
            return
        if signal:
            self._cancel_stop_delay()
            self._set_charging(True)
            return
        if self._charging and self._cancel_stop is None and not self._stopped:
            self._cancel_stop = async_call_later(
                self.hass, self.settings.charging_stop_delay, self._async_stop_delay_done
            )

    def _cancel_stop_delay(self) -> None:
        if self._cancel_stop is not None:
            self._cancel_stop()
            self._cancel_stop = None

    def _charging_over_now(self) -> None:
        """Charging is over for certain (not a solar dip), so without the stop delay."""
        self._cancel_stop_delay()
        self._charging = False

    def _set_charging(self, charging: bool) -> None:
        self._charging = charging
        if charging:
            power = self._trusted_charger_w()
            self._charger_charged = (
                power is not None and power >= self.settings.charging_min_power_w
            )

    async def _async_stop_delay_done(self, _now: datetime) -> None:
        self._cancel_stop = None
        if self._charging_signal() is False:
            self._charging = False
            await self.async_evaluate("charging stopped")

    async def _async_wake(
        self, reason: str, *, ends_early: bool = True, hint: str | None = None
    ) -> None:
        if self._stopped:
            return
        if not self._wake.active(self._now()):
            self._wake_hints_in.clear()
        if hint is not None:
            self._wake_hints_in.add(hint)
        self._wake.start(self._now(), self.settings.wake_window, ends_early=ends_early)
        if self._cancel_wake is not None:
            self._cancel_wake()
        self._cancel_wake = async_call_later(
            self.hass, self._wake.until - self._now() + 1, self._async_wake_done
        )
        await self.async_evaluate(reason)

    def _end_wake(self) -> None:
        self._wake.end()
        if self._cancel_wake is not None:
            self._cancel_wake()
            self._cancel_wake = None

    async def _async_wake_done(self, _now: datetime) -> None:
        self._cancel_wake = None
        await self.async_evaluate("wake window ended")

    def _driver_in_car(self) -> bool:
        """Whether a phone is still connected to the car, or a hint that started the window
        is still on, so "expecting a drive" does not end early (see WakeWindow)."""
        if any(self._phone_in_car.values()):
            return True
        return any(
            (state := self._state(e)) is not None and wake_hint_on(state.domain, state.state)
            for e in self._wake_hints_in
        )

    async def _async_wake_poll(self, car_on: bool) -> None:
        """A poll during "expecting a drive" found the car on or off (see WakeWindow)."""
        if not self._wake.car_seen(self._now(), car_on=car_on, held=self._driver_in_car()):
            return
        _LOGGER.info(
            "Expecting a drive: the car was still off at %s polls in a row, back to parked",
            self._wake.off_polls,
        )
        self._end_wake()
        await self.async_evaluate("car still off")

    # ---------- decisions ----------

    async def async_evaluate(self, reason: str) -> None:
        if not self._active or self._polling_disabled():
            return
        mode = decide_mode(
            vehicle_on=self._vehicle_on(),
            charging=self._charging,
            waking=self._wake.active(self._now()),
        )
        if mode is not self.mode:
            await self._async_enter(mode, reason)
            # Turned off or unloaded while entering the mode, or another event moved the mode
            # on meanwhile: its own evaluation has applied the newer mode's intervals.
            if not self._active or self.mode is not mode:
                return
        self._watch_keep_end()
        home = self._at_home()
        if home != self.home:
            _LOGGER.info("At home: %s -> %s (%s)", self.home, home, reason)
            self.home = home
            self._stored["home"] = home
            self._save()
        await self._async_apply(self.settings.intervals(mode, home=home is True), reason)
        self._notify()

    async def _async_enter(self, mode: Mode, reason: str) -> None:
        previous = self.mode
        self.mode = mode
        self.reason = reason
        _LOGGER.info("Mode %s -> %s (%s)", previous, mode, reason)

        if mode is Mode.DRIVING:
            self._odometer.note_drive()
        if stale_count_restarts(previous, mode) and self.stale_refreshes:
            # async_evaluate updates the entities once the mode is entered.
            self.stale_refreshes = 0
        # Charging times its own polls, and parked needs no faster rate started.
        if mode is Mode.CHARGING or (mode is Mode.PARKED and self._deferred_for_rate):
            self._cancel_deferred_poll()
        if not keeps_just_parked_polls(mode):
            self._cancel_just_parked_polls()
        if delays := just_parked_polls(previous, mode):
            self._cancel_just_parked_polls()
            self._cancel_just_parked = [
                async_call_later(self.hass, delay, self._async_just_parked_poll) for delay in delays
            ]
        if drive_ended(previous, mode) and self.byd.tracker:
            await self._async_update_entity(self.byd.tracker)
        if mode is Mode.DRIVING and self._cancel_charge_end is not None:
            # Driven off: the paused charge is over, and the drive polls anyway.
            self._finish_charge_session()
        # Only a charge that got as far as its charge check has a session. One left while still
        # being entered (charging stopped during the end of drive GPS fix) has nothing to pause
        # or end, and a pause there polled for a charge never tracked 20 min later.
        if previous is Mode.CHARGING and self._cancel_charge_check is not None:
            self._stop_charge_check()
            # Only when the charger itself showed the charge did it see the stop: one reading
            # then catches the final SoC. A charge seen in the car's data alone (public, or
            # no charger) was only seen to stop because the car was just polled. Not put
            # off: inside the gap (unplugged, say) a poll has just gone out.
            end_poll = self._charger_charged and self.charger_check.trusted
            if mode is not Mode.DRIVING and self._cable_connected() is not False:
                # Maybe only a pause (solar and clouds): the session and its end poll wait.
                # So at the true end of a charge the end poll comes RESUME_WINDOW later, when
                # the car may be asleep, and a restart before then drops it. What the session
                # learned is stored now, as a restart does not unload the entry.
                self.tracker.pause(self._now())
                self._store_kwh_per_pct(self.tracker.kept_kwh_per_pct())
                self._end_poll_wanted = end_poll
                self._cancel_charge_end = async_call_later(
                    self.hass, RESUME_WINDOW, self._async_charge_end_due
                )
            else:
                self._finish_charge_session()
                if end_poll and mode is not Mode.DRIVING:
                    await self._async_poll("charge ended", put_off=False)
        # The awaits above let other events in: another mode, turned off, or unloaded.
        if mode is Mode.CHARGING and self._active and self.mode is mode:
            # Never two at once: a handle overwritten here would keep polling for good.
            self._stop_charge_check()
            paused = self._cancel_charge_end is not None
            if self._cancel_charge_end is not None:
                self._cancel_charge_end()
                self._cancel_charge_end = None
            self._end_poll_wanted = False
            if self.tracker.resume(self._now(), self._charge_power_kw()):
                _LOGGER.info("Charging resumed within %s min: same session", RESUME_WINDOW // 60)
            elif paused:
                # Its end timer had not run yet: the tracker finished it instead.
                self._store_kwh_per_pct(self.tracker.prior)
            self._cancel_charge_check = async_track_time_interval(
                self.hass, self._async_charge_check_tick, timedelta(seconds=CHARGE_CHECK_INTERVAL)
            )

    async def _async_apply(self, target: Intervals, reason: str) -> None:
        if not self._active or not self._writes.due(target):
            return
        before = self._current_intervals()
        await self._set_intervals(target)
        current = self._current_intervals()
        if not self._writes.written(target, current):
            # Tried again on the next evaluation, or when the numbers come back. Often only
            # the BYD integration still starting or reloading, so a warning only once retries
            # have run out.
            if self._writes.misses >= MAX_WRITE_MISSES:
                log = _LOGGER.warning
            else:
                log = _LOGGER.info if self._writes.misses == 1 else _LOGGER.debug
            log(
                "BYD poll intervals did not take (try %s): wanted %s s / %s s, they show %s",
                self._writes.misses,
                target.telemetry,
                target.gps,
                current,
            )
            return
        if before is None:
            return
        # Before BYD Vehicle 0.0.93 its timer was set for the old rate and only restarted after
        # a poll, so a faster rate needs one poll now to take effect. Also for the first write
        # after a restart or turning on: BYD's startup refresh set its timer from the value it
        # had saved (3600 s, say), and a restart mid-drive otherwise saw the drive once an
        # hour. On 0.0.93 the poll is harmless. Charging times its own polls.
        if target.telemetry < before.telemetry and self.mode is not Mode.CHARGING:
            await self._async_poll(f"faster rate ({reason})", for_rate=True)
        if target.gps < before.gps and self.byd.tracker:
            await self._async_update_entity(self.byd.tracker)

    def _cancel_just_parked_polls(self) -> None:
        for cancel in self._cancel_just_parked:
            cancel()
        self._cancel_just_parked = []

    async def _async_just_parked_poll(self, _now: datetime) -> None:
        if just_parked_poll_wanted(
            self.mode,
            enabled=self.enabled,
            charger_trusted=self._trusted_charger_w() is not None,
            cable_connected=self._cable_connected(),
            home=self.home,
        ):
            await self._async_poll("just parked")

    def _finish_charge_session(self) -> None:
        """End the charge session, keeping what it learned about kWh per percent."""
        if self._cancel_charge_end is not None:
            self._cancel_charge_end()
            self._cancel_charge_end = None
        self._end_poll_wanted = False
        self._store_kwh_per_pct(self.tracker.finish())

    async def _async_charge_end_due(self, _now: datetime) -> None:
        """A paused charge did not resume in time: it has ended."""
        self._cancel_charge_end = None
        poll = self._end_poll_wanted
        self._finish_charge_session()
        if poll and self.mode is not Mode.CHARGING:
            await self._async_poll("charge ended", put_off=False)

    def _keep_stale_charge(self, reading: str) -> None:
        """Keep the stale charge count across restarts (see signals.kept_stale_charge)."""
        self._stored["stale_charge"] = {
            "reading": reading,
            "fresh": self._epoch() - (self._now() - self._fresh_at),
            "polls": self._stale_charge_polls,
        }
        self._save()

    def _stop_charge_check(self) -> None:
        if self._cancel_charge_check is not None:
            self._cancel_charge_check()
            self._cancel_charge_check = None

    async def _async_charge_check_tick(self, _now: datetime) -> None:
        await self._async_check_charge()

    async def _async_check_charge(self) -> None:
        if not self._active or self.mode is not Mode.CHARGING:
            return
        now = self._now()
        due = charge_poll_due(
            self.tracker.poll_due(now),
            charging=self._charging_signal() is True,
            power_kw=self.tracker.power_kw,
            reading_age=now - self._fresh_at,
        )
        if due and now >= self._charge_poll_after:
            before = self._state(self.byd.last_updated)
            if await self._async_poll("charge tick due", put_off=False) and before is not None:
                after = self._state(self.byd.last_updated)
                if after is not None and after.state == before.state:
                    self._stale_charge_polls += 1
                    wait = stale_backoff(self._stale_charge_polls)
                    self._charge_poll_after = now + wait
                    _LOGGER.debug("Charge poll brought no new reading, next in %s s", wait)
                    self._keep_stale_charge(after.state)
        # Time alone can end a charge: the car's last reading growing too old, or a charger
        # with no reading for longer than it may hold the charging state.
        if self._active and self.mode is Mode.CHARGING:
            self._update_charging()
        self._notify()

    # ---------- actions ----------

    async def _async_poll(
        self,
        reason: str,
        *,
        put_off: bool = True,
        for_rate: bool = False,
        count_stale: bool = True,
    ) -> bool:
        """Poll the car now, or as soon as the minimum gap allows.

        A poll inside the gap is put off, not dropped: a poll is also what makes the BYD
        integration pick up a faster rate, and dropping one left it on a 30 min timer for a
        whole drive (2026-10-23). Charge polls pass put_off=False (see poll_action).
        `for_rate` marks a poll only there to start a faster rate, dropped if the mode goes
        back to parked before it is due. `count_stale=False` keeps a poll that brings nothing
        out of the stale count whatever the mode (see _note_poll).
        """
        if not self._active or self._polling_disabled():
            return False
        now = self._now()
        wait = poll_wait(self._last_poll, now, MIN_POLL_GAP)
        action = poll_action(wait, put_off=put_off)
        if action == "skip":
            return False
        if action == "later":
            if self._cancel_deferred is None:
                self._deferred_for_rate = for_rate
                self._cancel_deferred = async_call_later(
                    self.hass,
                    wait,
                    partial(self._async_deferred_poll, reason, count_stale=count_stale),
                )
            elif not for_rate:
                self._deferred_for_rate = False
            return False
        self._cancel_deferred_poll()
        self._last_poll = now
        _LOGGER.debug("Polling the car: %s", reason)
        # Plain values, not the State: an unchanged write moves last_reported on the same
        # State object, so one kept across the await would read as the state after it.
        before = self._state(self.byd.last_updated)
        before_state = before.state if before else None
        before_reported = before.last_reported_timestamp if before else None
        await self._async_update_entity(self.byd.telemetry_interval)
        after = self._state(self.byd.last_updated)
        if not count_stale:
            return True
        self._note_poll(
            before_state,
            before_reported,
            after.state if after else None,
            after.last_reported_timestamp if after else None,
        )
        return True

    async def _async_deferred_poll(
        self, reason: str, _now: datetime, *, count_stale: bool = True
    ) -> None:
        self._cancel_deferred = None
        await self._async_poll(f"{reason} (put off)", count_stale=count_stale)

    def _cancel_deferred_poll(self) -> None:
        if self._cancel_deferred is not None:
            self._cancel_deferred()
            self._cancel_deferred = None

    async def _async_update_entity(self, entity_id: str) -> None:
        # Every poll and GPS fix goes through here.
        if not self._active:
            return
        try:
            await self.hass.services.async_call(
                "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
            )
        except Exception:  # noqa: BLE001 - a failed poll must not stop the controller
            _LOGGER.warning("Polling via %s failed", entity_id, exc_info=True)

    async def _set_intervals(self, target: Intervals) -> bool:
        """Write the BYD poll intervals. Returns True if they read back as target.

        A write to an unavailable number is skipped without an error, so only reading them
        back tells whether it took.
        """
        current = self._current_intervals()
        self._writing += 1
        try:
            for entity_id, value, now_value in (
                (
                    self.byd.telemetry_interval,
                    target.telemetry,
                    current.telemetry if current else None,
                ),
                (self.byd.gps_interval, target.gps, current.gps if current else None),
            ):
                if now_value == value:
                    continue
                try:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": entity_id, "value": value},
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001 - retried on the next evaluation
                    _LOGGER.warning("Could not set %s to %s", entity_id, value, exc_info=True)
        finally:
            self._writing -= 1
        return self._current_intervals() == target

    async def _async_interval_changed(self, event: Event[EventStateChangedData]) -> None:
        """A BYD poll interval number changed: a BYD reload, a manual edit or our own write.

        Written again if it no longer holds what Smart Poll wants, for example after a BYD
        reload that skipped our last write. While smart polling is off, only the original
        intervals are written, and only if turning it off could not write them.
        """
        if self._writing:
            return
        old = event.data["old_state"]
        came_back = old is None or old.state in _MISSING
        if not self.enabled:
            # Only when a number comes back, so a value the user sets by hand is left alone.
            if self._restore_pending and came_back and self._current_intervals() is not None:
                await self.async_restore_originals()
            return
        gave_up = self._writes.gave_up
        current = self._current_intervals()
        if self._writes.numbers_changed(current, came_back=came_back):
            await self.async_evaluate(f"{event.data['entity_id']} changed")
        elif self._writes.gave_up and not gave_up:
            target = self._writes.target
            _LOGGER.warning(
                "Something else keeps setting the BYD poll intervals (to %s, wanted %s): "
                "not writing them again until the wanted intervals change or BYD Vehicle "
                "reloads. Turn off any automation or other tool that sets %s or %s",
                current,
                target,
                self.byd.telemetry_interval,
                self.byd.gps_interval,
            )

    def _save(self) -> None:
        # After unloading, a late write of this copy would overwrite the next controller's.
        if self._stopped:
            return
        self._store.async_delay_save(lambda: self._stored, 30)
