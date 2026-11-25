"""Putting the BYD poll intervals back when Smart Poll is removed."""

import asyncio
import importlib
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from bsp import removal  # noqa: E402
from bsp.modes import Intervals  # noqa: E402
from test_controller import GPS, TELEMETRY, FakeHass  # noqa: E402

NUMBERS = (TELEMETRY, GPS)
ORIGINALS = Intervals(300, 300)
DEVICE = "device"


@pytest.fixture
def hass(monkeypatch):
    hass = FakeHass()
    hass.listeners = []
    hass.unsubscribed = []

    def track(_hass, entity_ids, action):
        hass.listeners.append(action)
        return lambda: hass.unsubscribed.append(action)

    monkeypatch.setattr(removal, "async_track_state_change_event", track)
    return hass


async def _changed(hass: FakeHass, entity_id: str, state: str) -> None:
    old = hass.data.get(entity_id)
    hass.set(entity_id, state)
    event = SimpleNamespace(
        data={"entity_id": entity_id, "old_state": old, "new_state": hass.data[entity_id]}
    )
    for action in list(hass.listeners):
        await action(event)


def test_restore_takes_straight_away(hass):
    hass.set(TELEMETRY, "3600.0")
    hass.set(GPS, "28800.0")
    assert asyncio.run(removal.async_restore_intervals(hass, NUMBERS, ORIGINALS, key=DEVICE))
    assert hass.writes() == [(TELEMETRY, 300), (GPS, 300)]
    assert hass.listeners == []


def test_unavailable_numbers_warn_and_restore_when_they_come_back(hass, caplog):
    hass.set(TELEMETRY, "unavailable")
    hass.set(GPS, "unavailable")

    async def run():
        with caplog.at_level(logging.WARNING):
            assert not await removal.async_restore_intervals(hass, NUMBERS, ORIGINALS, key=DEVICE)
        # The warning names what to set by hand.
        assert TELEMETRY in caplog.text and "300" in caplog.text
        assert len(hass.listeners) == 1
        # BYD is back with the rates Smart Poll left.
        await _changed(hass, TELEMETRY, "3600.0")
        assert hass.unsubscribed == []
        await _changed(hass, GPS, "28800.0")
        assert hass.data[TELEMETRY].state == "300.0"
        assert hass.data[GPS].state == "300.0"
        assert len(hass.unsubscribed) == 1

    asyncio.run(run())


def test_a_value_set_by_hand_meanwhile_is_left_alone(hass):
    hass.set(TELEMETRY, "unavailable")
    hass.set(GPS, "28800.0")

    async def run():
        assert not await removal.async_restore_intervals(hass, NUMBERS, ORIGINALS, key=DEVICE)
        assert hass.data[GPS].state == "300.0"
        # The user sets GPS by hand while telemetry is still unavailable.
        await _changed(hass, GPS, "600.0")
        # Telemetry coming back is written, and the hand-set GPS kept.
        await _changed(hass, TELEMETRY, "3600.0")
        assert hass.data[TELEMETRY].state == "300.0"
        assert hass.data[GPS].state == "600.0"
        assert len(hass.unsubscribed) == 1

    asyncio.run(run())


def test_a_number_changed_by_hand_while_the_retry_waits_is_not_written(hass, monkeypatch):
    hass.set(TELEMETRY, "3600.0")
    hass.set(GPS, "28800.0")
    calls = []

    async def failing(domain, service, data, blocking=False):
        # BYD's cloud failing: the write raises with both numbers still available.
        calls.append((domain, service, dict(data)))
        raise RuntimeError("BYD cloud error")

    monkeypatch.setattr(hass.services, "async_call", failing)

    async def run():
        assert not await removal.async_restore_intervals(hass, NUMBERS, ORIGINALS, key=DEVICE)
        writes = len(calls)
        # Neither number came back from unavailable: the user changed them.
        await _changed(hass, GPS, "600.0")
        await _changed(hass, TELEMETRY, "900.0")
        assert len(calls) == writes
        assert hass.unsubscribed == []

    asyncio.run(run())


def test_setting_the_car_up_again_stops_the_retry(hass):
    hass.set(TELEMETRY, "unavailable")
    hass.set(GPS, "unavailable")

    async def run():
        assert not await removal.async_restore_intervals(hass, NUMBERS, ORIGINALS, key=DEVICE)
        writes = len(hass.writes())
        removal.cancel_restore_retry(hass, "another car")
        assert hass.unsubscribed == []
        removal.cancel_restore_retry(hass, DEVICE)
        assert len(hass.unsubscribed) == 1
        # The new entry's own target is not written over when the numbers come back.
        await _changed(hass, TELEMETRY, "60.0")
        await _changed(hass, GPS, "120.0")
        assert len(hass.writes()) == writes

    asyncio.run(run())


def test_setup_stops_the_retry_left_by_a_removal(monkeypatch):
    integration = importlib.import_module("bsp.__init__")
    cancelled: list[str] = []
    monkeypatch.setattr(integration, "cancel_restore_retry", lambda _h, key: cancelled.append(key))

    def not_loaded(_hass, _device):
        raise integration.BydEntitiesError("still loading")

    async def version(_hass):
        return "0.0.93"

    monkeypatch.setattr(integration, "resolve", not_loaded)
    monkeypatch.setattr(integration, "async_byd_version", version)
    monkeypatch.setattr(integration, "car_device_id", lambda _h, _vin, device_id: device_id)
    monkeypatch.setattr(integration, "async_setup_failed", lambda *_args: None)
    entry = SimpleNamespace(
        entry_id="entry", unique_id="VIN", title="Car", data={"device_id": DEVICE}
    )
    with pytest.raises(integration.ConfigEntryNotReady):
        asyncio.run(integration.async_setup_entry(None, entry))
    # By VIN, so a BYD device made again for the same car still stops it.
    assert cancelled == ["VIN"]


def test_interval_numbers_found_by_unique_id(monkeypatch):
    from bsp import byd_entities

    ids = {
        "VIN_number_realtime_poll_interval": TELEMETRY,
        "VIN_number_gps_poll_interval": GPS,
    }
    registry = SimpleNamespace(async_get_entity_id=lambda _domain, _platform, uid: ids.get(uid))
    monkeypatch.setattr(byd_entities.er, "async_get", lambda _hass: registry)
    assert byd_entities.interval_entities(None, "VIN") == NUMBERS
    del ids["VIN_number_gps_poll_interval"]
    assert byd_entities.interval_entities(None, "VIN") is None
