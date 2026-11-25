"""The controller glue, driven with a small fake of Home Assistant's states and services.

Only what the interval writes, the "car on" keep and the cable check touch is faked, so these
cover the wiring between the pure logic and the BYD entities, not Home Assistant itself.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from bsp import controller as ctl_mod  # noqa: E402
from bsp.byd_entities import BydEntities  # noqa: E402
from bsp.modes import Intervals, Mode, Settings  # noqa: E402
from homeassistant.core import State  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

TELEMETRY = "number.car_poll_interval"
GPS = "number.car_gps_poll_interval"
VEHICLE_ON = "binary_sensor.car_vehicle_on"
SOC = "sensor.car_soc"
ODOMETER = "sensor.car_odometer"
LAST_UPDATED = "sensor.car_last_updated"
CABLE = "binary_sensor.wallbox_cable"

PARKED = Intervals(3600, 28800)
DRIVING = Intervals(60, 120)


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, State] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.states = SimpleNamespace(get=self.data.get)
        self.services = SimpleNamespace(async_call=self._call)
        self.loop = SimpleNamespace(time=lambda: 10_000.0)

    def set(self, entity_id: str, state: str, **attributes) -> None:
        old = self.data.get(entity_id)
        if old is not None and old.state == state and old.attributes == attributes:
            self.write_again(entity_id)
            return
        self.data[entity_id] = State(entity_id, state, attributes)

    def write_again(self, entity_id: str, at: datetime | None = None) -> None:
        """An unchanged write, as StateMachine.async_set_internal does it.

        No new State: last_reported moves on the one already stored, so a State kept across
        an await is the same object as the one read after it.
        """
        old = self.data[entity_id]
        old.last_reported = at or dt_util.utcnow()
        old._cache["last_reported_timestamp"] = old.last_reported.timestamp()

    async def _call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, dict(data)))
        if (domain, service) == ("number", "set_value"):
            # Like Home Assistant: an unavailable number is skipped without an error.
            state = self.data.get(data["entity_id"])
            if state is not None and state.state != "unavailable":
                self.set(data["entity_id"], str(float(data["value"])))

    def writes(self) -> list[tuple[str, float]]:
        return [(d["entity_id"], d["value"]) for dom, s, d in self.calls if s == "set_value"]

    def polls(self) -> int:
        return sum(1 for _, s, _ in self.calls if s == "update_entity")


@pytest.fixture
def setup(monkeypatch):
    later: list[float] = []

    def fake_call_later(_hass, delay, _action):
        later.append(delay)
        return lambda: None

    monkeypatch.setattr(ctl_mod, "async_call_later", fake_call_later)
    monkeypatch.setattr(ctl_mod, "Store", lambda *args, **kwargs: None)
    hass = FakeHass()
    byd = BydEntities(
        vin="VIN",
        car_name="Car",
        telemetry_interval=TELEMETRY,
        gps_interval=GPS,
        vehicle_on=VEHICLE_ON,
        soc=SOC,
        odometer=ODOMETER,
        last_updated=LAST_UPDATED,
    )
    controller = ctl_mod.SmartPollController(
        hass,
        "entry",
        byd,
        Settings(),
        charger_power=None,
        charger_connected=CABLE,
        wake_hints=[],
        car_bluetooth=None,
        phone_bluetooth=[],
        kwh_per_pct=0.8,
    )
    controller._save = lambda: None
    controller.issues.async_check = lambda: None
    controller._stored = {"originals": {"telemetry": 300, "gps": 300}}
    hass.set(TELEMETRY, "3600.0")
    hass.set(GPS, "28800.0")
    hass.set(VEHICLE_ON, "off")
    return hass, controller, later


def _event(entity_id: str, old: State | None) -> SimpleNamespace:
    return SimpleNamespace(data={"entity_id": entity_id, "old_state": old, "new_state": None})


def _numbers(hass: FakeHass, state: str) -> list[State | None]:
    old = [hass.data.get(TELEMETRY), hass.data.get(GPS)]
    hass.set(TELEMETRY, state)
    hass.set(GPS, state)
    return old


def test_byd_reload_mid_drive_writes_again_when_numbers_come_back(setup):
    hass, controller, _ = setup

    async def run():
        await controller.async_evaluate("startup")
        assert controller._current_intervals() == PARKED
        # The car turns on while BYD reloads: the numbers are unavailable, the write is lost.
        _numbers(hass, "unavailable")
        hass.set(VEHICLE_ON, "on")
        await controller.async_evaluate("vehicle on")
        assert controller.mode is Mode.DRIVING
        assert controller._writes.applied is None
        # They come back with the value BYD had saved, not the driving rate.
        old = _numbers(hass, "3600.0")
        hass.set(GPS, "28800.0")
        await controller._async_interval_changed(_event(TELEMETRY, old[0]))
        assert controller._current_intervals() == DRIVING
        assert controller._writes.applied == DRIVING

    asyncio.run(run())


def test_startup_write_that_needed_a_retry_polls_for_the_faster_rate(setup):
    hass, controller, _ = setup

    async def run():
        hass.set(VEHICLE_ON, "on")
        old = _numbers(hass, "unavailable")
        await controller.async_evaluate("startup")
        old = _numbers(hass, "3600.0")
        hass.set(GPS, "28800.0")
        await controller._async_interval_changed(_event(TELEMETRY, old[0]))
        assert controller._current_intervals() == DRIVING
        # BYD's startup refresh set its timer from the 3600 s it had saved. Before 0.0.93
        # only a poll restarts it, or a drive during a restart is seen once an hour.
        assert hass.polls() == 1

    asyncio.run(run())


def test_turning_off_during_a_reload_restores_when_numbers_come_back(setup):
    hass, controller, _ = setup

    async def run():
        hass.set(VEHICLE_ON, "on")
        await controller.async_evaluate("startup")
        assert controller._current_intervals() == DRIVING
        _numbers(hass, "unavailable")
        await controller.async_set_enabled(False)
        assert controller._restore_pending
        old = _numbers(hass, "60.0")
        hass.set(GPS, "120.0")
        await controller._async_interval_changed(_event(TELEMETRY, old[0]))
        assert controller._current_intervals() == Intervals(300, 300)
        assert not controller._restore_pending
        # A value set by hand afterwards is left alone.
        before = len(hass.writes())
        old = [hass.data[TELEMETRY]]
        hass.set(TELEMETRY, "900.0")
        await controller._async_interval_changed(_event(TELEMETRY, old[0]))
        assert len(hass.writes()) == before

    asyncio.run(run())


def test_off_with_numbers_changed_by_hand_is_not_fought(setup):
    hass, controller, _ = setup

    async def run():
        await controller.async_evaluate("startup")
        await controller.async_set_enabled(False)
        before = len(hass.writes())
        old = [hass.data[TELEMETRY]]
        hass.set(TELEMETRY, "120.0")
        await controller._async_interval_changed(_event(TELEMETRY, old[0]))
        assert len(hass.writes()) == before

    asyncio.run(run())


def test_unknown_car_on_keeps_driving_and_looks_again_later(setup):
    hass, controller, later = setup

    async def run():
        hass.set(VEHICLE_ON, "on")
        await controller.async_evaluate("startup")
        hass.set(VEHICLE_ON, "unavailable")
        await controller.async_evaluate("reload")
        assert controller.mode is Mode.DRIVING
        assert len(later) == 1 and later[0] > 30 * 60 - 5
        # Once the keep has run out, it counts as off.
        state = hass.data[VEHICLE_ON]
        hass.data[VEHICLE_ON] = State(
            VEHICLE_ON, "unavailable", last_changed=state.last_changed - timedelta(hours=1)
        )
        await controller._async_keep_ended(dt_util.utcnow())
        assert controller.mode is Mode.PARKED

    asyncio.run(run())


def _cable_event(hass: FakeHass, state: str) -> SimpleNamespace:
    old = hass.data.get(CABLE)
    hass.set(CABLE, state)
    return SimpleNamespace(
        data={"entity_id": CABLE, "old_state": old, "new_state": hass.data[CABLE]}
    )


def test_cable_on_while_the_car_drives_is_ignored_and_remembered(setup):
    hass, controller, _ = setup
    clock = [1_000_000.0]
    controller._epoch = lambda: clock[0]

    async def reading(minutes: float, odometer: float, on: bool) -> None:
        clock[0] = 1_000_000.0 + minutes * 60
        hass.set(VEHICLE_ON, "on" if on else "off")
        hass.set(ODOMETER, str(odometer))
        hass.set(SOC, "80")
        # The car's clock is an hour off: only Home Assistant's clock is used for the check.
        hass.set(LAST_UPDATED, dt_util.utc_from_timestamp(clock[0] + 3600).isoformat())
        await controller._async_car_reading()

    async def run():
        hass.set(CABLE, "on")
        controller._cable.cable(True, clock[0] - 3600)
        assert not controller._wake_allowed()
        # Moving with the car off between readings (it is never on for 15 min).
        await reading(1, 12345, False)
        await reading(5, 12346, False)
        await reading(8, 12350, False)
        # Inside the grace for a late unplug report: still believed.
        assert not controller._wake_allowed()
        await reading(16, 12360, False)
        assert controller._wake_allowed()
        assert controller.issues.cable_implausible
        assert controller._stored["cable_implausible"] == CABLE
        # The cable turns off: believed again, and no longer remembered.
        await controller._async_state_changed(_cable_event(hass, "off"))
        assert "cable_implausible" not in controller._stored
        assert not controller._cable.implausible
        assert not controller.issues.cable_implausible

    asyncio.run(run())


def test_car_on_while_charging_does_not_doubt_the_cable(setup):
    hass, controller, _ = setup
    clock = [1_000_000.0]
    controller._epoch = lambda: clock[0]
    controller._charging_signal = lambda: True

    async def run():
        hass.set(CABLE, "on")
        controller._cable.cable(True, clock[0] - 3600)
        for minute in range(0, 25, 5):
            clock[0] = 1_000_000.0 + minute * 60
            hass.set(VEHICLE_ON, "on")
            hass.set(ODOMETER, "12345")
            await controller._async_car_reading()
        assert not controller._cable.implausible
        assert not controller._wake_allowed()

    asyncio.run(run())


class FakeStore:
    def __init__(self, data: dict) -> None:
        self.data = data

    async def async_load(self) -> dict:
        return self.data

    async def async_save(self, data: dict) -> None:
        self.data = data


@pytest.fixture
def started(setup, monkeypatch):
    """Runs async_start with the given stored data, recording what it subscribes to."""
    hass, controller, _ = setup
    subscribed: list[list[str]] = []

    def track(_hass, entity_ids, _action):
        subscribed.append(list(entity_ids))
        return lambda: None

    monkeypatch.setattr(ctl_mod, "async_track_state_change_event", track)
    monkeypatch.setattr(ctl_mod, "async_track_state_report_event", track)
    controller.issues.async_start = lambda: None

    async def start(stored: dict, configured: str | None = CABLE) -> None:
        controller._charger_connected = configured
        controller._store = FakeStore(stored)
        await controller.async_start()

    return hass, controller, start, subscribed


def test_start_watches_the_interval_numbers(started):
    _, controller, start, subscribed = started
    asyncio.run(start({"originals": {"telemetry": 300, "gps": 300}}))
    assert [TELEMETRY, GPS] in subscribed
    assert controller._writes.applied == PARKED


def test_start_keeps_the_cable_doubt_for_the_same_sensor(started):
    hass, controller, start, _ = started
    hass.set(CABLE, "on")
    asyncio.run(start({"originals": {"telemetry": 300, "gps": 300}, "cable_implausible": CABLE}))
    assert controller._cable.implausible
    assert controller.issues.cable_implausible
    assert controller._wake_allowed()


def test_start_drops_the_cable_doubt_for_another_sensor(started):
    hass, controller, start, _ = started
    hass.set("binary_sensor.other_cable", "on")
    stored = {"originals": {"telemetry": 300, "gps": 300}, "cable_implausible": CABLE}
    asyncio.run(start(stored, configured="binary_sensor.other_cable"))
    assert not controller._cable.implausible
    assert not controller.issues.cable_implausible


def test_start_drops_the_cable_doubt_when_it_turned_off_meanwhile(started):
    hass, controller, start, _ = started
    hass.set(CABLE, "off")
    stored = {"originals": {"telemetry": 300, "gps": 300}, "cable_implausible": CABLE}
    asyncio.run(start(stored))
    assert not controller._cable.implausible
    assert not controller.issues.cable_implausible
    assert "cable_implausible" not in controller._stored


def test_restore_pending_survives_a_restart(started):
    hass, controller, start, _ = started

    async def run():
        # Off during a BYD outage: the restore did not take, and that is saved.
        _numbers(hass, "unavailable")
        controller.enabled = True
        await controller.async_set_enabled(False)
        assert controller._stored["restore_pending"] is True
        stored = dict(controller._stored)
        # After a restart BYD is back with its saved driving rate.
        controller._restore_pending = False
        hass.set(TELEMETRY, "60.0")
        hass.set(GPS, "120.0")
        await start(stored)
        assert controller._current_intervals() == Intervals(300, 300)
        assert not controller._restore_pending
        assert "restore_pending" not in controller._stored

    asyncio.run(run())


def test_restore_pending_after_a_restart_waits_for_the_numbers(started):
    hass, controller, start, _ = started

    async def run():
        _numbers(hass, "unavailable")
        stored = {"originals": {"telemetry": 300, "gps": 300}, "enabled": False}
        stored["restore_pending"] = True
        await start(stored)
        assert controller._restore_pending
        old = _numbers(hass, "60.0")
        hass.set(GPS, "120.0")
        await controller._async_interval_changed(_event(TELEMETRY, old[0]))
        assert controller._current_intervals() == Intervals(300, 300)
        assert not controller._restore_pending

    asyncio.run(run())


def test_another_writer_setting_the_numbers_is_not_fought_for_good(setup):
    hass, controller, _ = setup

    async def run():
        hass.set(VEHICLE_ON, "on")
        await controller.async_evaluate("startup")
        assert controller._current_intervals() == DRIVING
        # An old automation sets the telemetry rate back each time it changes.
        rounds = 0
        while hass.data[TELEMETRY].state != "300.0" and rounds < 10:
            rounds += 1
            old = hass.data[TELEMETRY]
            hass.set(TELEMETRY, "300.0")
            await controller._async_interval_changed(_event(TELEMETRY, old))
        assert rounds == 3
        assert controller._writes.gave_up

    asyncio.run(run())


def test_signals_in_use_needs_a_usable_state(setup):
    hass, controller, _ = setup
    # Both exist, but the cable sensor is not back yet and the odometer has no number.
    hass.set(CABLE, "unavailable")
    hass.set(ODOMETER, "unknown")
    assert controller.signals_in_use() == []
    hass.set(CABLE, "off")
    hass.set(ODOMETER, "12345.6")
    assert controller.signals_in_use() == ["cable connected", "odometer check"]


def test_signals_in_use_counts_a_button_never_pressed(setup):
    hass, controller, _ = setup
    button = "input_button.going_out"
    controller.issues.configured = replace(controller.issues.configured, wake_hints=(button,))
    hass.set(CABLE, "unavailable")
    hass.set(ODOMETER, "unknown")
    # Never pressed: unknown, but its first press is a wake.
    hass.set(button, "unknown")
    assert controller.signals_in_use() == ["wake hints (1)"]
    hass.set(button, "unavailable")
    assert controller.signals_in_use() == []
