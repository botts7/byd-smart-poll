"""Setting the entry up: BYD Vehicle version, finding the car by VIN, and Repairs on failure.

Also the config flow checks and the options that become Settings. Home Assistant's
registries and services are faked with just what each function touches.
"""

import asyncio
import importlib
import sys
from types import SimpleNamespace

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")

from homeassistant.core import State  # noqa: E402
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady  # noqa: E402

integration = importlib.import_module("bsp.__init__")
# config_flow imports from the package itself, which the "bsp" stub does not run.
sys.modules["bsp"].effective_options = integration.effective_options

from bsp import byd_entities, config_flow, issues  # noqa: E402
from bsp.modes import Settings  # noqa: E402

VIN = "LGXC0000000000001"
OLD_DEVICE = "old-device"
NEW_DEVICE = "new-device"


# ---------- BYD Vehicle version ----------


@pytest.mark.parametrize(
    ("version", "too_old"),
    [
        ("0.0.87", True),
        ("0.0.64", True),
        ("0.0.88", False),
        # A beta of the minimum is let through, one of an older release is not.
        ("0.0.88b1", False),
        ("0.0.88-beta.1", False),
        ("0.0.88.dev0", False),
        ("0.0.87b1", True),
        ("v0.0.90", False),
        ("0.0.93", False),
        ("0.1.0", False),
        # Unknown or not a version: not blocked.
        (None, False),
        ("", False),
        ("main", False),
    ],
)
def test_version_too_old(version, too_old):
    assert byd_entities.version_too_old(version) is too_old


def _setup_hass(registry_devices: dict[str, str]):
    """A hass with a device registry holding {device_id: vin}, and setup that records."""
    devices = {
        device_id: SimpleNamespace(id=device_id, identifiers={("byd_vehicle", vin)})
        for device_id, vin in registry_devices.items()
    }

    def get_device(identifiers):
        for device in devices.values():
            if device.identifiers & identifiers:
                return device
        return None

    registry = SimpleNamespace(async_get=devices.get, async_get_device=get_device)
    updates: list[dict] = []

    async def forward(_entry, _platforms):
        return None

    hass = SimpleNamespace(
        data={},
        states={},
        config_entries=SimpleNamespace(
            # Records what was changed: {"data": ...}, {"options": ...} or both.
            async_update_entry=lambda _entry, **kw: updates.append(kw),
            async_forward_entry_setups=forward,
        ),
    )
    return hass, registry, updates


@pytest.fixture
def setup_env(monkeypatch):
    """async_setup_entry with fakes for the registry, Repairs and the controller."""
    created: list[tuple[str, str]] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        issues.ir,
        "async_create_issue",
        lambda _h, _d, issue_id, **kw: created.append((issue_id, kw["translation_key"])),
    )
    monkeypatch.setattr(
        issues.ir, "async_delete_issue", lambda _h, _d, issue_id: deleted.append(issue_id)
    )

    async def version(_hass):
        return "0.0.93"

    monkeypatch.setattr(integration, "async_byd_version", version)
    monkeypatch.setattr(integration, "resolve", lambda _h, device_id: device_id)

    controllers: list[dict] = []

    class Controller:
        def __init__(self, _hass, _entry_id, byd, *_args, **kwargs):
            self.byd = byd
            controllers.append(kwargs)

        async def async_start(self):
            return None

    monkeypatch.setattr(integration, "SmartPollController", Controller)
    hass, registry, updates = _setup_hass({NEW_DEVICE: VIN})
    monkeypatch.setattr(byd_entities.dr, "async_get", lambda _hass: registry)

    def entry(device_id=OLD_DEVICE, data=None, options=None):
        return SimpleNamespace(
            entry_id="entry",
            unique_id=VIN,
            title="BYD Smart Poll 000001",
            data={"device_id": device_id, **(data or {})},
            options=options or {},
            async_on_unload=lambda _f: None,
            add_update_listener=lambda _f: None,
        )

    return SimpleNamespace(
        hass=hass,
        entry=entry,
        updates=updates,
        created=created,
        deleted=deleted,
        controllers=controllers,
        mp=monkeypatch,
    )


def test_setup_stops_with_an_old_byd_vehicle(setup_env):
    async def old(_hass):
        return "0.0.80"

    setup_env.mp.setattr(integration, "async_byd_version", old)
    issues.async_setup_failed(setup_env.hass, "entry", "Car", "device_not_found")
    with pytest.raises(ConfigEntryError, match=r"0\.0\.88"):
        asyncio.run(integration.async_setup_entry(setup_env.hass, setup_env.entry()))
    # A not-ready issue from earlier tries goes: it would point at the wrong cause.
    assert "entry_setup" in setup_env.deleted
    assert setup_env.hass.data[issues._SETUP_FAILURES] == {}


def test_setup_passes_the_settings_of_both_option_groups(setup_env):
    """No-home-charger and partial car name (setup) next to the energy seed and band edge."""
    options = {"no_home_charger": True, "battery_kwh": 82.5, "kwh_band_edge": 85}
    asyncio.run(
        integration.async_setup_entry(
            setup_env.hass, setup_env.entry(device_id=NEW_DEVICE, options=options)
        )
    )
    kwargs = setup_env.controllers[-1]
    assert kwargs["no_home_charger"] is True
    assert kwargs["car_bluetooth_partial"] is False
    assert kwargs["kwh_per_pct"] == pytest.approx(82.5 / 100 / 0.9, abs=0.01)
    assert kwargs["band_edge"] == 85
    # The battery size is only a starting guess, not a typed energy per 1%.
    assert kwargs["kwh_per_pct_override"] is None


def test_setup_passes_a_typed_energy_per_percent_as_the_override(setup_env):
    options = {"battery_kwh": 82.5, "kwh_per_pct": 0.95}
    asyncio.run(
        integration.async_setup_entry(
            setup_env.hass, setup_env.entry(device_id=NEW_DEVICE, options=options)
        )
    )
    kwargs = setup_env.controllers[-1]
    assert kwargs["kwh_per_pct"] == pytest.approx(0.95)
    assert kwargs["kwh_per_pct_override"] == pytest.approx(0.95)


# ---------- finding the car by VIN ----------


def test_byd_set_up_again_is_found_by_vin_and_the_device_saved(setup_env):
    entry = setup_env.entry(OLD_DEVICE)
    assert asyncio.run(integration.async_setup_entry(setup_env.hass, entry))
    assert entry.runtime_data.byd == NEW_DEVICE
    assert setup_env.updates == [{"data": {"device_id": NEW_DEVICE}}]


def test_same_device_is_not_saved_again(setup_env):
    entry = setup_env.entry(NEW_DEVICE)
    assert asyncio.run(integration.async_setup_entry(setup_env.hass, entry))
    assert setup_env.updates == []


def test_car_device_id_falls_back_to_the_saved_one(monkeypatch):
    _hass, registry, _ = _setup_hass({OLD_DEVICE: VIN})
    monkeypatch.setattr(byd_entities.dr, "async_get", lambda _hass: registry)
    assert byd_entities.car_device_id(None, None, OLD_DEVICE) == OLD_DEVICE
    assert byd_entities.car_device_id(None, "OTHER VIN", OLD_DEVICE) == OLD_DEVICE
    with pytest.raises(byd_entities.BydEntitiesError, match="device_not_found"):
        byd_entities.car_device_id(None, "OTHER VIN", "gone")


def test_car_gone_raises_a_repairs_issue_after_a_few_tries_then_clears(setup_env):
    hass = setup_env.hass
    _h, empty, _ = _setup_hass({})
    setup_env.mp.setattr(byd_entities.dr, "async_get", lambda _hass: empty)
    for _ in range(issues.SETUP_TRIES_BEFORE_ISSUE - 1):
        with pytest.raises(ConfigEntryNotReady):
            asyncio.run(integration.async_setup_entry(hass, setup_env.entry()))
    assert setup_env.created == []
    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(integration.async_setup_entry(hass, setup_env.entry()))
    assert setup_env.created == [("entry_setup", "car_not_found")]

    # BYD Vehicle is added again: set up, and the issue goes.
    _h, back, _ = _setup_hass({NEW_DEVICE: VIN})
    setup_env.mp.setattr(byd_entities.dr, "async_get", lambda _hass: back)
    assert asyncio.run(integration.async_setup_entry(hass, setup_env.entry()))
    assert "entry_setup" in setup_env.deleted
    assert hass.data[issues._SETUP_FAILURES] == {}


def test_removed_or_disabled_entry_forgets_its_setup_failures(setup_env):
    hass = setup_env.hass
    setup_env.mp.setattr(issues.ir, "async_get", lambda _hass: SimpleNamespace(issues={}))
    for _ in range(issues.SETUP_TRIES_BEFORE_ISSUE - 1):
        issues.async_setup_failed(hass, "entry", "Car", "device_not_found")
    issues.async_delete_entry_issues(hass, "entry")
    # Enabled or added again: the count starts over, so one failure raises nothing.
    issues.async_setup_failed(hass, "entry", "Car", "device_not_found")
    assert setup_env.created == []
    assert hass.data[issues._SETUP_FAILURES] == {"entry": 1}


def _entity_registry(monkeypatch, entries):
    registry = SimpleNamespace()
    monkeypatch.setattr(byd_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        byd_entities.er, "async_entries_for_device", lambda *_a, **_k: list(entries)
    )
    monkeypatch.setattr(byd_entities, "device_vin", lambda _h, _d: VIN)
    monkeypatch.setattr(byd_entities, "device_name", lambda _h, _d: "Car")


def _byd_entity(key, disabled=False):
    return SimpleNamespace(
        unique_id=f"{VIN}{byd_entities.BYD_ENTITY_SUFFIXES[key]}",
        entity_id=f"x.{key}",
        platform="byd_vehicle",
        disabled_by="integration" if disabled else None,
    )


def test_resolve_tells_disabled_from_missing(monkeypatch):
    required = ["telemetry_interval", "gps_interval", "vehicle_on", "soc"]
    _entity_registry(monkeypatch, [_byd_entity(k) for k in required])
    assert byd_entities.resolve(None, "device").soc == "x.soc"

    _entity_registry(monkeypatch, [_byd_entity(k, disabled=k == "vehicle_on") for k in required])
    with pytest.raises(byd_entities.BydEntitiesError, match="entities_disabled"):
        byd_entities.resolve(None, "device")

    # Before BYD Vehicle 0.0.65 there are no interval numbers at all.
    _entity_registry(monkeypatch, [_byd_entity(k) for k in ("vehicle_on", "soc")])
    with pytest.raises(byd_entities.BydEntitiesError, match="missing_entities"):
        byd_entities.resolve(None, "device")


# ---------- config flow ----------


def _flow_hass(states: dict[str, State], registry_entries: dict | None = None):
    registry_entries = registry_entries or {}
    in_domain = lambda domain: [e for e in states if e.startswith(f"{domain}.")]  # noqa: E731
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get, async_entity_ids=in_domain))
    return hass, SimpleNamespace(async_get=registry_entries.get, entities=registry_entries)


def test_config_flow_aborts_with_an_old_byd_vehicle(monkeypatch):
    async def old(_hass):
        return "0.0.80"

    monkeypatch.setattr(config_flow, "async_byd_version", old)
    flow = config_flow.SmartPollConfigFlow()
    flow.hass = SimpleNamespace()
    result = asyncio.run(flow.async_step_user())
    assert result["type"] == "abort"
    assert result["reason"] == "byd_too_old"
    assert result["description_placeholders"] == {"version": "0.0.80", "minimum": "0.0.88"}


POWER = "sensor.wallbox_power"


@pytest.mark.parametrize(
    ("attributes", "registry", "ok"),
    [
        ({"unit_of_measurement": "W"}, None, True),
        ({"unit_of_measurement": "kW"}, None, True),
        # No device class, as some template sensors.
        ({"unit_of_measurement": "kW", "device_class": None}, None, True),
        ({"unit_of_measurement": "kWh", "device_class": "energy"}, None, False),
        ({"unit_of_measurement": "A"}, None, False),
        # Unavailable after a restart: the registry knows.
        ({}, SimpleNamespace(unit_of_measurement="kW", device_class=None), True),
        (
            {},
            SimpleNamespace(
                unit_of_measurement=None, device_class=None, original_device_class="energy"
            ),
            False,
        ),
        # Nothing known: let through.
        ({}, None, True),
    ],
)
def test_charger_power_must_be_power(monkeypatch, attributes, registry, ok):
    hass, reg = _flow_hass(
        {POWER: State(POWER, "unavailable" if not attributes else "1.0", attributes)},
        {POWER: registry} if registry else {},
    )
    monkeypatch.setattr(config_flow.er, "async_get", lambda _hass: reg)
    errors = config_flow._charging_errors(hass, {"charger_power": POWER})
    assert errors == ({} if ok else {"charger_power": "not_power"})


def test_charger_power_picker_lists_power_sensors(monkeypatch):
    def registered(entity_id, unit=None, disabled=False):
        return SimpleNamespace(
            entity_id=entity_id,
            domain=entity_id.split(".")[0],
            unit_of_measurement=unit,
            device_class=None,
            original_device_class=None,
            disabled_by="user" if disabled else None,
        )

    states = {
        POWER: State(POWER, "7.0", {"unit_of_measurement": "kW"}),
        "sensor.solar_w": State("sensor.solar_w", "900", {"unit_of_measurement": "W"}),
        "sensor.energy": State("sensor.energy", "1", {"unit_of_measurement": "kWh"}),
        "sensor.outside": State("sensor.outside", "20", {"unit_of_measurement": "°C"}),
        # No unit at all, as some template sensors: cannot be told, so listed.
        "sensor.template": State("sensor.template", "5"),
        "sensor.byd_power": State("sensor.byd_power", "3", {"unit_of_measurement": "kW"}),
        "binary_sensor.cable": State("binary_sensor.cable", "on"),
    }
    registry = {
        # Unavailable after a restart with no state yet: the registry has its unit.
        "sensor.charger_restarting": registered("sensor.charger_restarting", "kW"),
        "sensor.disabled_power": registered("sensor.disabled_power", "W", disabled=True),
    }
    hass, reg = _flow_hass(states, registry)
    monkeypatch.setattr(config_flow.er, "async_get", lambda _hass: reg)
    monkeypatch.setattr(config_flow, "byd_entity_ids", lambda _h, _d: ["sensor.byd_power"])
    assert config_flow._charger_power_choices(hass, None) == [
        "sensor.charger_restarting",
        "sensor.solar_w",
        "sensor.template",
        POWER,
    ]
    # The saved one stays, even when it no longer reads power.
    assert "sensor.energy" in config_flow._charger_power_choices(hass, "sensor.energy")


def test_no_home_charger_with_a_charger_sensor_is_an_error(monkeypatch):
    hass, reg = _flow_hass({POWER: State(POWER, "0", {"unit_of_measurement": "W"})})
    monkeypatch.setattr(config_flow.er, "async_get", lambda _hass: reg)
    assert config_flow._charging_errors(hass, {"no_home_charger": True}) == {}
    assert config_flow._charging_errors(
        hass, {"no_home_charger": True, "charger_power": POWER}
    ) == {"base": "home_charger_and_sensors"}
    assert config_flow._charging_errors(
        hass, {"no_home_charger": True, "charger_connected": "binary_sensor.cable"}
    ) == {"base": "home_charger_and_sensors"}


def test_charging_step_saves_no_home_charger_and_clears_it(monkeypatch):
    hass, reg = _flow_hass({})
    monkeypatch.setattr(config_flow.er, "async_get", lambda _hass: reg)
    flow = config_flow.SmartPollOptionsFlow()
    flow.hass = hass
    flow._values = {}

    async def drivers():
        return {"type": "form", "step_id": "drivers"}

    flow.async_step_drivers = drivers
    asyncio.run(flow.async_step_charging({"no_home_charger": True}))
    assert flow._values == {"no_home_charger": True}
    asyncio.run(flow.async_step_charging({"no_home_charger": False}))
    assert flow._values == {}


def test_charging_step_shows_the_error_with_what_was_entered(monkeypatch):
    hass, reg = _flow_hass({POWER: State(POWER, "0", {"unit_of_measurement": "W"})})
    monkeypatch.setattr(config_flow.er, "async_get", lambda _hass: reg)
    monkeypatch.setattr(config_flow, "byd_entity_ids", lambda _hass, _domain: [])
    flow = config_flow.SmartPollOptionsFlow()
    flow.hass = hass
    flow._values = {"charger_connected": "binary_sensor.old_cable", "phone_bluetooth": ["x"]}
    result = asyncio.run(
        flow.async_step_charging({"charger_power": POWER, "no_home_charger": True})
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "home_charger_and_sensors"}
    # Shown again as entered: the cable cleared in the form stays cleared.
    shown = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
        if str(key) != "no_home_charger"
    }
    assert shown == {"charger_power": POWER, "charger_connected": None}
    defaults = {str(key): key.default for key in result["data_schema"].schema}
    assert defaults["no_home_charger"]() is True
    # Other steps' answers are kept.
    assert flow._values["phone_bluetooth"] == ["x"]


# ---------- options into the controller ----------


def test_no_home_charger_reaches_the_controller(setup_env):
    hass = setup_env.hass
    assert asyncio.run(integration.async_setup_entry(hass, setup_env.entry(NEW_DEVICE)))
    assert setup_env.controllers[-1]["no_home_charger"] is False
    entry = setup_env.entry(NEW_DEVICE, options={"no_home_charger": True})
    assert asyncio.run(integration.async_setup_entry(hass, entry))
    assert setup_env.controllers[-1]["no_home_charger"] is True


SEALION_MAC = "02:00:00:AA:BB:CC"
PHONE = "sensor.phone_bluetooth_connection"


def _phone_paired(hass, *devices):
    hass.states[PHONE] = State(PHONE, "1", {"paired_devices": list(devices)})


def _legacy(setup_env, car, *, in_options=True, phones=(PHONE,)):
    """An entry saved before whole names were needed, as async_migrate_entry marks it."""
    values = {"car_bluetooth": car, "phone_bluetooth": list(phones)}
    if in_options:
        return setup_env.entry(NEW_DEVICE, data={"car_bluetooth_partial": car}, options=values)
    return setup_env.entry(NEW_DEVICE, data={**values, "car_bluetooth_partial": car})


def test_old_part_of_the_car_name_becomes_its_mac(setup_env, caplog):
    # Saved before whole names were needed: "BYD SEALION" for a "BYD SEALION 7".
    hass = setup_env.hass
    _phone_paired(hass, f"{SEALION_MAC} (BYD SEALION 7)", "B0:A3:11:22:33:44 (Buds)")
    entry = _legacy(setup_env, "BYD SEALION", in_options=False)
    with caplog.at_level("INFO"):
        assert asyncio.run(integration.async_setup_entry(hass, entry))
    assert setup_env.controllers[-1]["car_bluetooth"] == SEALION_MAC
    assert setup_env.controllers[-1]["car_bluetooth_partial"] is False
    # Saved as the MAC, and the old-name mark gone.
    assert setup_env.updates == [
        {
            "data": {
                "device_id": NEW_DEVICE,
                "car_bluetooth": SEALION_MAC,
                "phone_bluetooth": [PHONE],
            }
        }
    ]
    assert "saving its MAC" in caplog.text

    # Saved in the options: updated there.
    setup_env.updates.clear()
    entry = _legacy(setup_env, "sealion")
    assert asyncio.run(integration.async_setup_entry(hass, entry))
    assert setup_env.updates == [
        {
            "data": {"device_id": NEW_DEVICE},
            "options": {"car_bluetooth": SEALION_MAC, "phone_bluetooth": [PHONE]},
        }
    ]


def test_old_part_of_the_car_name_matches_as_a_part_until_the_phones_report(setup_env):
    # Just after a restart the phones may not have said what they are paired with.
    entry = _legacy(setup_env, "BYD SEALION")
    assert asyncio.run(integration.async_setup_entry(setup_env.hass, entry))
    assert setup_env.controllers[-1]["car_bluetooth"] == "BYD SEALION"
    assert setup_env.controllers[-1]["car_bluetooth_partial"] is True
    assert setup_env.updates == []


def test_old_part_of_the_car_name_fitting_two_devices_still_matches_as_a_part(setup_env):
    _phone_paired(setup_env.hass, f"{SEALION_MAC} (BYD SEALION 7)", "01:02:03:04:05:06 (BYD Seal)")
    assert asyncio.run(integration.async_setup_entry(setup_env.hass, _legacy(setup_env, "BYD")))
    assert setup_env.controllers[-1]["car_bluetooth_partial"] is True
    assert setup_env.updates == []


def test_old_whole_car_name_only_loses_the_mark(setup_env, caplog):
    # The second phone calls the car "Sealion": that is a whole name too.
    hass = setup_env.hass
    _phone_paired(hass, f"{SEALION_MAC} (BYD SEALION 7)")
    other = "sensor.other_phone"
    hass.states[other] = State(other, "1", {"paired_devices": [f"{SEALION_MAC.lower()} (Sealion)"]})
    entry = _legacy(setup_env, "sealion", phones=(PHONE, other))
    assert asyncio.run(integration.async_setup_entry(hass, entry))
    assert setup_env.controllers[-1]["car_bluetooth"] == "sealion"
    assert setup_env.controllers[-1]["car_bluetooth_partial"] is False
    assert setup_env.updates == [{"data": {"device_id": NEW_DEVICE}, "options": entry.options}]
    assert "sealion" not in caplog.text


def test_old_car_name_that_fits_no_paired_device_is_logged_once(setup_env, caplog):
    hass = setup_env.hass
    _phone_paired(hass, f"{SEALION_MAC} (BYD SEALION 7)")
    entry = _legacy(setup_env, "Kia")
    assert asyncio.run(integration.async_setup_entry(hass, entry))
    assert "'Kia' is not part of the name of any device" in caplog.text
    assert setup_env.controllers[-1]["car_bluetooth_partial"] is False
    # The mark goes, so the next setup does not log it again.
    assert setup_env.updates == [{"data": {"device_id": NEW_DEVICE}, "options": entry.options}]


def test_old_car_name_changed_in_the_options_since_is_a_whole_name(setup_env):
    _phone_paired(setup_env.hass, f"{SEALION_MAC} (BYD SEALION 7)")
    entry = setup_env.entry(
        NEW_DEVICE,
        data={"car_bluetooth_partial": "BYD SEALION"},
        options={"car_bluetooth": "BYD Seal", "phone_bluetooth": [PHONE]},
    )
    assert asyncio.run(integration.async_setup_entry(setup_env.hass, entry))
    assert setup_env.controllers[-1]["car_bluetooth"] == "BYD Seal"
    assert setup_env.controllers[-1]["car_bluetooth_partial"] is False
    assert setup_env.updates == [{"data": {"device_id": NEW_DEVICE}, "options": entry.options}]


@pytest.mark.parametrize("paired", [True, False])
def test_new_whole_car_name_is_never_rewritten(setup_env, caplog, paired):
    # Two BYDs at home: the Seal driver typed "BYD Seal", the other phone has only the
    # Sealion paired. Not marked as old, so "BYD Seal" must not become the Sealion's MAC.
    hass = setup_env.hass
    if paired:
        _phone_paired(hass, f"{SEALION_MAC} (BYD SEALION 7)")
    options = {"car_bluetooth": "BYD Seal", "phone_bluetooth": [PHONE, "sensor.seal_phone"]}
    entry = setup_env.entry(NEW_DEVICE, options=options)
    assert asyncio.run(integration.async_setup_entry(hass, entry))
    assert setup_env.controllers[-1]["car_bluetooth"] == "BYD Seal"
    assert setup_env.controllers[-1]["car_bluetooth_partial"] is False
    assert setup_env.updates == []
    assert "BYD Seal" not in caplog.text


# ---------- config entry migration ----------


def _migrate(data=None, options=None, version=1, minor_version=1):
    updates: list[dict] = []
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=lambda _e, **kw: updates.append(kw))
    )
    entry = SimpleNamespace(
        version=version,
        minor_version=minor_version,
        data={"device_id": NEW_DEVICE, **(data or {})},
        options=options or {},
    )
    return asyncio.run(integration.async_migrate_entry(hass, entry)), updates


def test_migration_marks_an_old_typed_car_name():
    ok, updates = _migrate(data={"car_bluetooth": " BYD SEALION "})
    assert ok
    assert updates == [
        {
            "data": {
                "device_id": NEW_DEVICE,
                "car_bluetooth": " BYD SEALION ",
                "car_bluetooth_partial": "BYD SEALION",
            },
            "options": {},
            "minor_version": 3,
        }
    ]
    # Read from the options once they have been saved.
    ok, updates = _migrate(options={"car_bluetooth": "sealion"})
    assert updates[0]["data"]["car_bluetooth_partial"] == "sealion"


@pytest.mark.parametrize("car", ["", SEALION_MAC.lower()])
def test_migration_leaves_a_mac_or_no_name_unmarked(car):
    ok, updates = _migrate(data={"car_bluetooth": car})
    assert ok
    assert updates == [
        {
            "data": {"device_id": NEW_DEVICE, "car_bluetooth": car},
            "options": {},
            "minor_version": 3,
        }
    ]


def test_migration_of_a_saved_1_1_entry_makes_both_changes_at_once():
    # Like the owner's: options saved from the 1.1 form, which always saved 0.6 kWh.
    options = {
        "car_bluetooth": "BYD SEALION",
        "phone_bluetooth": ["sensor.phone_bluetooth_connection"],
        "charger_power": "sensor.wallbox_power",
        "charger_connected": "binary_sensor.wallbox_cable",
        "parked_telemetry": 3600,
        "kwh_per_pct": 0.6,
    }
    ok, updates = _migrate(options=options)
    assert ok
    assert len(updates) == 1
    assert updates[0]["minor_version"] == 3
    assert updates[0]["data"] == {"device_id": NEW_DEVICE, "car_bluetooth_partial": "BYD SEALION"}
    assert updates[0]["options"] == {k: v for k, v in options.items() if k != "kwh_per_pct"}
    # A typed-in energy per 1% stays.
    ok, updates = _migrate(options={**options, "kwh_per_pct": 0.78})
    assert updates[0]["options"]["kwh_per_pct"] == 0.78


@pytest.mark.parametrize(
    ("data", "options"),
    [
        # From the beta that only had whole car names: the default 0.6 is still dropped.
        ({}, {"car_bluetooth": SEALION_MAC, "kwh_per_pct": 0.6}),
        # From the beta that only had the battery size: the name is still marked.
        ({}, {"car_bluetooth": "BYD SEALION"}),
    ],
)
def test_migration_from_either_1_2_beta_finishes_the_other_change(data, options):
    ok, updates = _migrate(data=data, options=options, minor_version=2)
    assert ok
    assert updates[0]["minor_version"] == 3
    assert "kwh_per_pct" not in updates[0]["options"]
    car = options["car_bluetooth"]
    assert updates[0]["data"].get("car_bluetooth_partial") == (None if car == SEALION_MAC else car)


def test_migration_from_a_newer_version_fails_and_a_current_one_is_left():
    assert _migrate(version=2) == (False, [])
    assert _migrate(minor_version=3, data={"car_bluetooth": "BYD"}) == (True, [])


def test_new_entries_are_created_with_whole_names():
    assert config_flow.SmartPollConfigFlow.VERSION == 1
    assert config_flow.SmartPollConfigFlow.MINOR_VERSION == 3


# ---------- options to Settings ----------


def test_settings_from_options_defaults_and_units():
    assert integration.settings_from_options({}) == Settings()
    settings = integration.settings_from_options(
        {"charging_min_power": 1000, "charging_stop_delay": 600, "wake_window": 15}
    )
    assert settings.charging_min_power_w == 1000
    assert settings.charging_stop_delay == 600
    # Minutes in the options, seconds in Settings.
    assert settings.wake_window == 900


def test_options_intervals_step_shows_the_tunables_with_defaults():
    flow = config_flow.SmartPollOptionsFlow()
    flow._values = {}
    result = asyncio.run(flow.async_step_intervals())
    # The optional energy fields have no default (see test_config_flow).
    defaults = {
        str(key): key.default()
        for key in result["data_schema"].schema
        if key.default is not vol.UNDEFINED
    }
    assert defaults["charging_min_power"] == 300
    assert defaults["charging_stop_delay"] == 120
    assert defaults["wake_window"] == 10
    # Out of range is refused.
    with pytest.raises(vol.Invalid, match="too small"):
        result["data_schema"]({**defaults, "charging_min_power": 10})


def test_removal_finds_the_numbers_by_vin_with_the_device_gone(monkeypatch):
    restored: list[tuple] = []

    class Store:
        def __init__(self, *_args):
            pass

        async def async_load(self):
            return {"originals": {"telemetry": 600, "gps": 900}}

        async def async_remove(self):
            return None

    async def restore(_hass, numbers, intervals, *, key):
        restored.append((numbers, intervals, key))
        return True

    monkeypatch.setattr(integration, "Store", Store)
    monkeypatch.setattr(integration, "async_restore_intervals", restore)
    monkeypatch.setattr(integration, "async_delete_entry_issues", lambda *_a: None)
    monkeypatch.setattr(
        integration, "interval_entities", lambda _h, vin: ("n.t", "n.g") if vin == VIN else None
    )
    entry = SimpleNamespace(entry_id="entry", unique_id=VIN, data={"device_id": "gone"})
    asyncio.run(integration.async_remove_entry(None, entry))
    assert restored == [(("n.t", "n.g"), integration.Intervals(600, 900), VIN)]


def test_car_found_by_vin_per_config_entry_on_newer_home_assistant(monkeypatch):
    # Newer Home Assistant: async_get_device(identifiers=...) is deprecated, look up per entry.
    found = SimpleNamespace(id=NEW_DEVICE)
    asked: list[tuple] = []

    def by_identifier(identifier, config_entry_id):
        asked.append((identifier, config_entry_id))
        return found if config_entry_id == "byd_entry_2" else None

    def deprecated(**_kw):
        raise AssertionError("the deprecated lookup was used")

    registry = SimpleNamespace(
        async_get=lambda _id: None,
        async_get_device=deprecated,
        async_get_device_by_identifier=by_identifier,
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: (
                [
                    SimpleNamespace(entry_id="byd_entry_1"),
                    SimpleNamespace(entry_id="byd_entry_2"),
                ]
                if domain == "byd_vehicle"
                else []
            )
        )
    )
    monkeypatch.setattr(byd_entities.dr, "async_get", lambda _hass: registry)
    assert byd_entities.car_device_id(hass, VIN, OLD_DEVICE) == NEW_DEVICE
    assert asked == [(("byd_vehicle", VIN), "byd_entry_1"), (("byd_vehicle", VIN), "byd_entry_2")]


def test_stored_state_is_loaded_before_the_entities_are_added(setup_env, monkeypatch):
    # Entities added first showed the defaults (eg energy per 1% 0.82) until the store loaded.
    order: list[str] = []
    started = integration.SmartPollController

    class Recording(started):
        async def async_start(self):
            order.append("start")

    async def forward(_entry, _platforms):
        order.append("entities")

    monkeypatch.setattr(integration, "SmartPollController", Recording)
    setup_env.hass.config_entries.async_forward_entry_setups = forward
    assert asyncio.run(integration.async_setup_entry(setup_env.hass, setup_env.entry(NEW_DEVICE)))
    assert order == ["start", "entities"]
