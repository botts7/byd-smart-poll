"""The options form: what it saves, and the texts it shows."""

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

# The stub package (see conftest) does not run the integration's __init__, which the flow
# imports from.
sys.modules["bsp"].effective_options = importlib.import_module("bsp.__init__").effective_options

from bsp import config_flow  # noqa: E402
from bsp.config_flow import SmartPollConfigFlow, SmartPollOptionsFlow  # noqa: E402
from bsp.const import (  # noqa: E402
    CONF_BATTERY_KWH,
    CONF_CAR_BLUETOOTH,
    CONF_CAR_BLUETOOTH_PARTIAL,
    CONF_DEVICE_ID,
    CONF_KWH_BAND_EDGE,
    CONF_KWH_PER_PCT,
)

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "byd_smart_poll"


def _intervals(values: dict, user_input: dict | None) -> dict:
    flow = SmartPollOptionsFlow()
    flow.flow_id = "flow"
    flow.handler = "entry"
    flow._values = dict(values)
    return asyncio.run(flow.async_step_intervals(user_input))


def _defaults(result: dict) -> dict:
    return {
        str(key): key.default() if callable(key.default) else None
        for key in result["data_schema"].schema
    }


def test_band_edge_is_in_the_form_with_its_default():
    result = _intervals({}, None)
    assert _defaults(result)[CONF_KWH_BAND_EDGE] == 80.0


def test_band_edge_is_saved():
    result = _intervals({CONF_KWH_PER_PCT: 0.9}, {CONF_KWH_BAND_EDGE: 85})
    assert result["data"][CONF_KWH_BAND_EDGE] == 85


def test_band_edge_from_the_options():
    energy_options = sys.modules["bsp.__init__"].energy_options
    assert energy_options({}) == (0.6, 80.0)
    assert energy_options({CONF_KWH_PER_PCT: 0.9, CONF_KWH_BAND_EDGE: 85}) == (0.9, 85.0)


def test_options_form_leaves_out_the_old_car_name_marker(monkeypatch):
    # A migrated entry whose options were never saved: the form starts from the setup data.
    entry = SimpleNamespace(
        data={
            CONF_DEVICE_ID: "device",
            CONF_CAR_BLUETOOTH: "BYD SEAL",
            CONF_CAR_BLUETOOTH_PARTIAL: "BYD SEAL",
        },
        options={},
    )
    monkeypatch.setattr(SmartPollOptionsFlow, "config_entry", entry)
    flow = SmartPollOptionsFlow()

    async def charging():
        return {"type": "form", "step_id": "charging"}

    flow.async_step_charging = charging
    asyncio.run(flow.async_step_init())
    assert flow._values == {CONF_CAR_BLUETOOTH: "BYD SEAL"}


def test_strings_and_english_translation_match():
    strings = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))
    assert strings == english


def test_strings_have_nothing_hassfest_reads_as_html():
    # hassfest rejects any <...> in a string, eg a placeholder like sensor.<phone>_x.
    import re

    def texts(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from texts(value)
        elif isinstance(node, str):
            yield node

    strings = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
    assert [t for t in texts(strings) if re.search(r"<[^>]*>", t)] == []


# ---------- battery size and energy per 1% ----------


def test_battery_size_is_the_first_guess():
    energy_options = sys.modules["bsp.__init__"].energy_options
    assert energy_options({CONF_BATTERY_KWH: 82.5})[0] == pytest.approx(0.9167, abs=1e-4)
    # The energy per 1% field overrides it.
    assert energy_options({CONF_BATTERY_KWH: 82.5, CONF_KWH_PER_PCT: 0.85})[0] == 0.85


def test_battery_size_takes_a_plug_in_hybrid_pack():
    result = _intervals({}, None)
    field = next(
        value for key, value in result["data_schema"].schema.items() if key == CONF_BATTERY_KWH
    )
    # 8.3 kWh in some DM-i models.
    assert field.config["min"] <= 8.3


def test_cleared_energy_per_percent_falls_back_to_the_battery_size():
    values = {CONF_KWH_PER_PCT: 0.85, CONF_BATTERY_KWH: 60}
    result = _intervals(values, {CONF_KWH_BAND_EDGE: 80})
    assert CONF_KWH_PER_PCT not in result["data"]
    assert CONF_BATTERY_KWH not in result["data"]
    result = _intervals(values, {CONF_KWH_BAND_EDGE: 80, CONF_BATTERY_KWH: 82.5})
    assert result["data"][CONF_BATTERY_KWH] == 82.5
    assert CONF_KWH_PER_PCT not in result["data"]


def test_energy_fields_show_what_is_set():
    result = _intervals({CONF_BATTERY_KWH: 82.5}, None)
    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested[CONF_BATTERY_KWH] == 82.5
    # Empty, not 0.6: a value there would override the battery size.
    assert suggested[CONF_KWH_PER_PCT] is None


def test_setup_keeps_the_battery_size(monkeypatch):
    flow = SmartPollConfigFlow()
    flow.hass = SimpleNamespace()
    monkeypatch.setattr(config_flow, "device_vin", lambda _hass, _device: "LGXCE4CB0S0123456")
    monkeypatch.setattr(config_flow, "resolve", lambda _hass, _device: None)

    async def byd_version(_hass):
        return "0.0.93"

    monkeypatch.setattr(config_flow, "async_byd_version", byd_version)

    async def set_unique_id(_vin):
        return None

    async def charging():
        return {"step_id": "charging"}

    flow.async_set_unique_id = set_unique_id
    flow._abort_if_unique_id_configured = lambda: None
    flow.async_step_charging = charging
    asyncio.run(flow.async_step_user({CONF_DEVICE_ID: "dev", CONF_BATTERY_KWH: 82.5}))
    assert flow._values == {CONF_DEVICE_ID: "dev", CONF_BATTERY_KWH: 82.5}


def test_old_untouched_energy_per_percent_is_dropped():
    migrate = sys.modules["bsp.__init__"].migrate_options
    assert migrate({CONF_KWH_PER_PCT: 0.6, "x": 1}, 1) == {"x": 1}
    # Typed in: kept as an override.
    assert migrate({CONF_KWH_PER_PCT: 0.82}, 1) == {CONF_KWH_PER_PCT: 0.82}
    # 1.2 was a beta that still always saved it.
    assert migrate({CONF_KWH_PER_PCT: 0.6}, 2) == {}
    # Already migrated: 0.6 typed in since stays.
    assert migrate({CONF_KWH_PER_PCT: 0.6}, 3) == {CONF_KWH_PER_PCT: 0.6}


def test_migrate_entry_updates_the_options_once():
    integration = sys.modules["bsp.__init__"]
    updates: list[dict] = []
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=lambda _e, **kw: updates.append(kw))
    )
    entry = SimpleNamespace(
        version=1, minor_version=1, data={"x": 1}, options={CONF_KWH_PER_PCT: 0.6, "y": 2}
    )
    assert asyncio.run(integration.async_migrate_entry(hass, entry))
    assert updates == [{"data": {"x": 1}, "options": {"y": 2}, "minor_version": 3}]
    assert SmartPollConfigFlow.MINOR_VERSION == 3
