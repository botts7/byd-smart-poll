import pytest
from bsp.signal_health import (
    UNAVAILABLE_LIMIT,
    Configured,
    EntityStatus,
    Issue,
    issues_for,
    signals_in_use,
    usable_state,
)

CHARGER = "sensor.wallbox_power"
CABLE = "binary_sensor.wallbox_cable"
PHONE = "sensor.pixel_bluetooth_connection"
HINT = "input_boolean.carplay"

ALL = Configured(
    charger_power=CHARGER, charger_connected=CABLE, phone_bluetooth=(PHONE,), wake_hints=(HINT,)
)


def _missing(entity_id: str, key: str, use: str) -> Issue:
    return Issue(f"signal_missing_{entity_id}", key, (("entity_id", entity_id), ("use", use)))


# ---------- issues ----------


def test_everything_working_no_issues():
    assert issues_for(ALL, {e: EntityStatus() for e in (CHARGER, CABLE, PHONE, HINT)}) == set()


def test_unchecked_entities_count_as_working():
    assert issues_for(ALL, {}) == set()


def test_no_charger_power():
    assert issues_for(Configured(), {}) == {Issue("no_charger_power", "no_charger_power")}


def test_no_home_charger_is_not_asked_for_a_charger_sensor():
    assert issues_for(Configured(no_home_charger=True), {}) == set()


def test_missing_disabled_and_long_unavailable():
    statuses = {
        CHARGER: EntityStatus(found=False),
        CABLE: EntityStatus(disabled=True),
        HINT: EntityStatus(unavailable_for=UNAVAILABLE_LIMIT),
    }
    assert issues_for(ALL, statuses) == {
        _missing(CHARGER, "signal_not_found", "charger power"),
        _missing(CABLE, "signal_disabled", "cable connected"),
        _missing(HINT, "signal_unavailable", "wake hint"),
    }


def test_short_outage_is_not_an_issue():
    statuses = {CHARGER: EntityStatus(unavailable_for=UNAVAILABLE_LIMIT - 1)}
    assert issues_for(ALL, statuses) == set()


def test_disabled_phone_sensor_gets_companion_app_hint():
    assert issues_for(ALL, {PHONE: EntityStatus(disabled=True)}) == {
        _missing(PHONE, "phone_sensor_disabled", "phone bluetooth")
    }


def test_entity_in_two_roles_gives_one_issue():
    both = Configured(charger_power=CHARGER, phone_bluetooth=(PHONE,), wake_hints=(PHONE,))
    issues = issues_for(both, {PHONE: EntityStatus(found=False)})
    assert issues == {_missing(PHONE, "signal_not_found", "phone bluetooth")}


# ---------- signals in use ----------


def test_signals_in_use_all():
    assert signals_in_use(ALL, {}, odometer=True, car_battery_power=True) == [
        "charger power",
        "cable connected",
        "phone bluetooth (1)",
        "wake hints (1)",
        "odometer check",
        "car battery power",
    ]


def test_signals_in_use_leaves_out_broken_and_unconfigured():
    configured = Configured(charger_power=CHARGER, wake_hints=(HINT, "switch.android_auto"))
    statuses = {CHARGER: EntityStatus(found=False), HINT: EntityStatus(disabled=True)}
    assert signals_in_use(configured, statuses, odometer=False, car_battery_power=True) == [
        "wake hints (1)",
        "car battery power",
    ]


def test_signals_in_use_only_with_a_usable_state():
    # Only the phone has a state that can be used now.
    assert signals_in_use(ALL, {}, odometer=False, car_battery_power=False, usable={PHONE}) == [
        "phone bluetooth (1)"
    ]


@pytest.mark.parametrize(
    ("state", "numeric", "expected"),
    [
        ("7.2", True, True),
        ("-300", True, True),
        ("on", True, False),
        ("nan", True, False),
        ("unavailable", False, False),
        ("unknown", False, False),
        (None, False, False),
        ("off", False, True),
    ],
)
def test_usable_state(state, numeric, expected):
    assert usable_state(state, numeric=numeric) is expected


def test_signals_in_use_car_only():
    assert signals_in_use(Configured(), {}, odometer=False, car_battery_power=False) == []


def test_charger_not_this_car_issue():
    working = {e: EntityStatus() for e in (CHARGER, CABLE, PHONE, HINT)}
    assert issues_for(ALL, working, charger_implausible=True) == {
        Issue("charger_not_this_car", "charger_not_this_car", (("entity_id", CHARGER),))
    }
    # Without a charger there is nothing to doubt.
    assert Issue("no_charger_power", "no_charger_power") in issues_for(
        Configured(), {}, charger_implausible=True
    )
    assert len(issues_for(Configured(), {}, charger_implausible=True)) == 1


# ---------- cable sensor that does not match the car ----------


def test_cable_implausible_issue():
    issues = issues_for(ALL, {}, cable_implausible=True)
    assert issues == {Issue("cable_implausible", "cable_implausible", (("entity_id", CABLE),))}


def test_cable_implausible_needs_a_cable_sensor():
    no_cable = Configured(charger_power=CHARGER)
    assert issues_for(no_cable, {}, cable_implausible=True) == set()
