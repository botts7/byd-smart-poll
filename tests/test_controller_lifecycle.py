"""The controller's timers and polls across off, on, unload and racing events.

Uses the fake Home Assistant from test_controller, with a fake clock and fake timers that
remember which handles are still live, so a leaked timer shows up as one left behind.
"""

import asyncio
import importlib
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from functools import partial

import pytest

pytest.importorskip("homeassistant")

from bsp import controller as ctl_mod  # noqa: E402
from bsp.byd_entities import BydEntities  # noqa: E402
from bsp.charge_tracker import BELOW, Bands, ChargeTracker, Learned  # noqa: E402
from bsp.modes import Intervals, Mode, Settings  # noqa: E402
from bsp.signals import AWAKE_HOLD, ChargerSign  # noqa: E402
from homeassistant.core import State  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from test_controller import (  # noqa: E402
    CABLE,
    GPS,
    LAST_UPDATED,
    PARKED,
    SOC,
    TELEMETRY,
    VEHICLE_ON,
    FakeHass,
)

CHARGER = "sensor.wallbox_power"
CHARGING_FLAG = "binary_sensor.car_charging"
TRACKER = "device_tracker.car"
CHARGING_RATES = Settings().intervals(Mode.CHARGING, home=True)


class Timer:
    def __init__(self, timers: "Timers", due: float, action, every: float | None) -> None:
        self.timers, self.due, self.action, self.every = timers, due, action, every
        self.live = True

    def cancel(self) -> None:
        self.live = False


class Timers:
    """async_call_later and async_track_time_interval on a clock the test moves."""

    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self.all: list[Timer] = []

    def call_later(self, _hass, delay, action):
        timer = Timer(self, self.hass.now + delay, action, None)
        self.all.append(timer)
        return timer.cancel

    def every(self, _hass, action, interval):
        seconds = interval.total_seconds()
        timer = Timer(self, self.hass.now + seconds, action, seconds)
        self.all.append(timer)
        return timer.cancel

    def live(self, *, repeating: bool | None = None) -> list[Timer]:
        return [
            t
            for t in self.all
            if t.live and (repeating is None or (t.every is not None) == repeating)
        ]

    async def run_until(self, until: float) -> None:
        """Move the clock, firing each live timer when it is due."""
        while True:
            due = [t for t in self.live() if t.due <= until]
            if not due:
                break
            timer = min(due, key=lambda t: t.due)
            self.hass.now = max(self.hass.now, timer.due)
            if timer.every is None:
                timer.live = False
            else:
                timer.due += timer.every
            result = timer.action(dt_util.utcnow())
            if isinstance(result, Awaitable):
                await result
        self.hass.now = until


class Hass(FakeHass):
    def __init__(self) -> None:
        super().__init__()
        self.now = 10_000.0
        self.loop.time = lambda: self.now
        # Run while a service call is "in flight", to let another event in at that point.
        self.during_call: Callable[[str, str, dict], Awaitable[None]] | None = None

    async def _call(self, domain, service, data, blocking=False):
        await super()._call(domain, service, data, blocking)
        hook, self.during_call = self.during_call, None
        if hook is not None:
            await hook(domain, service, data)


class Store:
    """Home Assistant's Store: a delayed save only lands once its delay has passed."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.pending = None

    async def async_load(self) -> dict:
        return self.data

    async def async_save(self, data: dict) -> None:
        self.pending = None
        self.data = dict(data)

    def async_delay_save(self, data_func, _delay) -> None:
        self.pending = data_func

    def flush(self) -> None:
        """The delay passed (or Home Assistant's final write at shutdown)."""
        if self.pending is not None:
            self.data = dict(self.pending())
            self.pending = None


@pytest.fixture
def rig(monkeypatch):
    hass = Hass()
    timers = Timers(hass)
    # Home Assistant's clock moves with the test's, so last_changed and last_reported do too.
    base = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: base + timedelta(seconds=hass.now))
    monkeypatch.setattr(ctl_mod, "async_call_later", timers.call_later)
    monkeypatch.setattr(ctl_mod, "async_track_time_interval", timers.every)
    monkeypatch.setattr(ctl_mod, "Store", lambda *args, **kwargs: Store())
    byd = BydEntities(
        vin="VIN",
        car_name="Car",
        telemetry_interval=TELEMETRY,
        gps_interval=GPS,
        vehicle_on=VEHICLE_ON,
        soc=SOC,
        charging=CHARGING_FLAG,
        tracker=TRACKER,
        last_updated=LAST_UPDATED,
    )
    controller = ctl_mod.SmartPollController(
        hass,
        "entry",
        byd,
        Settings(),
        charger_power=CHARGER,
        charger_connected=CABLE,
        wake_hints=[],
        car_bluetooth=None,
        phone_bluetooth=[],
        kwh_per_pct=0.8,
    )
    controller.issues.async_check = lambda: None
    controller.issues.async_stop = lambda: None
    controller._stored = {"originals": {"telemetry": 300, "gps": 300}}
    hass.set(TELEMETRY, "3600.0")
    hass.set(GPS, "28800.0")
    hass.set(VEHICLE_ON, "off")
    hass.set(SOC, "50")
    hass.set(CHARGING_FLAG, "off")
    hass.set(TRACKER, "home")
    hass.set(CABLE, "on")
    hass.set(CHARGER, "7000", unit_of_measurement="W")
    return hass, controller, timers


async def _charging(controller) -> None:
    controller._note_charger_power()
    controller._update_charging()
    await controller.async_evaluate("startup")
    assert controller.mode is Mode.CHARGING


def test_off_and_on_while_charging_leaves_no_charge_check_behind(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        assert len(timers.live(repeating=True)) == 1
        await controller.async_set_enabled(False)
        assert controller.mode is None
        assert timers.live(repeating=True) == []
        polls = hass.polls()
        # Nothing polls while it is off, however long the charge runs.
        await timers.run_until(hass.now + 3600)
        await controller._async_check_charge()
        assert hass.polls() == polls

        await controller.async_set_enabled(True)
        assert controller.mode is Mode.CHARGING
        assert len(timers.live(repeating=True)) == 1

        await controller.async_stop()
        assert timers.live() == []
        polls = hass.polls()
        await timers.run_until(hass.now + 3600)
        await controller._async_check_charge()
        await controller._async_poll("late")
        assert hass.polls() == polls

    asyncio.run(run())


def test_unplug_while_off_does_not_poll(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await controller.async_set_enabled(False)
        polls = hass.polls()
        hass.set(CHARGER, "0", unit_of_measurement="W")
        old = hass.data[CABLE]
        hass.set(CABLE, "off")
        await controller._async_state_changed(
            _changed(CABLE, old, hass.data[CABLE]),
        )
        await timers.run_until(hass.now + 600)
        assert hass.polls() == polls

    asyncio.run(run())


def _changed(entity_id, old, new):
    from types import SimpleNamespace

    return SimpleNamespace(data={"entity_id": entity_id, "old_state": old, "new_state": new})


def test_unload_during_the_unplugged_poll_starts_nothing_after(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)

        async def unload(domain, service, _data):
            if service == "update_entity":
                await controller.async_stop()

        hass.set(CHARGER, "0", unit_of_measurement="W")
        old = hass.data[CABLE]
        hass.set(CABLE, "off")
        hass.during_call = unload
        await controller._async_state_changed(_changed(CABLE, old, hass.data[CABLE]))
        assert controller._stopped
        assert timers.live() == []
        polls = hass.polls()
        await timers.run_until(hass.now + 3600)
        assert hass.polls() == polls

    asyncio.run(run())


def test_entering_charging_again_leaves_one_charge_check(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        # A path that left the mode without stopping the check (none should): entering
        # charging again must not start a second one beside it.
        controller.mode = Mode.PARKED
        await controller._async_enter(Mode.CHARGING, "again")
        assert len(timers.live(repeating=True)) == 1

    asyncio.run(run())


def test_charge_check_not_started_when_the_mode_moved_on_meanwhile(rig):
    hass, controller, timers = rig

    async def run():
        # Driving with the charger already showing power (DC through the home meter, say).
        hass.set(VEHICLE_ON, "on")
        controller._note_charger_power()
        controller._update_charging()
        await controller.async_evaluate("startup")
        assert controller.mode is Mode.DRIVING

        async def charger_stops(domain, service, data):
            # While the GPS fix for the end of the drive is out, the charger stops.
            if service == "update_entity" and data["entity_id"] == TRACKER:
                hass.set(CHARGER, "0", unit_of_measurement="W")
                controller._charging = False
                await controller.async_evaluate("charger stopped")

        hass.during_call = charger_stops
        hass.set(VEHICLE_ON, "off")
        calls = len(hass.calls)
        await controller.async_evaluate("car off")
        assert controller.mode is Mode.PARKED
        assert timers.live(repeating=True) == []
        # The outer evaluation does not write its older mode's intervals over the newer ones,
        # nor poll for a "faster rate" that never applied.
        assert controller._current_intervals() == PARKED
        later = hass.calls[calls:]
        assert [d["entity_id"] for _, s, d in later if s == "update_entity"] == [TRACKER]
        # A charge that never got going has no session to pause, nor an end to poll for.
        assert controller._cancel_charge_end is None
        polls = hass.polls()
        await timers.run_until(hass.now + 30 * 60)
        assert hass.polls() == polls

    asyncio.run(run())


def test_plugged_in_during_the_end_of_drive_fix_keeps_the_charging_rates(rig):
    hass, controller, timers = rig
    hass.set(CHARGER, "0", unit_of_measurement="W")

    async def run():
        hass.set(VEHICLE_ON, "on")
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        assert controller.mode is Mode.DRIVING

        async def plugged_in(domain, service, data):
            if service == "update_entity" and data["entity_id"] == TRACKER:
                await _charger(controller, hass, 7000)

        hass.during_call = plugged_in
        hass.set(VEHICLE_ON, "off")
        await controller.async_evaluate("car off")
        assert controller.mode is Mode.CHARGING
        assert controller._current_intervals() == CHARGING_RATES
        assert len(timers.live(repeating=True)) == 1

    asyncio.run(run())


def test_unload_saves_straight_away(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await controller.async_set_enabled(False)
        # A reload within the 30 s save delay reads the file: it must say off.
        await controller.async_stop()
        assert controller._store.data["enabled"] is False
        # And nothing written after the unload, also once a save delay would have passed.
        before = dict(controller._store.data)
        controller._stored["enabled"] = True
        controller._save()
        controller._store.flush()
        assert controller._store.data == before

    asyncio.run(run())


@pytest.fixture
def start(rig, monkeypatch):
    """Runs async_start with the given stored data, with nothing really subscribed."""
    hass, controller, timers = rig
    for name in ("async_track_state_change_event", "async_track_state_report_event"):
        monkeypatch.setattr(ctl_mod, name, lambda *_args: lambda: None)
    controller.issues.async_start = lambda: None

    async def run(stored: dict) -> None:
        controller._store.data = stored
        await controller.async_start()

    return hass, controller, timers, run


def test_restart_with_the_charger_unavailable_at_home_does_not_poll(start):
    hass, controller, timers, run = start
    # Both wallbox sensors come back a little after Home Assistant, and the car's last reading
    # is from a solar charge that has paused since.
    hass.set(CHARGER, "unavailable")
    hass.set(CABLE, "unavailable")
    hass.set(CHARGING_FLAG, "on")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": True})
        assert controller.mode is Mode.PARKED
        assert not controller._charging
        # The charger comes back idle: still parked, and no poll at all.
        hass.set(CHARGER, "0", unit_of_measurement="W")
        hass.set(CABLE, "on")
        controller._note_charger_power()
        controller._update_charging()
        await controller.async_evaluate("charger back")
        await timers.run_until(hass.now + 600)
        assert controller.mode is Mode.PARKED
        assert hass.polls() == 0

    asyncio.run(go())


def test_charger_unavailable_mid_charge_at_home_keeps_charging(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        hass.set(CHARGER, "unavailable")
        controller._update_charging()
        await timers.run_until(hass.now + 300)
        assert controller.mode is Mode.CHARGING

    asyncio.run(run())


def test_heating_on_the_cable_at_a_charge_limit_is_not_stored(rig):
    hass, controller, timers = rig
    hass.set(CHARGER, "2000", unit_of_measurement="W")
    hass.set(SOC, "80")

    async def run():
        controller._cable.cable(True, 0.0)
        await _charging(controller)
        for minutes in (60, 61):
            hass.now = 10_000.0 + minutes * 60
            await controller._async_car_reading()
        assert controller.charger_check.drawing
        assert not controller.charger_check.implausible
        assert controller._stored.get("charger_implausible") is None
        assert not controller.issues.charger_implausible
        # As the state change that brought the reading does next.
        await controller.async_evaluate("reading")
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


async def _charger(controller, hass, watts: float) -> None:
    """The charger's power changes, as the state change handler takes it."""
    hass.set(CHARGER, str(watts), unit_of_measurement="W")
    controller._note_charger_power()
    controller._update_charging()
    if controller.mode is Mode.CHARGING:
        controller.tracker.set_power(controller._now(), controller._charge_power_kw())
    await controller.async_evaluate("charger changed")


def test_solar_pause_and_resume_keeps_the_session_without_polls(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await timers.run_until(hass.now + 30)
        assert hass.polls() == 1  # the first reading of the session
        controller.tracker.on_reading(hass.now, 50)
        polls = hass.polls()
        # A cloud: the charger stops, and after the stop delay the car counts as parked.
        await _charger(controller, hass, 0)
        await timers.run_until(hass.now + 300)
        assert controller.mode is Mode.PARKED
        # Back within the window: same session, and no poll for either.
        await _charger(controller, hass, 7000)
        assert controller.mode is Mode.CHARGING
        await timers.run_until(hass.now + 60)
        assert hass.polls() == polls
        assert controller.tracker.last_tick is None and controller.tracker._soc == 50

        # The charge ends for good: one poll for the final SoC, once the window has passed.
        controller.tracker.learned[BELOW] = Learned(0.9, 0.05, pct=40)
        await _charger(controller, hass, 0)
        await timers.run_until(hass.now + 300)
        assert hass.polls() == polls
        # What it learned is stored at the pause: a restart in the window does not unload.
        kept = controller.tracker.kept_kwh_per_pct()
        assert kept.below != pytest.approx(0.8)
        controller._store.flush()
        assert controller._store.data["kwh_per_pct_below"] == pytest.approx(kept.below)
        assert controller._store.data["kwh_per_pct_above"] == pytest.approx(kept.above)
        assert controller._store.data["kwh_per_pct"] == pytest.approx(kept.below)
        await timers.run_until(hass.now + 20 * 60)
        assert hass.polls() == polls + 1

    asyncio.run(run())


def test_charge_seen_only_in_the_car_data_ends_without_a_poll(rig):
    hass, controller, timers = rig
    # Away at a public charger, the home charger idle.
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        hass.now += 60
        hass.set(CHARGING_FLAG, "on")
        controller._reading_at = hass.now
        controller._update_charging()
        await controller.async_evaluate("car says charging")
        assert controller.mode is Mode.CHARGING
        await timers.run_until(hass.now + 30)
        controller.tracker.on_reading(hass.now, 50)
        polls = hass.polls()
        hass.set(CHARGING_FLAG, "off")
        controller._update_charging()
        await timers.run_until(hass.now + 30 * 60)
        assert controller.mode is Mode.PARKED
        assert hass.polls() == polls

    asyncio.run(run())


def test_unplug_mid_charge_polls_once(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await timers.run_until(hass.now + 30)
        hass.now += 600
        polls = hass.polls()
        old = hass.data[CABLE]
        hass.set(CABLE, "off")
        hass.set(CHARGER, "0", unit_of_measurement="W")
        await controller._async_state_changed(_changed(CABLE, old, hass.data[CABLE]))
        await timers.run_until(hass.now + 30 * 60)
        assert controller.mode is Mode.PARKED
        assert hass.polls() == polls + 1

    asyncio.run(run())


def test_charging_on_stale_car_data_away_drops_to_parked(rig):
    hass, controller, timers = rig
    # Unplugged at a destination charger, the car asleep with its last upload saying charging.
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        hass.now += 60
        hass.set(CHARGING_FLAG, "on")
        await _reading(controller, hass, "2026-11-14T08:01:00+00:00")
        assert controller.mode is Mode.CHARGING
        # Each charge poll brings the same reading back. Not doubted while it is recent.
        await timers.run_until(hass.now + 15 * 60)
        assert controller.mode is Mode.CHARGING
        await timers.run_until(hass.now + 30 * 60)
        assert controller.mode is Mode.PARKED
        polls = hass.polls()
        await timers.run_until(hass.now + 3 * 3600)
        assert controller.mode is Mode.PARKED
        assert hass.polls() == polls

    asyncio.run(run())


def test_put_off_faster_rate_poll_dropped_when_parked_again(rig):
    hass, controller, timers = rig
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        assert controller.mode is Mode.PARKED
        await controller._async_poll("unplugged")
        # A wake hint inside the gap: its faster rate poll is put off.
        await controller._async_wake("wake hint")
        assert controller.mode is Mode.WAKING
        assert controller._cancel_deferred is not None
        polls = hass.polls()
        # Back to parked before it is due: it no longer applies.
        controller._end_wake()
        await controller.async_evaluate("wake ended")
        assert controller.mode is Mode.PARKED
        await timers.run_until(hass.now + 120)
        assert hass.polls() == polls

    asyncio.run(run())


def test_unload_for_a_disabled_entry_restores_the_originals(rig):
    hass, controller, _ = rig

    async def run():
        await _charging(controller)
        await controller.async_stop(restore=True)
        assert controller._current_intervals() == Intervals(300, 300)

    asyncio.run(run())


def test_unload_for_a_reload_keeps_the_intervals(rig):
    hass, controller, _ = rig

    async def run():
        await _charging(controller)
        before = controller._current_intervals()
        await controller.async_stop()
        assert controller._current_intervals() == before

    asyncio.run(run())


async def _reading(controller, hass, car_time: str) -> None:
    """A new reading from the car arrives, as the state change handler takes it."""
    old = hass.data.get(LAST_UPDATED)
    hass.set(LAST_UPDATED, car_time)
    await controller._async_state_changed(_changed(LAST_UPDATED, old, hass.data[LAST_UPDATED]))


async def _poll_brings_the_same(controller, hass) -> bool:
    """One of our polls, whose refresh writes the car's timestamp again unchanged."""

    async def same(domain, service, data):
        old = hass.data[LAST_UPDATED]
        hass.write_again(LAST_UPDATED, old.last_reported + timedelta(seconds=1))

    hass.during_call = same
    sent = await controller._async_poll("test", put_off=False)
    hass.during_call = None
    return sent


def _write_again(hass, entity_id: str) -> None:
    """Home Assistant's async_write_ha_state with an unchanged state: only last_reported moves."""
    hass.write_again(entity_id)


async def _rewrite(controller, hass) -> None:
    """The BYD integration refreshes with no new reading (its timer, an HVAC reconcile).

    Like the real integration, the refresh writes every entity on its coordinator again,
    the interval numbers too, before the repeated reading is handled.
    """
    _write_again(hass, TELEMETRY)
    _write_again(hass, LAST_UPDATED)
    await controller._async_reading_repeated(None)


def test_public_charge_with_rewrites_and_a_slow_reading_stays_charging(rig, caplog):
    hass, controller, timers = rig
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        hass.now += 60
        hass.set(CHARGING_FLAG, "on")
        await _reading(controller, hass, "2026-11-14T08:01:00+00:00")
        assert controller.mode is Mode.CHARGING
        # The first charge poll brings nothing new, then two rewrites.
        assert controller._stale_charge_polls == 1
        with caplog.at_level(logging.INFO):
            # Rewrites from anything else (an HVAC reconcile) are not stale data.
            for _ in range(2):
                hass.now += 30
                await _rewrite(controller, hass)
            assert not controller.data_stale
            # Two of our own polls that bring the same reading back are.
            for _ in range(2):
                hass.now += 61
                await _poll_brings_the_same(controller, hass)
        assert controller.data_stale
        assert caplog.text.count("Car data is stale") == 1
        await timers.run_until(hass.now + 20 * 60)
        assert controller.mode is Mode.CHARGING
        # The next percent arrives, then another slow stretch.
        await _reading(controller, hass, "2026-11-14T08:22:00+00:00")
        await _rewrite(controller, hass)
        await timers.run_until(hass.now + 25 * 60)
        assert controller.mode is Mode.CHARGING

    asyncio.run(run())


def test_charger_down_for_long_at_home_lets_the_car_decide(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        # The wallbox gateway goes down mid-charge, and the car stops charging meanwhile.
        hass.set(CHARGING_FLAG, "off")
        down = dt_util.utcnow() - timedelta(minutes=5)
        hass.data[CHARGER] = State(CHARGER, "unavailable", last_changed=down)
        await timers.run_until(hass.now + 300)
        assert controller.mode is Mode.CHARGING
        down = dt_util.utcnow() - timedelta(minutes=11)
        hass.data[CHARGER] = State(CHARGER, "unavailable", last_changed=down)
        await timers.run_until(hass.now + 300)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_removed_charger_does_not_hold_charging(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        hass.set(CHARGING_FLAG, "off")
        del hass.data[CHARGER]
        await timers.run_until(hass.now + 300)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_unplug_during_a_solar_pause_polls_once(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await timers.run_until(hass.now + 30)
        controller.tracker.on_reading(hass.now, 50)
        # A cloud in the evening: the charge pauses.
        await _charger(controller, hass, 0)
        await timers.run_until(hass.now + 300)
        assert controller.mode is Mode.PARKED
        assert controller._cancel_charge_end is not None
        polls = hass.polls()
        # Then the car is unplugged: the unplugged poll reads the SoC, and the paused charge
        # is over, so no "charge ended" poll follows 20 min later.
        old = hass.data[CABLE]
        hass.set(CABLE, "off")
        await controller._async_state_changed(_changed(CABLE, old, hass.data[CABLE]))
        await timers.run_until(hass.now + 30 * 60)
        assert hass.polls() == polls + 1
        assert timers.live() == []

    asyncio.run(run())


def test_charge_tick_left_running_while_off_does_not_poll(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await timers.run_until(hass.now + 30)
        # As if a path turned it off without stopping the charge check.
        controller.enabled = False
        polls = hass.polls()
        await timers.run_until(hass.now + 3600)
        assert len(timers.live(repeating=True)) == 1
        assert hass.polls() == polls

    asyncio.run(run())


def test_each_guard_holds_after_unload(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await controller.async_stop()
        # The mode is left as it was, so a late handler still sees charging.
        assert controller.mode is Mode.CHARGING
        hass.now += 3600
        calls = len(hass.calls)
        asked: list[str] = []

        async def spy(name: str, *_args, **_kwargs) -> bool:
            asked.append(name)
            return True

        await controller._async_update_entity(TRACKER)
        assert len(hass.calls) == calls
        controller._async_update_entity = partial(spy, "update_entity")
        await controller._async_poll("late")
        controller._async_poll = partial(spy, "poll")
        await controller._async_check_charge()
        assert asked == []
        await controller._async_wake("late wake hint")
        assert not controller._wake.active(hass.now)
        # A late charger reading saying stopped starts no stop delay.
        hass.set(CHARGER, "0", unit_of_measurement="W")
        controller._note_charger_power()
        controller._update_charging()
        assert timers.live() == []
        assert len(hass.calls) == calls

    asyncio.run(run())


def test_turning_on_while_on_keeps_the_charge_session(rig):
    hass, controller, timers = rig

    async def run():
        await _charging(controller)
        await timers.run_until(hass.now + 30)
        controller.tracker.on_reading(hass.now, 50)
        polls = hass.polls()
        hass.now += 120
        # An automation calling switch.turn_on while it is already on.
        await controller.async_set_enabled(True)
        assert controller.tracker._soc == 50
        assert hass.polls() == polls
        assert len(timers.live(repeating=True)) == 1

    asyncio.run(run())


def test_charger_down_from_startup_lets_the_car_decide_when_the_hold_ends(start, monkeypatch):
    hass, controller, timers, run = start
    # Home Assistant's clock moves with the fake one, so the charger's age does too.
    base = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: base + timedelta(seconds=hass.now))
    # The wallbox gateway stays down after a restart while the car really is charging.
    hass.set(CHARGER, "unavailable")
    hass.set(CABLE, "unavailable")
    hass.set(CHARGING_FLAG, "on")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": True})
        assert controller.mode is Mode.PARKED
        await timers.run_until(hass.now + 9 * 60)
        assert controller.mode is Mode.PARKED
        # Nothing else changes, but the hold running out is enough to look again.
        await timers.run_until(hass.now + 2 * 60)
        assert controller.mode is Mode.CHARGING
        await controller.async_stop()
        assert timers.live() == []

    asyncio.run(go())


def test_charger_dropping_out_while_parked_looks_again_when_the_hold_ends(rig, monkeypatch):
    hass, controller, timers = rig
    base = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: base + timedelta(seconds=hass.now))
    # Parked on a paused charge, the car's last reading still saying charging.
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CHARGING_FLAG, "on")

    async def run():
        controller._cable.cable(True, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        assert controller.mode is Mode.PARKED
        # The gateway goes down and stays down.
        hass.set(CHARGER, "unavailable")
        controller._update_charging()
        await controller.async_evaluate("charger gone")
        await timers.run_until(hass.now + 9 * 60)
        assert controller.mode is Mode.PARKED
        await timers.run_until(hass.now + 2 * 60)
        assert controller.mode is Mode.CHARGING

    asyncio.run(run())


def test_restart_with_the_charger_not_added_yet_at_home_does_not_poll(start):
    hass, controller, timers, run = start
    # A real restart: the wallbox entities do not exist yet when Smart Poll starts, then come
    # back as unavailable, then with readings. The car's last reading is from a paused charge.
    hass.data.pop(CHARGER, None)
    hass.data.pop(CABLE, None)
    hass.set(CHARGING_FLAG, "on")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": True})
        assert controller.mode is Mode.PARKED
        assert not controller._charging
        hass.set(CHARGER, "unavailable")
        hass.set(CABLE, "unavailable")
        controller._note_charger_power()
        controller._update_charging()
        await controller.async_evaluate("charger added")
        assert controller.mode is Mode.PARKED
        hass.set(CHARGER, "0", unit_of_measurement="W")
        hass.set(CABLE, "on")
        controller._note_charger_power()
        controller._update_charging()
        await controller.async_evaluate("charger back")
        await timers.run_until(hass.now + 1800)
        assert controller.mode is Mode.PARKED
        assert hass.polls() == 0

    asyncio.run(go())


def test_same_reading_back_after_unavailable_is_not_a_new_reading(rig):
    hass, controller, timers = rig
    # Away at a public charger, with the car's charge polls already bringing nothing new.
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        hass.now += 60
        hass.set(CHARGING_FLAG, "on")
        await _reading(controller, hass, "2026-11-14T08:01:00+00:00")
        assert controller.mode is Mode.CHARGING
        assert controller._stale_charge_polls == 1
        fresh_at, after = controller._fresh_at, controller._charge_poll_after
        checked: list[float] = []
        controller._check_charger = lambda soc, power: checked.append(soc)
        # A BYD reload: the entities go unavailable, then the same reading comes back.
        hass.now += 30
        await _reading(controller, hass, "unavailable")
        await _reading(controller, hass, "2026-11-14T08:01:00+00:00")
        assert controller._stale_charge_polls == 1
        assert controller._fresh_at == fresh_at
        assert controller._charge_poll_after == after
        assert checked == []

    asyncio.run(run())


def test_reading_there_at_start_back_after_unavailable_is_not_a_new_reading(start):
    hass, controller, timers, run = start
    # BYD Vehicle was loaded before Smart Poll (a reload of ours, or a slow start).
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": True})
        readings: list[float] = []

        async def reading() -> None:
            readings.append(hass.now)

        controller._async_car_reading = reading
        await _reading(controller, hass, "unavailable")
        await _reading(controller, hass, "2026-11-14T08:00:00+00:00")
        assert readings == []
        await _reading(controller, hass, "2026-11-14T08:05:00+00:00")
        assert len(readings) == 1

    asyncio.run(go())


def test_same_soc_back_after_unavailable_is_not_a_new_reading_without_timestamp(rig):
    hass, controller, timers = rig
    controller.byd = replace(controller.byd, last_updated=None)

    async def run():
        await controller.async_evaluate("startup")
        readings: list[float] = []

        async def reading() -> None:
            readings.append(hass.now)

        controller._async_car_reading = reading
        old = hass.data[SOC]
        hass.set(SOC, "unavailable")
        await controller._async_state_changed(_changed(SOC, old, hass.data[SOC]))
        old = hass.data[SOC]
        hass.set(SOC, "50")
        await controller._async_state_changed(_changed(SOC, old, hass.data[SOC]))
        assert readings == []
        old = hass.data[SOC]
        hass.set(SOC, "51")
        await controller._async_state_changed(_changed(SOC, old, hass.data[SOC]))
        assert len(readings) == 1

    asyncio.run(run())


def test_restart_during_a_public_charge_keeps_charging_when_the_charger_comes_back(start):
    hass, controller, timers, run = start
    # Away at a public charger: the home wallbox comes back after Home Assistant, idle.
    hass.set(CHARGER, "unavailable")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(CHARGING_FLAG, "on")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": False})
        assert controller.mode is Mode.CHARGING
        await timers.run_until(hass.now + 20)
        hass.set(CHARGER, "0", unit_of_measurement="W")
        controller._note_charger_power()
        controller._update_charging()
        await controller.async_evaluate("charger back")
        await timers.run_until(hass.now + 10 * 60)
        assert controller.mode is Mode.CHARGING

    asyncio.run(go())


def test_restart_during_a_public_charge_with_the_charger_idle_keeps_charging(start):
    hass, controller, timers, run = start
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(CHARGING_FLAG, "on")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": False})
        assert controller.mode is Mode.CHARGING
        await timers.run_until(hass.now + 10 * 60)
        assert controller.mode is Mode.CHARGING

    asyncio.run(go())


def test_home_charger_going_idle_still_ignores_the_older_car_reading(rig):
    hass, controller, timers = rig
    # Charged at home, unplugged and driven off unseen: the car's last reading says charging.
    hass.set(CHARGING_FLAG, "on")

    async def run():
        await _charging(controller)
        hass.set(TRACKER, "not_home")
        old = hass.data[CABLE]
        hass.set(CABLE, "off")
        await _charger(controller, hass, 0)
        await controller._async_state_changed(_changed(CABLE, old, hass.data[CABLE]))
        await timers.run_until(hass.now + 10 * 60)
        assert controller.mode is not Mode.CHARGING

    asyncio.run(run())


def test_car_data_charge_with_no_power_goes_stale(rig):
    hass, controller, timers = rig
    # Away, the home charger idle, the car's flag on with no battery power (no such sensor).
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        hass.now += 60

        async def fresh(domain, service, data):
            # The session's first poll brings a fresh reading, then the car sleeps.
            if service == "update_entity" and data["entity_id"] == TELEMETRY:
                await _reading(controller, hass, "2026-11-14T08:02:00+00:00")
            else:
                hass.during_call = fresh

        hass.during_call = fresh
        hass.set(CHARGING_FLAG, "on")
        await _reading(controller, hass, "2026-11-14T08:01:00+00:00")
        assert controller.mode is Mode.CHARGING
        assert controller._stale_charge_polls == 0
        await timers.run_until(hass.now + 12 * 3600)
        assert controller.mode is Mode.PARKED
        assert hass.polls() <= 3
        # Kept for a restart, until a newer reading comes.
        kept = controller._stored["stale_charge"]
        assert kept["reading"] == "2026-11-14T08:02:00+00:00" and kept["polls"] >= 1
        hass.set(CHARGING_FLAG, "off")
        await _reading(controller, hass, "2026-11-14T20:00:00+00:00")
        assert "stale_charge" not in controller._stored

    asyncio.run(run())


def test_restart_with_a_stale_charge_flag_away_does_not_charge_again(start):
    hass, controller, timers, run = start
    # Away, unplugged after a public charge, the car asleep with its last upload still saying
    # charging. A charge poll before the restart already found that reading stale.
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(CHARGING_FLAG, "on")
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")
    stale = {
        "reading": "2026-11-14T08:00:00+00:00",
        "fresh": dt_util.utcnow().timestamp() - 3600,
        "polls": 1,
    }

    async def go():
        await run(
            {"originals": {"telemetry": 300, "gps": 300}, "home": False, "stale_charge": stale}
        )
        assert controller.mode is Mode.PARKED
        await timers.run_until(hass.now + 3600)
        assert controller.mode is Mode.PARKED
        assert hass.polls() == 0

    asyncio.run(go())


def test_changing_energy_per_percent_in_the_settings_is_used(start):
    hass, controller, timers, run = start

    async def go():
        # Learned 0.7 starting from 0.6; the user then types 0.8.
        controller._kwh_per_pct_override = 0.8
        stored = {
            "originals": {"telemetry": 300, "gps": 300},
            "kwh_per_pct": 0.7,
            "kwh_per_pct_setting": 0.6,
        }
        await run(stored)
        assert controller.kwh_per_pct == pytest.approx(0.8)
        assert controller._stored["kwh_per_pct_override"] == pytest.approx(0.8)
        assert "kwh_per_pct_setting" not in controller._stored
        assert controller._stored["kwh_per_pct_below"] == pytest.approx(0.8)
        assert controller._stored["kwh_per_pct_above"] == pytest.approx(0.8)
        # Kept for older versions, as the lower band.
        assert controller._stored["kwh_per_pct"] == pytest.approx(0.8)

    asyncio.run(go())


def test_learned_energy_per_percent_kept_from_an_older_store(start):
    hass, controller, timers, run = start

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "kwh_per_pct": 0.7})
        assert controller.kwh_per_pct == pytest.approx(0.7)
        # From now on a typed setting is noticed.
        assert controller._stored["kwh_per_pct_override"] is None

    asyncio.run(go())


def test_learned_energy_per_percent_kept_when_an_early_store_has_no_setting(start):
    hass, controller, timers, run = start
    # An early 0.2.0 kept no setting key; the typed 0.75 was already there when it learned.
    controller._kwh_per_pct_override = 0.75

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "kwh_per_pct": 0.93})
        assert controller.kwh_per_pct_bands == Bands(0.93, 0.93, 80.0)

    asyncio.run(go())


def test_one_learned_value_from_before_the_bands_seeds_both(start):
    hass, controller, timers, run = start

    async def go():
        stored = {
            "originals": {"telemetry": 300, "gps": 300},
            "kwh_per_pct": 0.93,
            "kwh_per_pct_setting": 0.8,
        }
        await run(stored)
        assert controller.kwh_per_pct_bands == Bands(0.93, 0.93, 80.0)
        assert controller._stored["kwh_per_pct_below"] == pytest.approx(0.93)
        assert controller._stored["kwh_per_pct_above"] == pytest.approx(0.93)
        assert controller._stored["kwh_per_pct"] == pytest.approx(0.93)

    asyncio.run(go())


def test_one_value_learned_after_a_downgrade_replaces_the_lower_band(start):
    hass, controller, timers, run = start

    async def go():
        # Stored with the bands, then an older version learned 0.85 into the one value.
        stored = {
            "originals": {"telemetry": 300, "gps": 300},
            "kwh_per_pct": 0.85,
            "kwh_per_pct_below": 0.9,
            "kwh_per_pct_above": 1.0,
            "kwh_per_pct_setting": 0.8,
        }
        await run(stored)
        assert controller.kwh_per_pct_bands == Bands(0.85, 1.0, 80.0)
        assert controller._stored["kwh_per_pct_below"] == pytest.approx(0.85)
        assert controller._stored["kwh_per_pct_above"] == pytest.approx(1.0)
        assert controller._stored["kwh_per_pct"] == pytest.approx(0.85)

    asyncio.run(go())


def test_energy_per_percent_in_use_follows_the_battery_level(start):
    hass, controller, timers, run = start
    hass.set(CHARGER, "0", unit_of_measurement="W")
    stored = {
        "originals": {"telemetry": 300, "gps": 300},
        "kwh_per_pct_below": 0.9,
        "kwh_per_pct_above": 1.0,
        "kwh_per_pct_setting": 0.8,
    }

    async def go():
        await run(stored)
        assert controller.mode is Mode.PARKED
        hass.set(SOC, "79")
        assert controller.kwh_per_pct == pytest.approx(0.9)
        hass.set(SOC, "80")
        assert controller.kwh_per_pct == pytest.approx(1.0)
        # Both kept as they were: the setting did not change.
        assert controller.kwh_per_pct_bands == Bands(0.9, 1.0, 80.0)

    asyncio.run(go())


def _sensor_module():
    """The sensor platform, which imports the config entry type from the package __init__
    that the test stub package leaves out (as in test_config_flow)."""
    stub = sys.modules["bsp"]
    if not hasattr(stub, "SmartPollConfigEntry"):
        stub.SmartPollConfigEntry = importlib.import_module("bsp.__init__").SmartPollConfigEntry
    return importlib.import_module("bsp.sensor")


def test_energy_per_percent_sensor_shows_the_band_in_use_and_both_bands(start):
    hass, controller, timers, run = start
    hass.set(CHARGER, "0", unit_of_measurement="W")
    stored = {
        "originals": {"telemetry": 300, "gps": 300},
        "kwh_per_pct_below": 0.9,
        "kwh_per_pct_above": 1.0,
        "kwh_per_pct_setting": 0.8,
    }

    async def go():
        await run(stored)
        sensor = _sensor_module().KwhPerPctSensor(controller, "kwh_per_pct")
        assert sensor.extra_state_attributes == {
            "below_band_edge": 0.9,
            "above_band_edge": 1.0,
            "band_edge": 80.0,
        }
        hass.set(SOC, "79")
        assert sensor.native_value == pytest.approx(0.9)
        hass.set(SOC, "81")
        assert sensor.native_value == pytest.approx(1.0)

    asyncio.run(go())


def test_changing_energy_per_percent_restarts_both_bands(start):
    hass, controller, timers, run = start
    stored = {
        "originals": {"telemetry": 300, "gps": 300},
        "kwh_per_pct_below": 0.9,
        "kwh_per_pct_above": 1.0,
        "kwh_per_pct_override": None,
    }
    controller._kwh_per_pct_override = 0.8

    async def go():
        await run(stored)
        assert controller.kwh_per_pct_bands == Bands(0.8, 0.8, 80.0)
        assert controller._stored["kwh_per_pct_above"] == pytest.approx(0.8)

    asyncio.run(go())


@pytest.mark.parametrize(
    "stored",
    [
        # Upgraded from 0.2.0: one learned value, the field's 0.6 left alone.
        {"kwh_per_pct": 0.88, "kwh_per_pct_setting": 0.6},
        # Both bands learned on a beta that kept the configured value.
        {
            "kwh_per_pct": 0.88,
            "kwh_per_pct_below": 0.88,
            "kwh_per_pct_above": 1.02,
            "kwh_per_pct_setting": 0.6,
        },
        # Both bands learned on this version, no setting typed.
        {
            "kwh_per_pct": 0.88,
            "kwh_per_pct_below": 0.88,
            "kwh_per_pct_above": 1.02,
            "kwh_per_pct_override": None,
        },
    ],
)
def test_battery_size_entered_after_the_upgrade_keeps_what_was_learned(start, stored):
    hass, controller, timers, run = start
    # 82.5 kWh entered under Configure: a new starting guess of about 0.92 kWh.
    controller._default_kwh_per_pct = 0.9167

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, **stored})
        above = stored.get("kwh_per_pct_above", 0.88)
        assert controller.kwh_per_pct_bands == Bands(0.88, above, 80.0)
        assert controller._stored["kwh_per_pct_below"] == pytest.approx(0.88)
        assert controller._stored["kwh_per_pct_override"] is None
        # Kept on the next start too.
        await controller.async_stop()
        await run(dict(controller._store.data))
        assert controller.kwh_per_pct_bands == Bands(0.88, above, 80.0)

    asyncio.run(go())


def test_energy_per_percent_typed_after_the_upgrade_starts_again(start):
    hass, controller, timers, run = start
    controller._default_kwh_per_pct = 0.95
    controller._kwh_per_pct_override = 0.95
    stored = {"kwh_per_pct": 0.88, "kwh_per_pct_setting": 0.6}

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, **stored})
        assert controller.kwh_per_pct_bands == Bands(0.95, 0.95, 80.0)
        # Learning then goes on from it: the same setting on the next start keeps it.
        controller._store_kwh_per_pct(Bands(0.9, 1.0, 80.0))
        await controller.async_stop()
        await run(dict(controller._store.data))
        assert controller.kwh_per_pct_bands == Bands(0.9, 1.0, 80.0)

    asyncio.run(go())


def test_energy_per_percent_typed_in_0_2_0_is_not_taken_as_new(start):
    hass, controller, timers, run = start
    # 0.9 typed in 0.2.0 stays in the options (only the untouched 0.6 is dropped).
    controller._default_kwh_per_pct = 0.9
    controller._kwh_per_pct_override = 0.9

    async def go():
        await run(
            {
                "originals": {"telemetry": 300, "gps": 300},
                "kwh_per_pct": 0.86,
                "kwh_per_pct_setting": 0.9,
            }
        )
        assert controller.kwh_per_pct_bands == Bands(0.86, 0.86, 80.0)

    asyncio.run(go())


def test_band_edge_from_the_settings(rig):
    hass, controller, timers = rig
    controller = ctl_mod.SmartPollController(
        hass,
        "entry",
        controller.byd,
        Settings(),
        charger_power=None,
        charger_connected=None,
        wake_hints=[],
        car_bluetooth=None,
        phone_bluetooth=[],
        kwh_per_pct=0.8,
        band_edge=90,
    )
    assert controller.kwh_per_pct_bands.edge == 90


@pytest.mark.parametrize(
    "learned",
    [{}, {"kwh_per_pct_below": 0.9, "kwh_per_pct_above": 1.0, "kwh_per_pct_setting": 0.8}],
)
def test_band_edge_from_the_settings_is_kept_by_start(start, learned):
    hass, rig_controller, timers, _run = start
    controller = ctl_mod.SmartPollController(
        hass,
        "entry",
        rig_controller.byd,
        Settings(),
        charger_power=CHARGER,
        charger_connected=CABLE,
        wake_hints=[],
        car_bluetooth=None,
        phone_bluetooth=[],
        kwh_per_pct=0.8,
        band_edge=90,
    )
    controller.issues.async_check = lambda: None
    controller.issues.async_stop = lambda: None
    controller.issues.async_start = lambda: None
    hass.set(CHARGER, "0", unit_of_measurement="W")

    async def go():
        controller._store.data = {"originals": {"telemetry": 300, "gps": 300}, **learned}
        await controller.async_start()
        # The tracker async_start makes, not only the constructor's.
        assert controller.kwh_per_pct_bands.edge == 90
        sensor = _sensor_module().KwhPerPctSensor(controller, "kwh_per_pct")
        assert sensor.extra_state_attributes["band_edge"] == 90
        await controller.async_stop()

    asyncio.run(go())


def test_unplug_after_a_stale_charge_poll_does_not_show_stale_data(rig):
    hass, controller, timers = rig
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def run():
        await _charging(controller)
        # The last poll of a solar charge brought nothing new.
        hass.now += 61
        assert await _poll_brings_the_same(controller, hass)
        assert controller.stale_refreshes == 1
        # The owner unplugs to drive before the cloud reading has moved: the unplugged poll
        # brings the same reading back, but it is not a charge poll.
        hass.now += 61
        polls = hass.polls()

        async def same(domain, service, data):
            hass.write_again(
                LAST_UPDATED, hass.data[LAST_UPDATED].last_reported + timedelta(seconds=1)
            )

        hass.during_call = same
        hass.set(CHARGER, "0", unit_of_measurement="W")
        await _unplug(controller, hass)
        hass.during_call = None
        assert hass.polls() == polls + 1
        assert controller.stale_refreshes == 1
        assert not controller.data_stale

    asyncio.run(run())


def test_solar_pause_with_the_car_drawing_from_its_battery_keeps_the_sign(start):
    hass, controller, timers, run = start
    battery = "sensor.car_battery_power"
    controller.byd = replace(controller.byd, battery_power=battery)
    # At home, plugged in, the Wallbox paused for a cloud while the car's flag still says
    # charging and it heats its battery from it.
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CHARGING_FLAG, "on")
    hass.set(battery, "-1.5", unit_of_measurement="kW")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "charging_negative": False})
        assert controller.charger_check.trusted
        await controller._async_car_reading()
        assert controller._stored["charging_negative"] is False
        await controller.async_stop()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("charger", "cable", "stored"),
    [
        # Every Home Assistant restart: the Wallbox's sensors come back after Smart Poll.
        ("unavailable", "unavailable", {}),
        ("unknown", "on", {}),
        # A charger not trusted (found implausible once) still shows the solar pause.
        ("0", "on", {"charger_implausible": CHARGER}),
        # The Wallbox back but its cable sensor not yet: 0 W is still a pause.
        ("0", "unavailable", {}),
        ("0", "unknown", {}),
    ],
)
def test_sign_not_learned_without_a_charger_reading(start, charger, cable, stored):
    hass, controller, timers, run = start
    battery = "sensor.car_battery_power"
    controller.byd = replace(controller.byd, battery_power=battery)
    # At home during a solar pause, the car's flag still on and it heats its battery.
    hass.set(CHARGER, charger, unit_of_measurement="W")
    hass.set(CABLE, cable)
    hass.set(CHARGING_FLAG, "on")
    hass.set(battery, "-1.5", unit_of_measurement="kW")

    async def go():
        await run(
            {
                "originals": {"telemetry": 300, "gps": 300},
                "home": True,
                "charging_negative": False,
                **stored,
            }
        )
        await controller._async_car_reading()
        assert controller._stored["charging_negative"] is False
        await controller.async_stop()

    asyncio.run(go())


@pytest.mark.parametrize(("place", "learned"), [("home", False), ("not_home", True)])
def test_sign_without_a_charger_sensor_learned_only_away(start, place, learned):
    hass, controller, timers, run = start
    battery = "sensor.car_battery_power"
    controller.byd = replace(controller.byd, battery_power=battery)
    controller._charger_power = None
    hass.set(CABLE, "off")
    hass.set(TRACKER, place)
    hass.set(CHARGING_FLAG, "on")
    hass.set(battery, "-7.0", unit_of_measurement="kW")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": place == "home"})
        await controller._async_car_reading()
        assert controller._stored.get("charging_negative") is (True if learned else None)
        await controller.async_stop()

    asyncio.run(go())


PHONE = "sensor.phone_bluetooth_connection"
HINT = "binary_sensor.android_auto"
IN_CAR = ["02:00:00:AA:BB:CC (BYD SEALION 7)"]


async def _plugged_in_parked(controller, hass) -> None:
    """Plugged in at home with the charge done, as on a weekday morning."""
    hass.set(CHARGER, "0", unit_of_measurement="W")
    controller._cable.cable(True, 0.0)
    controller._note_charger_power()
    await controller.async_evaluate("startup")
    assert controller.mode is Mode.PARKED


async def _unplug(controller, hass) -> None:
    old = hass.data[CABLE]
    hass.set(CABLE, "off")
    await controller._async_state_changed(_changed(CABLE, old, hass.data[CABLE]))


def test_unplugged_with_a_phone_in_the_car_expects_a_drive(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]
    hass.set(PHONE, "0", connected_paired_devices=[])

    async def run():
        await _plugged_in_parked(controller, hass)
        # The phone connects while the cable is still in: no wake then.
        old = hass.data[PHONE]
        hass.set(PHONE, "1", connected_paired_devices=IN_CAR)
        await controller._async_state_changed(_changed(PHONE, old, hass.data[PHONE]))
        assert controller.mode is Mode.PARKED
        polls = hass.polls()
        await _unplug(controller, hass)
        assert hass.polls() == polls + 1
        assert controller.mode is Mode.WAKING
        assert controller._current_intervals() == Intervals(60, 28800)

    asyncio.run(run())


def test_unplugged_with_a_wake_hint_on_expects_a_drive(rig):
    hass, controller, timers = rig
    controller._wake_hints = [HINT]
    hass.set(HINT, "off")

    async def run():
        await _plugged_in_parked(controller, hass)
        old = hass.data[HINT]
        hass.set(HINT, "on")
        await controller._async_state_changed(_changed(HINT, old, hass.data[HINT]))
        assert controller.mode is Mode.PARKED
        await _unplug(controller, hass)
        assert controller.mode is Mode.WAKING

    asyncio.run(run())


def test_unplugged_with_no_phone_in_the_car_stays_parked(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]
    hass.set(PHONE, "1", connected_paired_devices=["B0:A3:11:22:33:44 (OPPO Enco Air3)"])

    async def run():
        await _plugged_in_parked(controller, hass)
        await _unplug(controller, hass)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


@pytest.mark.parametrize(
    ("state", "unit"),
    [("7000", "W"), ("7.0", "kW"), ("0.007", "MW"), ("7000", None)],
)
def test_charger_power_in_any_unit_counts_as_charging(rig, state, unit):
    hass, controller, timers = rig
    hass.set(CHARGER, state, **({"unit_of_measurement": unit} if unit else {}))

    async def run():
        await _charging(controller)
        assert controller._charge_power_kw() == pytest.approx(7.0)

    asyncio.run(run())


def test_charger_showing_charging_as_negative_is_negated(rig):
    hass, controller, timers = rig
    # An energy monitor on the charger circuit that counts import as negative.
    hass.set(CHARGER, "-7.2", unit_of_measurement="kW")

    async def run():
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        assert controller.mode is Mode.PARKED
        # A parked poll finds the car charging.
        hass.set(CHARGING_FLAG, "on")
        await _reading(controller, hass, "2026-11-14T08:00:00+00:00")
        assert controller.mode is Mode.CHARGING
        assert controller._charge_power_kw() == pytest.approx(7.2)
        assert controller._stored["charger_negative"] == CHARGER
        # The charge ends: back to idle, not charging.
        await _charger(controller, hass, -0.01)
        await timers.run_until(hass.now + 300)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_charger_negated_is_kept_across_a_restart(start):
    hass, controller, timers, run = start
    hass.set(CHARGER, "-7000", unit_of_measurement="W")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "charger_negative": CHARGER})
        assert controller.mode is Mode.CHARGING

    asyncio.run(go())


def test_negative_charger_after_a_positive_one_is_not_negated(rig):
    hass, controller, timers = rig
    # A house meter: it imported earlier, now it exports solar while the car charges.
    hass.set(CHARGER, "2000", unit_of_measurement="W")

    async def run():
        controller._note_charger_power()
        await _charger(controller, hass, -1500)
        hass.set(CHARGING_FLAG, "on")
        await _reading(controller, hass, "2026-11-14T08:00:00+00:00")
        assert controller._charger_w() == -1500
        assert controller._stored.get("charger_negative") is None

    asyncio.run(run())


def test_only_our_own_polls_that_bring_nothing_count_as_stale(rig):
    hass, controller, timers = rig
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def run():
        await _charging(controller)
        # Rewrites with no new reading from anything else: BYD's own timer, an HVAC
        # reconcile, a car that only pushes its data.
        for _ in range(3):
            hass.now += 600
            await _rewrite(controller, hass)
        assert controller.stale_refreshes == 0
        # A poll the BYD integration puts off (debounced) writes nothing yet.
        hass.now += 61
        assert await controller._async_poll("test", put_off=False)
        assert controller.stale_refreshes == 0
        # Our polls whose refresh brought the old reading back.
        for _ in range(2):
            hass.now += 61
            assert await _poll_brings_the_same(controller, hass)
        assert controller.data_stale
        # A new reading, from a poll or pushed by the car, ends it.
        await _reading(controller, hass, "2026-11-14T08:30:00+00:00")
        assert controller.stale_refreshes == 0

    asyncio.run(run())


def test_polls_while_parked_do_not_count_as_stale(rig):
    hass, controller, timers = rig

    async def run():
        await _parked_unplugged(controller, hass)
        # A sleeping car, or one that only pushes its data, brings back its last reading.
        for _ in range(3):
            hass.now += 61
            assert await _poll_brings_the_same(controller, hass)
        assert not controller.data_stale

    asyncio.run(run())


def test_false_wakes_on_different_days_do_not_add_up_to_stale(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        for _ in range(2):
            # The phone reconnects to the parked, sleeping car, and the faster rate poll
            # brings back its last reading.
            await _phone(controller, hass, [])
            await _phone(controller, hass, IN_CAR)
            assert controller.mode is Mode.WAKING
            hass.now += 61
            assert await _poll_brings_the_same(controller, hass)
            await timers.run_until(hass.now + 3600)
            assert controller.mode is Mode.PARKED
            hass.now += 86400
        assert controller.stale_refreshes == 0
        assert not controller.data_stale

    asyncio.run(run())


def test_polls_that_bring_nothing_in_two_charges_are_not_in_a_row(rig):
    hass, controller, timers = rig
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")

    async def run():
        await _charging(controller)
        # The last poll of one charge brings nothing new, and the car goes to sleep.
        hass.now += 61
        assert await _poll_brings_the_same(controller, hass)
        assert controller.stale_refreshes == 1
        await _charger(controller, hass, 0)
        await timers.run_until(hass.now + 3600)
        assert controller.mode is Mode.PARKED
        # The next night's scheduled charge: its first poll finds the car still asleep.
        hass.now += 86400
        await _charger(controller, hass, 7000)
        assert controller.mode is Mode.CHARGING
        hass.now += 61
        assert await _poll_brings_the_same(controller, hass)
        assert not controller.data_stale
        # A second one in the same charge is.
        hass.now += 61
        assert await _poll_brings_the_same(controller, hass)
        assert controller.data_stale

    asyncio.run(run())


def test_old_part_of_the_car_name_matches_only_until_upgraded(rig):
    # Saved before whole names were needed, and setup could not turn it into a MAC yet.
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION"
    controller._phone_bluetooth = [PHONE]
    hass.set(PHONE, "1", connected_paired_devices=IN_CAR)
    controller._car_bluetooth_partial = True
    assert controller._phone_connected(controller._state(PHONE))
    controller._car_bluetooth_partial = False
    assert not controller._phone_connected(controller._state(PHONE))


def test_charging_flag_back_from_unavailable_is_not_new_car_data(rig):
    hass, controller, timers = rig

    async def flag(state: str) -> None:
        old = hass.data[CHARGING_FLAG]
        hass.set(CHARGING_FLAG, state)
        await controller._async_state_changed(
            _changed(CHARGING_FLAG, old, hass.data[CHARGING_FLAG])
        )

    async def run():
        await controller.async_evaluate("startup")
        await flag("on")
        changed_at = controller._car_charge_data_at
        assert changed_at == hass.now
        hass.now += 600
        # A BYD reload: unavailable, then the same value back.
        await flag("unavailable")
        await flag("on")
        assert controller._car_charge_data_at == changed_at

    asyncio.run(run())


def test_wake_hint_turning_off_is_not_a_wake(rig):
    hass, controller, timers = rig
    controller._wake_hints = ["input_boolean.car_connected"]
    hass.set(CABLE, "off")
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set("input_boolean.car_connected", "on")

    async def hint(state: str) -> None:
        entity_id = "input_boolean.car_connected"
        old = hass.data[entity_id]
        hass.set(entity_id, state)
        await controller._async_state_changed(_changed(entity_id, old, hass.data[entity_id]))

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        await hint("off")
        assert controller.mode is Mode.PARKED
        await hint("on")
        assert controller.mode is Mode.WAKING

    asyncio.run(run())


def test_charger_not_this_car_is_stored(rig):
    hass, controller, timers = rig
    # A charger showing power while the car, at home with the cable out, says not charging.
    hass.set(CHARGER, "2000", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(SOC, "80")

    async def run():
        controller._cable.cable(False, 0.0)
        await _charging(controller)
        for minutes in (60, 61):
            hass.now = 10_000.0 + minutes * 60
            await controller._async_car_reading()
        assert controller.charger_check.implausible
        assert controller._stored["charger_implausible"] == CHARGER
        assert controller.issues.charger_implausible

    asyncio.run(run())


def test_charger_check_uses_the_energy_per_percent_of_the_battery_level(rig):
    hass, controller, timers = rig
    hass.set(CHARGER, "2000", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(SOC, "80")
    # An 82 kWh pack: about 0.92 kWh per 1% from the band edge (80%) up.
    controller.tracker = ChargeTracker(Bands(0.6, 0.92))

    async def run():
        controller._cable.cable(False, 0.0)
        await _charging(controller)
        # 1.5 kWh with no rise: two percent's worth at 0.6, not at 0.92.
        for minutes in (45, 46):
            hass.now = 10_000.0 + minutes * 60
            await controller._async_car_reading()
        assert not controller.charger_check.implausible
        for minutes in (60, 61):
            hass.now = 10_000.0 + minutes * 60
            await controller._async_car_reading()
        assert controller.charger_check.implausible

    asyncio.run(run())


def test_charger_not_this_car_is_kept_across_a_restart(start):
    hass, controller, timers, run = start
    hass.set(CHARGER, "2000", unit_of_measurement="W")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "charger_implausible": CHARGER})
        assert controller.charger_check.implausible
        assert controller.issues.charger_implausible
        # Not trusted, so its power alone is not charging.
        assert controller.mode is Mode.PARKED

    asyncio.run(go())


def test_plugging_in_ends_expecting_a_drive(rig):
    hass, controller, timers = rig
    hass.set(CABLE, "off")
    hass.set(CHARGER, "0", unit_of_measurement="W")

    async def run():
        controller._cable.cable(False, 0.0)
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        # A short trip home: the phone connected at the start, then the car is plugged in
        # to wait for its charge schedule.
        await controller._async_wake("phone connected")
        assert controller.mode is Mode.WAKING
        hass.now += 120
        old = hass.data[CABLE]
        hass.set(CABLE, "on")
        await controller._async_state_changed(_changed(CABLE, old, hass.data[CABLE]))
        assert controller.mode is Mode.PARKED
        assert controller._current_intervals() == PARKED
        assert controller._cancel_wake is None

    asyncio.run(run())


async def _parked_unplugged(controller, hass) -> None:
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(LAST_UPDATED, "2026-11-14T08:00:00+00:00")
    controller._cable.cable(False, 0.0)
    controller._note_charger_power()
    await controller.async_evaluate("startup")
    assert controller.mode is Mode.PARKED


async def _phone(controller, hass, devices: list[str]) -> None:
    old = hass.data.get(PHONE)
    hass.set(PHONE, str(len(devices)), connected_paired_devices=devices)
    await controller._async_state_changed(_changed(PHONE, old, hass.data[PHONE]))


def test_false_wake_ends_after_two_polls_with_the_car_off(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        # The phone reconnects to the parked car (walking past it, say).
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        assert controller.mode is Mode.WAKING
        # The driving rate polls find the car off: a new reading, then the asleep car's
        # reading brought back. The first minutes do not count (see WAKE_OFF_MIN_AGE). The
        # phone drops off again as its owner walks on.
        hass.now += 60
        await _phone(controller, hass, [])
        await _reading(controller, hass, "2026-11-14T08:05:00+00:00")
        for _ in range(2):
            hass.now += 60
            await _rewrite(controller, hass)
            assert controller.mode is Mode.WAKING
        hass.now += 60
        await _rewrite(controller, hass)
        assert controller.mode is Mode.PARKED
        assert controller._current_intervals() == Settings().intervals(Mode.PARKED)
        assert controller._cancel_wake is None
        # The driver comes back a few minutes later: the phone connects again.
        hass.now += 300
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        assert controller.mode is Mode.WAKING
        hass.now += 60
        hass.set(VEHICLE_ON, "on")
        await _reading(controller, hass, "2026-11-14T08:12:00+00:00")
        assert controller.mode is Mode.DRIVING

    asyncio.run(run())


def test_phone_still_in_the_car_keeps_expecting_a_drive(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        # The phone connects at unlock, and the driver sits for 8 min (navigation, a call)
        # before turning the car on. BYD's driving rate polls keep finding it off.
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        assert controller.mode is Mode.WAKING
        for _ in range(8):
            hass.now += 60
            await _rewrite(controller, hass)
            assert controller.mode is Mode.WAKING
        # Turned on: seen at the next 60 s poll, not the next parked one an hour later.
        hass.now += 60
        hass.set(VEHICLE_ON, "on")
        await _reading(controller, hass, "2026-11-14T08:09:00+00:00")
        assert controller.mode is Mode.DRIVING

    asyncio.run(run())


def test_phone_left_connected_to_a_parked_car_still_ends_at_the_wake_window(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        start = hass.now
        # The phone stays connected to the parked car (left inside, or in range on the drive)
        # and the car never turns on: the 60 s polls must not go on for hours.
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        assert controller.mode is Mode.WAKING
        for _ in range(9):
            hass.now += 60
            await _rewrite(controller, hass)
        assert controller.mode is Mode.WAKING
        await timers.run_until(start + controller.settings.wake_window + 120)
        assert controller.mode is Mode.PARKED
        assert controller._current_intervals() == Settings().intervals(Mode.PARKED)
        # Later readings, the phone still connected, keep it parked.
        for _ in range(30):
            hass.now += 60
            await _rewrite(controller, hass)
            assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_phone_leaving_the_car_ends_expecting_a_drive_at_the_next_poll(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        for _ in range(5):
            hass.now += 60
            await _rewrite(controller, hass)
        assert controller.mode is Mode.WAKING
        # The driver gets out without driving: the first poll after that ends it.
        hass.now += 30
        await _phone(controller, hass, [])
        assert controller.mode is Mode.WAKING
        hass.now += 30
        await _rewrite(controller, hass)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def _hint_change(controller, hass, entity_id: str, state: str):
    old = hass.data.get(entity_id)
    hass.set(entity_id, state)
    return controller._async_state_changed(_changed(entity_id, old, hass.data[entity_id]))


def test_wake_hint_still_on_keeps_expecting_a_drive(rig):
    hass, controller, timers = rig
    controller._wake_hints = [HINT]
    hass.set(HINT, "off")

    async def run():
        await _parked_unplugged(controller, hass)
        # Android Auto connects at unlock and stays on while the driver sits.
        await _hint_change(controller, hass, HINT, "on")
        assert controller.mode is Mode.WAKING
        for _ in range(8):
            hass.now += 60
            await _rewrite(controller, hass)
            assert controller.mode is Mode.WAKING
        # Once it goes off, the next poll that finds the car off ends the window.
        await _hint_change(controller, hass, HINT, "off")
        hass.now += 60
        await _rewrite(controller, hass)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


@pytest.mark.parametrize(
    ("hint", "states"),
    [
        # A hint that was only on for a moment (a short reconnect to the parked car).
        (HINT, ("off", "on", "off")),
        # A button press says nothing about who is in the car afterwards.
        (
            "input_button.going_out",
            ("2026-11-14T07:00:00+00:00", "2026-11-14T08:00:00+00:00"),
        ),
    ],
)
def test_wake_hint_no_longer_on_still_ends_early(rig, hint, states):
    hass, controller, timers = rig
    controller._wake_hints = [hint]
    hass.set(hint, states[0])

    async def run():
        await _parked_unplugged(controller, hass)
        for state in states[1:]:
            await _hint_change(controller, hass, hint, state)
        assert controller.mode is Mode.WAKING
        for _ in range(3):
            hass.now += 60
            await _rewrite(controller, hass)
            assert controller.mode is Mode.WAKING
        hass.now += 60
        await _rewrite(controller, hass)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_another_hint_on_does_not_hold_a_window_it_did_not_start(rig):
    hass, controller, timers = rig
    other = "binary_sensor.other_car_android_auto"
    controller._wake_hints = [HINT, other]
    hass.set(HINT, "off")
    # Stuck on from a different car, since before.
    hass.set(other, "on")

    async def run():
        await _parked_unplugged(controller, hass)
        await _hint_change(controller, hass, HINT, "on")
        await _hint_change(controller, hass, HINT, "off")
        assert controller.mode is Mode.WAKING
        for _ in range(4):
            hass.now += 60
            await _rewrite(controller, hass)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_unplug_and_go_keeps_expecting_a_drive_with_the_car_off(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]
    hass.set(PHONE, "1", connected_paired_devices=IN_CAR)
    controller._phone_in_car[PHONE] = True
    hass.set(LAST_UPDATED, "2026-11-14T07:00:00+00:00")

    async def run():
        await _plugged_in_parked(controller, hass)
        await _unplug(controller, hass)
        assert controller.mode is Mode.WAKING
        # Stowing the cable and loading the car takes a few minutes. Off readings well past
        # the point where a hint's window would end early must not end this one.
        for minute in range(1, 8):
            hass.now += 60
            await _reading(controller, hass, f"2026-11-14T07:0{minute}:00+00:00")
            assert controller.mode is Mode.WAKING
        hass.now += 60
        hass.set(VEHICLE_ON, "on")
        await _reading(controller, hass, "2026-11-14T07:08:00+00:00")
        assert controller.mode is Mode.DRIVING

    asyncio.run(run())


def test_longer_wake_window_is_not_ended_at_four_minutes(rig):
    hass, controller, timers = rig
    controller.settings = replace(controller.settings, wake_window=1200)
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        # Set to 20 min to cover a long wait (navigation, a passenger) before driving off.
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        assert controller.mode is Mode.WAKING
        # BYD's own refreshes at the driving rate keep bringing the asleep car's reading.
        for _ in range(5):
            hass.now += 60
            await _rewrite(controller, hass)
            assert controller.mode is Mode.WAKING
        hass.now += 60
        hass.set(VEHICLE_ON, "on")
        await _reading(controller, hass, "2026-11-14T08:06:00+00:00")
        assert controller.mode is Mode.DRIVING

    asyncio.run(run())


def test_wake_hint_after_unplug_and_go_keeps_the_full_window(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]
    controller._wake_hints = [HINT]
    hass.set(HINT, "off")
    hass.set(PHONE, "1", connected_paired_devices=IN_CAR)
    controller._phone_in_car[PHONE] = True
    hass.set(LAST_UPDATED, "2026-11-14T07:00:00+00:00")

    async def run():
        await _plugged_in_parked(controller, hass)
        start = hass.now
        await _unplug(controller, hass)
        assert controller.mode is Mode.WAKING
        # Wireless Android Auto finishes connecting 45 s after the unplug.
        hass.now += 45
        old = hass.data[HINT]
        hass.set(HINT, "on")
        await controller._async_state_changed(_changed(HINT, old, hass.data[HINT]))
        assert controller.mode is Mode.WAKING
        while hass.now < start + 300:
            hass.now += 60
            await _rewrite(controller, hass)
            assert controller.mode is Mode.WAKING
        # It still ends: at the end of the window started by the hint.
        await timers.run_until(start + 45 + 601)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


@pytest.mark.parametrize(
    ("hint", "states", "wakes"),
    [
        # A count of the phone's Bluetooth devices: a headset connecting is no wake.
        ("sensor.phone_bluetooth_connection", ("1", "2", "1"), False),
        # A tracker updating its place is no wake, nor arriving home (just after a drive).
        ("device_tracker.car_beacon", ("not_home", "Work", "not_home"), False),
        ("device_tracker.car_beacon", ("not_home", "home"), False),
        # A button counts on each press.
        (
            "input_button.going_out",
            ("2026-11-14T07:00:00+00:00", "2026-11-14T08:00:00+00:00"),
            True,
        ),
    ],
)
def test_only_a_change_meaning_connected_or_a_press_is_a_wake(rig, hint, states, wakes):
    hass, controller, timers = rig
    controller._wake_hints = [hint]

    async def run():
        await _parked_unplugged(controller, hass)
        hass.set(hint, states[0])
        for state in states[1:]:
            old = hass.data[hint]
            hass.set(hint, state)
            await controller._async_state_changed(_changed(hint, old, hass.data[hint]))
        assert (controller.mode is Mode.WAKING) is wakes

    asyncio.run(run())


def test_unplugged_with_a_sensor_hint_saying_connected_expects_a_drive(rig):
    hass, controller, timers = rig
    hint = "sensor.carplay"
    controller._wake_hints = [hint]
    hass.set(hint, "Connected")

    async def run():
        await _plugged_in_parked(controller, hass)
        await _unplug(controller, hass)
        assert controller.mode is Mode.WAKING

    asyncio.run(run())


def test_unplugged_with_a_tracker_hint_at_home_stays_parked(rig):
    hass, controller, timers = rig
    hint = "device_tracker.phone"
    controller._wake_hints = [hint]
    hass.set(hint, "home")

    async def run():
        await _plugged_in_parked(controller, hass)
        # A solar or overnight unplug with no drive: the phone is home, as always.
        await _unplug(controller, hass)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_charger_seen_positive_is_kept_across_a_restart(start):
    hass, controller, timers, run = start
    # A house meter that imported before the restart, exporting solar just after it.
    hass.set(CHARGER, "-1500", unit_of_measurement="W")
    hass.set(CHARGING_FLAG, "on")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "charger_positive": CHARGER})
        controller._check_charger_sign()
        assert controller._charger_w() == -1500
        assert controller._stored.get("charger_negative") is None

    asyncio.run(go())


def test_charger_seen_positive_is_stored(rig):
    hass, controller, timers = rig
    hass.set(CHARGER, "2000", unit_of_measurement="W")

    async def run():
        controller._note_charger_power()
        assert controller._stored["charger_positive"] == CHARGER

    asyncio.run(run())


def test_car_turned_on_minutes_after_the_phone_connects_is_a_drive(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        # Unlocked: the phone and Android Auto connect, the driver sets up navigation.
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        assert controller.mode is Mode.WAKING
        # The car is still off at 1 and 2 min: fresh readings, then a repeat.
        hass.now += 60
        await _reading(controller, hass, "2026-11-14T08:05:00+00:00")
        hass.now += 60
        await _rewrite(controller, hass)
        assert controller.mode is Mode.WAKING
        # Turned on at 3 min, with no new phone event.
        hass.now += 60
        hass.set(VEHICLE_ON, "on")
        await _reading(controller, hass, "2026-11-14T08:08:00+00:00")
        assert controller.mode is Mode.DRIVING

    asyncio.run(run())


def test_repeat_just_after_our_interval_write_is_not_an_off_poll(rig):
    hass, controller, timers = rig
    controller._car_bluetooth = "BYD SEALION 7"
    controller._phone_bluetooth = [PHONE]

    async def run():
        await _parked_unplugged(controller, hass)
        await _phone(controller, hass, [])
        await _phone(controller, hass, IN_CAR)
        hass.now += 300
        # Setting an interval rewrites every BYD entity: not a poll of the car.
        old = hass.data[TELEMETRY]
        hass.set(TELEMETRY, "120.0")
        await controller._async_interval_changed(_changed(TELEMETRY, old, hass.data[TELEMETRY]))
        await controller._async_reading_repeated(None)
        assert controller._wake.off_polls == 0
        hass.now += 60
        await _rewrite(controller, hass)
        assert controller._wake.off_polls == 1

    asyncio.run(run())


@pytest.mark.parametrize(("threshold", "mode"), [(300, Mode.CHARGING), (1000, Mode.PARKED)])
def test_charging_threshold_from_the_settings(rig, threshold, mode):
    hass, controller, timers = rig
    controller.settings = replace(controller.settings, charging_min_power_w=threshold)
    hass.set(CHARGER, "700", unit_of_measurement="W")

    async def run():
        controller._note_charger_power()
        controller._update_charging()
        await controller.async_evaluate("startup")
        assert controller.mode is mode

    asyncio.run(run())


def test_charging_stop_delay_from_the_settings(rig):
    hass, controller, timers = rig
    controller.settings = replace(controller.settings, charging_stop_delay=600)

    async def run():
        await _charging(controller)
        start = hass.now
        await _charger(controller, hass, 0)
        # Past the default 120 s, still charging.
        await timers.run_until(start + 300)
        assert controller.mode is Mode.CHARGING
        await timers.run_until(start + 601)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def test_wake_window_from_the_settings(rig):
    hass, controller, timers = rig
    controller.settings = replace(controller.settings, wake_window=1200)
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")

    async def run():
        controller._note_charger_power()
        await controller.async_evaluate("startup")
        start = hass.now
        await controller._async_wake("test")
        assert controller.mode is Mode.WAKING
        assert controller._wake.until == start + 1200
        # Past the default 10 minutes, still expecting a drive, also when something else
        # (the charger's power, every few seconds) has it decide again.
        await timers.run_until(start + 700)
        await controller.async_evaluate("tick")
        assert controller.mode is Mode.WAKING
        await timers.run_until(start + 1202)
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


@pytest.mark.parametrize("sign", [1, -1])
def test_public_charge_is_timed_whatever_sign_the_car_gives_its_power(start, sign):
    hass, controller, timers, run = start
    battery = "sensor.car_battery_power"
    controller.byd = replace(controller.byd, battery_power=battery)
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(CHARGING_FLAG, "on")
    hass.set(battery, str(sign * 7.0), unit_of_measurement="kW")

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "home": False})
        assert controller.mode is Mode.CHARGING
        assert controller.tracker.power_kw == pytest.approx(7.0)
        # A reading learns the car's sign and keeps it.
        await controller._async_car_reading()
        assert controller._stored["charging_negative"] is (sign < 0)
        assert controller.tracker.power_kw == pytest.approx(7.0)
        # Then drawing from the battery while it still says charging (heating the cabin at a
        # slow charger) adds nothing, whichever way round the car shows it.
        hass.set(battery, str(-sign * 0.3), unit_of_measurement="kW")
        await controller._async_car_reading()
        assert controller._stored["charging_negative"] is (sign < 0)
        assert controller._charge_power_kw() == 0.0
        await controller.async_stop()

    asyncio.run(go())


def test_charging_sign_is_kept_across_a_restart(start):
    hass, controller, timers, run = start
    battery = "sensor.car_battery_power"
    controller.byd = replace(controller.byd, battery_power=battery)
    hass.set(CHARGER, "0", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    hass.set(CHARGING_FLAG, "on")
    # A positive-convention car drawing from its battery while it says charging.
    hass.set(battery, "-0.3", unit_of_measurement="kW")

    async def go():
        stored = {"originals": {"telemetry": 300, "gps": 300}, "home": False}
        await run({**stored, "charging_negative": False})
        assert controller._charge_power_kw() == 0.0
        await controller.async_stop()

    asyncio.run(go())


def test_awake_share_caps_a_parked_reading_at_the_longest_parked_interval(start, monkeypatch):
    hass, controller, timers, run = start
    base = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: base)
    now = base.timestamp()
    hour = 3600.0
    controller.settings = replace(
        controller.settings, parked_telemetry=int(2 * hour), parked_home_telemetry=int(3 * hour)
    )
    # Asleep, awake, then asleep with nothing since for 10 h: the last reading stands for the
    # longest parked interval (3 h), not 10 h, and the awake one for AWAKE_HOLD.
    samples = [
        [now - 12 * hour, 80, 0.0, True],
        [now - 11 * hour, 80, -300.0, True],
        [now - 10 * hour, 80, 0.0, True],
    ]

    async def go():
        await run({"originals": {"telemetry": 300, "gps": 300}, "samples": samples})
        assert controller.awake_share() == pytest.approx(
            AWAKE_HOLD / (hour + AWAKE_HOLD + 3 * hour)
        )
        await controller.async_stop()

    asyncio.run(go())


# ---------- charging threshold from the settings, everywhere it is used ----------
# A 1000 W threshold, and a charger showing 600 W: standby on a house meter, say.


def _threshold_1000(rig):
    hass, controller, timers = rig
    controller.settings = replace(controller.settings, charging_min_power_w=1000)
    return hass, controller, timers


def test_charger_below_the_threshold_does_not_turn_the_sign_back(rig):
    hass, controller, timers = _threshold_1000(rig)
    controller._charger_sign = ChargerSign(negative=True)
    hass.set(CHARGER, "600", unit_of_measurement="W")
    controller._note_charger_power()
    assert controller._charger_sign.negative
    assert not controller._charger_sign.seen_positive


def test_charger_below_the_threshold_is_idle(rig):
    hass, controller, timers = _threshold_1000(rig)
    hass.set(CHARGER, "7000", unit_of_measurement="W")
    controller._note_charger_power()
    assert controller._charger_idle is False
    hass.now += 60
    hass.set(CHARGER, "600", unit_of_measurement="W")
    controller._note_charger_power()
    assert controller._charger_idle is True
    assert controller._charger_idle_at == hass.now


def test_charger_below_the_threshold_is_not_checked(rig):
    hass, controller, timers = _threshold_1000(rig)
    # Charging away from home, with the home charger showing standby.
    hass.set(CHARGER, "600", unit_of_measurement="W")
    hass.set(CHARGING_FLAG, "on")
    hass.set(CABLE, "off")
    hass.set(TRACKER, "not_home")
    readings = []

    async def run():
        controller._cable.cable(False, 0.0)
        await _charging(controller)
        controller.charger_check.reading = lambda *args, **kwargs: readings.append(kwargs)
        controller._check_charger(50.0, None)
        assert readings == []

    asyncio.run(run())


def test_charger_below_the_threshold_is_not_the_charge_power(rig):
    hass, controller, timers = _threshold_1000(rig)
    battery = "sensor.car_battery_power"
    controller.byd = replace(controller.byd, battery_power=battery)
    # A public charge: the home charger shows standby, the cable is not in at home.
    hass.set(CHARGER, "600", unit_of_measurement="W")
    hass.set(CABLE, "off")
    hass.set(CHARGING_FLAG, "on")
    hass.set(battery, "7.0", unit_of_measurement="kW")
    controller._cable.cable(False, 0.0)
    assert controller._charge_power_kw() == pytest.approx(7.0)


def test_charging_on_a_charger_below_the_threshold_is_not_charger_charged(rig):
    hass, controller, timers = _threshold_1000(rig)
    hass.set(CHARGER, "600", unit_of_measurement="W")
    controller._set_charging(True)
    assert controller._charger_charged is False
    hass.set(CHARGER, "1500", unit_of_measurement="W")
    controller._set_charging(True)
    assert controller._charger_charged is True
