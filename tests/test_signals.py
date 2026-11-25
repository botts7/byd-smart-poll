import pytest
from bsp.modes import (
    BYD_DEFAULTS,
    WAKE_OFF_MIN_AGE,
    WAKE_OFF_SPACING,
    Intervals,
    Mode,
    Settings,
    WakeWindow,
    pick_originals,
)
from bsp.signals import (
    AWAKE_HOLD,
    DRAIN_MIN_HOURS,
    ChargerSign,
    DrainMeter,
    OdometerWatch,
    Sample,
    at_home,
    charge_power_w,
    charging_shows_negative,
    device_for_typed_name,
    is_mac,
    is_wake_hint,
    likely_car,
    paired_devices,
    phone_connected_to_car,
    poll_brought_nothing,
    wake_hint_on,
)

H = 3600

# ---------- Bluetooth ----------

CONNECTED = ["02:00:00:AA:BB:CC (BYD SEALION 7)", "B0:A3:11:22:33:44 (OPPO Enco Air3)"]


def test_phone_connected_by_name_case_insensitive():
    assert phone_connected_to_car(CONNECTED, " byd sealion 7 ")


def test_part_of_a_name_does_not_match_another_car():
    # A "BYD Seal" driver typing their car's name must not count a "BYD SEALION 7".
    assert not phone_connected_to_car(CONNECTED, "BYD Seal")
    assert not phone_connected_to_car(CONNECTED, "byd sealion")
    assert phone_connected_to_car(["01:02:03:04:05:06 (BYD Seal)"], "BYD SEAL")


def test_phone_entry_without_a_mac_matches_the_whole_name():
    assert phone_connected_to_car(["BYD SEALION 7"], "byd sealion 7")
    assert not phone_connected_to_car(["BYD SEALION 7"], "BYD SEAL")


def test_device_for_a_typed_name():
    devices = paired_devices(
        [
            [
                "02:00:00:AA:BB:CC (BYD SEALION 7)",
                "01:02:03:04:05:06 (BYD Seal)",
                "B0:A3:11:22:33:44 (Buds)",
            ]
        ]
    )
    seal, sealion = (d for d in devices if d.name.startswith("BYD"))
    # The whole name, in any case.
    assert device_for_typed_name("byd seal", devices) == seal
    assert device_for_typed_name("BYD SEALION 7", devices) == sealion
    # A part that fits only one device (saved before whole names were needed).
    assert device_for_typed_name("sealion", devices) == sealion
    # A part that fits two, or none: unclear.
    assert device_for_typed_name("BYD", devices) is None
    assert device_for_typed_name("Kia", devices) is None
    assert device_for_typed_name(" ", devices) is None


def test_old_part_of_a_car_name_matches_only_when_asked():
    assert phone_connected_to_car(CONNECTED, "byd sealion", partial=True)
    assert not phone_connected_to_car(CONNECTED, "Kia", partial=True)


def test_paired_devices_with_every_name():
    phone_a = ["02:00:00:AA:BB:CC (BYD SEALION 7)"]
    phone_b = ["02:00:00:aa:bb:cc (Sealion)", "02:00:00:AA:BB:CC (byd sealion 7)"]
    devices = paired_devices([phone_a, phone_b], every_name=True)
    assert [(d.mac, d.name) for d in devices] == [
        ("02:00:00:AA:BB:CC", "BYD SEALION 7"),
        ("02:00:00:AA:BB:CC", "Sealion"),
    ]
    # The same car under two names is one device for a part of its name.
    assert device_for_typed_name("sealion", devices).mac == "02:00:00:AA:BB:CC"
    assert device_for_typed_name("sealion", devices).name == "Sealion"


def test_is_mac():
    assert is_mac(" 02:00:00:aa:bb:cc ")
    assert not is_mac("BYD SEALION 7")


def test_phone_connected_by_mac():
    assert phone_connected_to_car(CONNECTED, "02:00:00:aa:bb:cc")


def test_other_car_does_not_count():
    assert not phone_connected_to_car(["01:02:03:04:05:06 (Kia)"], "BYD SEALION 7")


def test_no_devices_or_no_car_name():
    assert not phone_connected_to_car(None, "BYD")
    assert not phone_connected_to_car(CONNECTED, "  ")


def test_paired_devices_merged_by_mac_and_sorted():
    phone_a = ["02:00:00:AA:BB:CC (BYD SEALION 7)", "B0:A3:11:22:33:44 (OPPO Enco Air3)"]
    # The same car on a second phone, lower case MAC and renamed, plus junk entries.
    phone_b = ["02:00:00:aa:bb:cc (Sealion)", "not a device", "01:02:03:04:05:06"]
    devices = paired_devices([phone_a, phone_b, None])
    assert [(d.mac, d.label) for d in devices] == [
        ("01:02:03:04:05:06", "01:02:03:04:05:06"),
        ("02:00:00:AA:BB:CC", "BYD SEALION 7 (02:00:00:AA:BB:CC)"),
        ("B0:A3:11:22:33:44", "OPPO Enco Air3 (B0:A3:11:22:33:44)"),
    ]


def test_paired_devices_none_reported():
    assert paired_devices([None, []]) == []


def test_likely_car_prefers_own_name_then_byd():
    devices = paired_devices([["01:00:00:00:00:01 (BYD ATTO 3)", "01:00:00:00:00:02 (BYD SEAL)"]])
    assert likely_car(devices, "Seal").mac == "01:00:00:00:00:02"
    assert likely_car(devices, "Sealion 7").mac == "01:00:00:00:00:01"
    assert likely_car(devices, None).mac == "01:00:00:00:00:01"


def test_likely_car_none_when_nothing_matches():
    devices = paired_devices([["B0:A3:11:22:33:44 (OPPO Enco Air3)"]])
    assert likely_car(devices, "Sealion 7") is None


# ---------- home ----------


def test_cable_connected_means_home():
    assert at_home(cable_connected=True, car_zone_home=False, moved_since_fix=True, previous=None)


def test_cable_out_falls_back_to_gps_zone():
    assert at_home(cable_connected=False, car_zone_home=True, moved_since_fix=False, previous=None)
    assert not at_home(
        cable_connected=False, car_zone_home=False, moved_since_fix=False, previous=True
    )


def test_gps_zone_not_trusted_after_missed_drive():
    assert not at_home(
        cable_connected=False, car_zone_home=True, moved_since_fix=True, previous=True
    )


def test_unknown_signals_keep_previous():
    # A charger that is unavailable during an HA restart must not flip home to away.
    assert at_home(cable_connected=None, car_zone_home=None, moved_since_fix=False, previous=True)
    assert (
        at_home(cable_connected=None, car_zone_home=None, moved_since_fix=False, previous=None)
        is None
    )


def test_no_gps_fix_keeps_previous():
    # 2026-10-21: a failed GPS fetch at startup left the tracker unavailable with the cable
    # out, and the car parked at home was counted as away.
    assert at_home(cable_connected=False, car_zone_home=None, moved_since_fix=False, previous=True)
    assert not at_home(
        cable_connected=False, car_zone_home=None, moved_since_fix=False, previous=False
    )


def test_no_charger_configured_uses_gps():
    assert at_home(cable_connected=None, car_zone_home=True, moved_since_fix=False, previous=None)


# ---------- missed drive ----------


def test_odometer_detects_missed_drive():
    watch = OdometerWatch()
    assert not watch.update(10250.0, parked=True)  # first reading
    assert not watch.update(10250.0, parked=True)  # parked, hourly poll
    assert watch.update(10263.0, parked=True)  # a 13 km errand nothing reported
    assert not watch.update(10263.0, parked=True)


def test_odometer_seen_drive_is_not_missed():
    # 2026-10-21: the first parked reading after a drive that was seen was flagged as missed.
    watch = OdometerWatch()
    watch.update(36520.0, parked=True)
    watch.note_drive()
    assert not watch.update(36530.0, parked=False)  # readings while driving
    assert not watch.update(36561.0, parked=True)  # parked at the end of it
    assert not watch.update(36561.0, parked=True)
    assert watch.update(36570.0, parked=True)  # a later drive nothing reported


def test_odometer_ignores_missing_readings():
    watch = OdometerWatch()
    watch.update(100.0, parked=True)
    assert not watch.update(None, parked=True)
    assert watch.update(101.0, parked=True)


# ---------- drain meter ----------


def test_awake_share_counts_parked_only():
    m = DrainMeter()
    for i, power in enumerate([-172.0, 0.0, 0.0, 0.0]):
        m.add(Sample(i * H, 90, power, parked=True))
    m.add(Sample(5 * H, 90, -9000.0, parked=False))  # driving, ignored
    assert m.awake_share(6 * H, H) == pytest.approx(AWAKE_HOLD / (AWAKE_HOLD + 3 * H))


def test_awake_share_needs_a_few_readings():
    m = DrainMeter([Sample(0, 90, 0.0, True)])
    assert m.awake_share(H, H) is None


def test_awake_share_weighs_readings_by_time():
    # Just parked, then a waking window: ten minute polls finding the car awake, then a
    # night of hourly polls finding it asleep. By count that was 10 of 33 readings.
    m = DrainMeter()
    for i in range(10):
        m.add(Sample(i * 60, 90, -500.0, parked=True))
    for h in range(23):
        m.add(Sample(600 + h * H, 90, 0.0, parked=True))
    share = m.awake_share(600 + 23 * H, H)
    assert share == pytest.approx(600 / (600 + 23 * H))
    assert share < 0.01


def test_awake_share_caps_each_reading_at_the_parked_interval():
    m = DrainMeter()
    for h in range(3):
        m.add(Sample(h * H, 90, 0.0, parked=True))
    # Awake, then nothing for 7 h (Home Assistant down) and awake again: one interval only.
    m.add(Sample(3 * H, 90, -300.0, parked=True))
    m.add(Sample(10 * H, 90, -300.0, parked=True))
    # Asleep next, so the second stands for AWAKE_HOLD; the last one for one interval.
    m.add(Sample(11 * H, 90, 0.0, parked=True))
    share = (H + AWAKE_HOLD) / (3 * H + H + AWAKE_HOLD + H)
    assert m.awake_share(12 * H, H) == pytest.approx(share)
    assert m.awake_share(20 * H, H) == pytest.approx(share)


def test_awake_share_holds_an_awake_reading_before_sleep_only_briefly():
    m = DrainMeter()
    m.add(Sample(0, 90, 0.0, parked=True))
    m.add(Sample(H, 90, -300.0, parked=True))
    m.add(Sample(3 * H, 90, 0.0, parked=True))
    # The awake reading counts for AWAKE_HOLD, not up to the next one; also when it is the last.
    assert m.awake_share(4 * H, 4 * H) == pytest.approx(AWAKE_HOLD / (H + AWAKE_HOLD + H))
    m.add(Sample(4 * H, 90, -300.0, parked=True))
    share = (2 * AWAKE_HOLD) / (H + AWAKE_HOLD + H + AWAKE_HOLD)
    assert m.awake_share(8 * H, 4 * H) == pytest.approx(share)


@pytest.mark.parametrize("interval", [H, 4 * H])
def test_awake_share_of_a_commute_day_with_just_parked_polls(interval):
    # Two parks a day; the polls 3 and 8 min after parking find the car still awake from the
    # drive, then it sleeps. It was really awake for well under 5 % of the parked time.
    m = DrainMeter()
    for leave, park, until in ((0, H / 2, 9 * H), (9 * H, 9.5 * H, 24 * H)):
        m.add(Sample(leave, 90, -9000.0, parked=False))
        m.add(Sample(park + 180, 90, -400.0, parked=True))
        m.add(Sample(park + 480, 90, -400.0, parked=True))
        t = park + 480 + interval
        while t < until:
            m.add(Sample(t, 90, 0.0, parked=True))
            t += interval
    share = m.awake_share(24 * H, interval)
    assert share is not None
    assert share < 0.05


def test_awake_share_counts_only_the_last_24_h():
    m = DrainMeter()
    # Awake twice in a row for 2 h, half of it inside the window, then a short hold.
    m.add(Sample(0, 90, -300.0, parked=True))
    m.add(Sample(2 * H, 90, -300.0, parked=True))
    for h in range(3, 26):
        m.add(Sample(h * H, 90, 0.0, parked=True))
    awake = H + AWAKE_HOLD
    assert m.awake_share(25 * H, 2 * H) == pytest.approx(awake / (awake + 22 * H))


def test_drain_ignores_charging_between_parked_stretches():
    m = DrainMeter()
    # 10 h parked losing 1 %, then a charge from 89 to 95, then 10 h parked losing 1 %.
    for h in range(11):
        m.add(Sample(h * H, 90 if h < 10 else 89, 0.0, parked=True))
    m.add(Sample(11 * H, 92, 3000.0, parked=False))
    for h in range(12, 23):
        m.add(Sample(h * H, 95 if h < 22 else 94, 0.0, parked=True))
    drain = m.parked_drain_per_day()
    assert drain == pytest.approx(2 / 20 * 24)


def test_drain_waits_for_enough_parked_time():
    m = DrainMeter([Sample(0, 90, 0.0, True), Sample((DRAIN_MIN_HOURS - 1) * H, 89, 0.0, True)])
    assert m.parked_drain_per_day() is None


def test_meter_keeps_72h_and_counts_polls():
    m = DrainMeter()
    for h in range(100):
        m.add(Sample(h * H, 90, 0.0, True))
    assert m.samples[0].ts == 27 * H
    assert m.polls(99 * H) == 25


# ---------- intervals ----------


def test_home_rate_only_when_parked():
    s = Settings(parked_telemetry=3600, parked_home_telemetry=14400)
    assert s.intervals(Mode.PARKED, home=True).telemetry == 14400
    assert s.intervals(Mode.PARKED, home=False).telemetry == 3600
    assert s.intervals(Mode.DRIVING, home=True) == s.intervals(Mode.DRIVING)


def test_pick_originals_ignores_home_rate_too():
    s = Settings(parked_home_telemetry=14400)
    assert pick_originals(Intervals(14400, 28800), s) == BYD_DEFAULTS


def test_drive_ended_only_when_leaving_driving():
    from bsp.modes import drive_ended

    assert drive_ended(Mode.DRIVING, Mode.PARKED)
    assert drive_ended(Mode.DRIVING, Mode.CHARGING)
    assert not drive_ended(Mode.PARKED, Mode.DRIVING)
    assert not drive_ended(None, Mode.PARKED)
    assert not drive_ended(Mode.WAKING, Mode.PARKED)


def test_poll_inside_gap_is_put_off_not_dropped():
    # 2026-10-23: unplug poll at t=0, then "expecting a drive" 120 s later on a loop clock
    # a hair short of the gap. The poll must still happen, a moment later.
    import math

    from bsp.modes import poll_wait

    assert poll_wait(0.0, 119.9, 120) == pytest.approx(0.1)
    assert poll_wait(0.0, 120.0, 120) == 0.0
    assert poll_wait(-math.inf, 5.0, 120) == 0.0


def test_wake_only_when_cable_not_in():
    from bsp.signals import wake_allowed

    assert not wake_allowed(True)  # plugged in: phone reconnecting to the parked car
    assert wake_allowed(False)
    assert wake_allowed(None)  # no charger configured, or unavailable


def test_charge_poll_inside_gap_is_skipped_not_put_off():
    # 2026-10-27: repeat charge polls put off by the gap became a poll every minute.
    from bsp.modes import poll_action

    assert poll_action(0.0, put_off=False) == "now"
    assert poll_action(30.0, put_off=False) == "skip"
    assert poll_action(30.0, put_off=True) == "later"
    assert poll_action(0.0, put_off=True) == "now"


def test_stale_charge_polls_back_off():
    # 2026-10-29: the car stopped answering mid-charge and was polled every minute for 73 min.
    from bsp.modes import stale_backoff

    assert [stale_backoff(n) for n in range(7)] == [60, 120, 240, 480, 960, 1800, 1800]
    # Polls in the 73 minute outage with the back-off: about 6 instead of about 70.
    t, n, polls = 0.0, 0, 0
    while t < 73 * 60:
        polls += 1
        n += 1
        t += stale_backoff(n)
    assert polls <= 7


def test_stale_count_only_in_a_drive_or_charge_and_restarts_with_each():
    from bsp.modes import counts_stale_polls, stale_count_restarts

    assert counts_stale_polls(Mode.DRIVING) and counts_stale_polls(Mode.CHARGING)
    # A false wake (the phone reconnecting to the parked car) is not stale data.
    assert not counts_stale_polls(Mode.WAKING) and not counts_stale_polls(Mode.PARKED)
    assert not counts_stale_polls(None)
    assert stale_count_restarts(Mode.PARKED, Mode.CHARGING)
    assert stale_count_restarts(Mode.WAKING, Mode.DRIVING)
    assert stale_count_restarts(None, Mode.CHARGING)
    # Driven home and plugged in: the same spell.
    assert not stale_count_restarts(Mode.DRIVING, Mode.CHARGING)
    assert not stale_count_restarts(Mode.CHARGING, Mode.PARKED)


def test_regen_while_driving_is_not_charging():
    # 2026-11-03: +16 kW of regen mid-drive, charger sensor unknown, counted as charging.
    from bsp.signals import car_says_charging

    assert car_says_charging(False, 16299.6, True, 300) is False
    assert car_says_charging(True, 7000.0, True, 300) is False
    # Car off: its own data counts (eg public charging, no home charger sensor).
    assert car_says_charging(False, 7000.0, False, 300) is True
    assert car_says_charging(True, None, False, 300) is True
    assert car_says_charging(False, -229.2, False, 300) is False
    assert car_says_charging(None, None, False, 300) is None


def test_negative_battery_power_counts_only_with_the_charging_flag():
    from bsp.signals import car_says_charging

    # Cars that report charging as negative: the flag says charging, whatever the power.
    assert car_says_charging(True, -7000.0, False, 300) is True
    # Without the flag, negative is the car drawing from its battery (heating, say).
    assert car_says_charging(False, -7000.0, False, 300) is False
    assert car_says_charging(None, -7000.0, False, 300) is False
    # And never while the car is on.
    assert car_says_charging(True, -16000.0, True, 300) is False


def test_older_reading_does_not_turn_the_car_off():
    # 2026-11-05 17:35: the poll's own reply (car time 06:35:29 UTC) said on, then the cloud's
    # cached reading from 06:35:07 said off, 0.3 s later.
    from bsp.signals import NewestReading

    newest = NewestReading()
    assert newest.note(1793860213, False)  # 06:30:13, parked
    assert newest.note(1793860529, True)  # 06:35:29, on
    assert newest.vehicle_on_now(False, 1793860507) is True  # the older reading's "off"
    assert newest.note(1793860507, False) is False
    assert newest.vehicle_on == True  # noqa: E712
    # The next real reading decides, either way.
    assert newest.note(1793860663, True)
    assert newest.vehicle_on_now(False, 1793860723) is False
    assert newest.note(1793860723, False)


def test_reading_far_older_is_taken_as_new():
    # A car clock that jumps back must not leave "car on" stuck.
    from bsp.signals import OLDER_READING_WINDOW, NewestReading

    newest = NewestReading()
    assert newest.note(1793860529, True)
    jumped = 1793860529 - OLDER_READING_WINDOW - 1
    assert newest.vehicle_on_now(False, jumped) is False
    assert newest.note(jumped, False)
    assert newest.vehicle_on_now(False, None) is False


# ---------- car on sensor unknown ----------


def test_vehicle_on_reported_wins():
    from bsp.signals import vehicle_on_or_keep

    assert vehicle_on_or_keep(True, driving=False, unknown_for=None)
    assert not vehicle_on_or_keep(False, driving=True, unknown_for=None)


def test_vehicle_on_unknown_keeps_the_drive_for_a_while():
    from bsp.signals import VEHICLE_ON_UNKNOWN_KEEP, vehicle_on_or_keep

    # A BYD reload mid-drive: the sensor is unavailable, not off.
    assert vehicle_on_or_keep(None, driving=True, unknown_for=5)
    assert not vehicle_on_or_keep(None, driving=False, unknown_for=5)
    # Broken for longer, or gone altogether, counts as off.
    assert not vehicle_on_or_keep(None, driving=True, unknown_for=VEHICLE_ON_UNKNOWN_KEEP)
    assert not vehicle_on_or_keep(None, driving=True, unknown_for=None)


def test_vehicle_on_keep_left():
    from bsp.signals import VEHICLE_ON_UNKNOWN_KEEP, vehicle_on_keep_left

    assert vehicle_on_keep_left(60) == VEHICLE_ON_UNKNOWN_KEEP - 60
    assert vehicle_on_keep_left(VEHICLE_ON_UNKNOWN_KEEP) is None
    assert vehicle_on_keep_left(None) is None


# ---------- cable sensor plausibility ----------

# Plugged in at T - 700 by default, so readings from T on are past the settle time.
T = 1000.0


def _plugged(at: float = T - 700):
    from bsp.signals import CableCheck

    check = CableCheck()
    check.cable(True, at)
    return check


def test_cable_believed_while_parked_and_plugged():
    check = _plugged()
    assert not check.reading(vehicle_on=False, odometer=12345, time=T)
    assert not check.reading(vehicle_on=False, odometer=12345, time=T + 5000)
    assert check.trusted(True) is True


def test_cable_wrong_when_the_car_moves_while_connected():
    from bsp.signals import MOVED_WHILE_PLUGGED

    # A shared charger: another car is plugged in while this one drives off.
    check = _plugged()
    check.reading(vehicle_on=False, odometer=12345, time=T)
    assert not check.reading(vehicle_on=False, odometer=12347, time=T + 400)
    assert check.reading(vehicle_on=False, odometer=12355, time=T + 400 + MOVED_WHILE_PLUGGED)
    assert check.implausible
    assert check.trusted(True) is None
    # Reported only once.
    assert not check.reading(
        vehicle_on=True, odometer=12360, time=T + 400 + MOVED_WHILE_PLUGGED + 60
    )


def test_one_odometer_step_then_parked_is_not_a_move():
    from bsp.signals import MOVED_WHILE_PLUGGED

    # The last reading of the drive home comes in after the settle time, one km short.
    check = _plugged()
    check.reading(vehicle_on=False, odometer=12344, time=T)
    check.reading(vehicle_on=False, odometer=12345, time=T + 60)
    for i in range(1, 10):
        assert not check.reading(
            vehicle_on=False, odometer=12345, time=T + 60 + i * MOVED_WHILE_PLUGGED
        )
    assert not check.implausible


def test_late_reading_from_the_drive_home_does_not_count():
    from bsp.signals import CONNECT_SETTLE, MOVED_WHILE_PLUGGED

    # Plugged in at 0. A late reading from the drive home and the car-off reading both come in
    # within the settle time, so the baseline is the parked odometer.
    check = _plugged(at=0)
    check.reading(vehicle_on=True, odometer=12344, time=10)
    check.reading(vehicle_on=False, odometer=12345, time=90)
    check.reading(vehicle_on=False, odometer=12345, time=CONNECT_SETTLE + 10)
    assert not check.reading(
        vehicle_on=False, odometer=12345, time=CONNECT_SETTLE + 10 + 2 * MOVED_WHILE_PLUGGED
    )
    assert not check.implausible


def test_late_unplug_report_on_a_drive_off_is_fine():
    # The charger reports the unplug a few minutes after the car drove off.
    check = _plugged()
    check.reading(vehicle_on=False, odometer=12345, time=T)
    assert not check.reading(vehicle_on=True, odometer=12345, time=50_000)
    assert not check.reading(vehicle_on=True, odometer=12346, time=50_180)
    assert not check.reading(vehicle_on=True, odometer=12348, time=50_240)
    check.cable(False, 50_300)
    assert not check.implausible
    assert check.trusted(False) is False


def test_found_wrong_is_kept_from_before_a_restart_until_it_turns_off():
    from bsp.signals import CableCheck

    check = CableCheck(implausible=True)
    check.cable(None, 0)
    check.cable(True, 10)
    assert check.trusted(True) is None
    check.cable(False, 20)
    assert check.trusted(False) is False


def test_cable_wrong_when_the_car_stays_on_while_connected():
    from bsp.signals import CAR_ON_WHILE_PLUGGED

    check = _plugged()
    assert not check.reading(vehicle_on=True, odometer=None, time=T)
    assert not check.reading(vehicle_on=True, odometer=None, time=T + CAR_ON_WHILE_PLUGGED - 1)
    assert check.reading(vehicle_on=True, odometer=None, time=T + CAR_ON_WHILE_PLUGGED)


def test_car_on_while_it_charges_is_fine():
    from bsp.signals import CAR_ON_WHILE_PLUGGED

    # Sitting in the car with the air on for 20 min while it charges.
    check = _plugged()
    for minute in range(0, 21, 5):
        assert not check.reading(
            vehicle_on=True, odometer=12345, time=T + minute * 60, charging=True
        )
    # Charging stops with the car still on: counted from then, not from before.
    assert not check.reading(vehicle_on=True, odometer=12345, time=T + 21 * 60)
    assert not check.reading(
        vehicle_on=True, odometer=12345, time=T + 21 * 60 + CAR_ON_WHILE_PLUGGED - 1
    )
    assert not check.implausible


def test_car_switched_on_briefly_while_plugged_is_fine():
    from bsp.signals import CAR_ON_WHILE_PLUGGED

    # Sitting in the car with the air on while it charges, twice.
    check = _plugged()
    check.reading(vehicle_on=True, odometer=12345, time=T)
    check.reading(vehicle_on=False, odometer=12345, time=T + 400)
    check.reading(vehicle_on=True, odometer=12345, time=T + CAR_ON_WHILE_PLUGGED)
    assert not check.implausible


def test_out_of_date_odometer_from_before_plugging_in_does_not_count():
    from bsp.signals import CONNECT_SETTLE

    # A drive home nobody saw: the odometer in HA is from before it, and the first reading
    # after plugging in jumps. Readings this soon after the cable connected are not used.
    check = _plugged(at=T)
    assert not check.reading(vehicle_on=False, odometer=12300, time=T + 10)
    assert not check.reading(vehicle_on=True, odometer=12340, time=T + 20)
    assert not check.reading(vehicle_on=False, odometer=12345, time=T + CONNECT_SETTLE)
    assert not check.reading(vehicle_on=False, odometer=12345, time=T + 5 * CONNECT_SETTLE)
    assert not check.implausible


def test_cable_believed_again_after_it_turns_off():
    from bsp.signals import CONNECT_SETTLE, MOVED_WHILE_PLUGGED

    check = _plugged()
    check.reading(vehicle_on=False, odometer=1, time=T)
    check.reading(vehicle_on=True, odometer=3, time=T + 100)
    check.reading(vehicle_on=True, odometer=9, time=T + 100 + MOVED_WHILE_PLUGGED)
    assert check.implausible
    check.cable(None, 5000)  # unknown keeps it
    assert check.implausible
    check.cable(False, 5100)
    assert not check.implausible
    assert check.trusted(False) is False
    # Plugged in again: a fresh start, the old odometer is not compared.
    check.cable(True, 6000)
    assert not check.reading(vehicle_on=False, odometer=10, time=6000 + CONNECT_SETTLE)
    assert not check.implausible


def test_cable_off_or_unknown_ignores_readings():
    from bsp.signals import CableCheck

    check = CableCheck()
    assert not check.reading(vehicle_on=True, odometer=1, time=1)
    check.cable(False, 0)
    check.reading(vehicle_on=True, odometer=1, time=1)
    assert not check.reading(vehicle_on=True, odometer=5, time=2)
    assert check.trusted(None) is None


def test_cable_step_from_the_drive_home_does_not_cut_the_unplug_grace():
    # Review trace: a single late odometer step after plugging in, hours parked, then an unplug
    # the charger reports a minute or two late. The first drive reading must not flag it.
    from bsp.signals import MOVED_WHILE_PLUGGED, CableCheck

    check = CableCheck()
    check.cable(True, 0)
    assert check.reading(vehicle_on=False, odometer=1000, time=400) is False
    assert check.reading(vehicle_on=False, odometer=1001, time=1200) is False
    later = 10 * 3600
    assert check.reading(vehicle_on=True, odometer=1002, time=later) is False
    assert check.reading(vehicle_on=True, odometer=1003, time=later + 120) is False
    # Still connected 10 min into the drive: now the sensor is wrong.
    assert check.reading(vehicle_on=True, odometer=1008, time=later + MOVED_WHILE_PLUGGED) is True


# ---------- charge power and wake hints ----------


@pytest.mark.parametrize(
    ("charger", "car", "cable", "car_says", "expected"),
    [
        # A home charge: the charger.
        (7000.0, 6800.0, True, True, 7000.0),
        # A public charge with the home charger idle and the cable out: the car's power.
        (0.0, 45000.0, False, True, 45000.0),
        (0.0, 45000.0, None, True, 45000.0),
        # A solar dip at home: the charger is still right, with the cable in...
        (100.0, 6800.0, True, True, 100.0),
        # ...or with the car not saying charging.
        (0.0, 6800.0, False, False, 0.0),
        (0.0, 6800.0, False, None, 0.0),
        # Never negative.
        (-50.0, None, True, True, 0.0),
        (None, -2000.0, None, False, 0.0),
        (None, -2000.0, None, None, 0.0),
        # Cars that report charging as negative battery power, once the car says charging.
        (None, -7000.0, None, True, 7000.0),
        (0.0, -45000.0, False, True, 45000.0),
        # No charger (or not trusted): the car's power, or nothing.
        (None, 7000.0, None, True, 7000.0),
        (None, None, None, True, 0.0),
    ],
)
def test_charge_power_w(charger, car, cable, car_says, expected):
    power = charge_power_w(charger, car, cable_connected=cable, car_says=car_says, min_power_w=300)
    assert power == expected


@pytest.mark.parametrize(
    ("car", "negative", "expected"),
    [
        # Before the sign is known: the size of the power.
        (7000.0, None, 7000.0),
        (-7000.0, None, 7000.0),
        # A car that shows charging as positive: drawing from the battery while it says
        # charging (heating the cabin at a slow charger) adds nothing.
        (7000.0, False, 7000.0),
        (-300.0, False, 0.0),
        # One that shows it as negative, the other way round.
        (-7000.0, True, 7000.0),
        (300.0, True, 0.0),
    ],
)
def test_charge_power_w_follows_the_learned_sign(car, negative, expected):
    power = charge_power_w(
        None, car, cable_connected=None, car_says=True, min_power_w=300, charging_negative=negative
    )
    assert power == expected


@pytest.mark.parametrize(
    ("previous", "charging", "power", "vehicle_on", "expected"),
    [
        # A charging reading strong enough either way shows the sign.
        (None, True, 7000.0, False, False),
        (None, True, -7000.0, False, True),
        # The latest such reading wins.
        (True, True, 7000.0, False, False),
        # Not from a weak reading (cabin heating at a slow charger), without the flag, without
        # a power, or with the car on (regenerative braking).
        (None, True, -300.0, False, None),
        (False, True, -300.0, False, False),
        (None, False, -7000.0, False, None),
        (None, None, 7000.0, False, None),
        (None, True, None, False, None),
        (None, True, 16000.0, True, None),
    ],
)
def test_charging_shows_negative(previous, charging, power, vehicle_on, expected):
    assert charging_shows_negative(previous, charging, power, vehicle_on) is expected


def test_charging_sign_is_not_learned_while_the_charger_is_idle():
    # A solar pause: the flag still says charging and the car draws from its battery.
    assert charging_shows_negative(False, True, -1500.0, False, charger_idle=True) is False
    assert charging_shows_negative(None, True, -1500.0, False, charger_idle=True) is None


@pytest.mark.parametrize(
    ("domain", "old", "new", "expected"),
    [
        # Toggles and binary sensors count when they turn on, not off.
        ("input_boolean", "off", "on", True),
        ("input_boolean", "on", "off", False),
        ("binary_sensor", "off", "on", True),
        ("binary_sensor", "on", "off", False),
        ("switch", "off", "on", True),
        # A button when pressed, also the first press.
        ("input_button", "2026-11-14T08:00:00+00:00", "2026-11-14T09:00:00+00:00", True),
        ("button", "unknown", "2026-11-14T09:00:00+00:00", True),
        ("input_button", "unavailable", "2026-11-14T08:00:00+00:00", False),
        ("button", "2026-11-14T08:00:00+00:00", "2026-11-14T08:00:00+00:00", False),
        # Anything else when it changes to connected or on.
        ("sensor", "disconnected", "Connected", True),
        # Not a tracker arriving home: that comes just after a drive.
        ("device_tracker", "not_home", "home", False),
        ("input_select", "Away", "on", True),
        ("device_tracker", "home", "home", False),
        ("device_tracker", "home", "not_home", False),
        ("sensor", "Home", "Car", False),
        # A count of connected devices changes on every headset.
        ("sensor", "1", "2", False),
        ("input_select", "a", "a", False),
        # Unavailable or unknown on either side is a restart, not a wake.
        ("binary_sensor", "unavailable", "on", False),
        ("binary_sensor", None, "on", False),
        ("sensor", "unknown", "Car", False),
        ("sensor", "Car", "unavailable", False),
    ],
)
def test_is_wake_hint(domain, old, new, expected):
    assert is_wake_hint(domain, old, new) is expected


@pytest.mark.parametrize(
    ("domain", "state", "expected"),
    [
        ("input_boolean", "on", True),
        ("binary_sensor", "off", False),
        ("sensor", "Connected", True),
        # Home at every unplug would start a full window each time.
        ("device_tracker", "home", False),
        ("sensor", "2", False),
        ("input_button", "2026-11-14T08:00:00+00:00", False),
        ("sensor", "unavailable", False),
        ("sensor", None, False),
    ],
)
def test_wake_hint_on(domain, state, expected):
    assert wake_hint_on(domain, state) is expected


# ---------- charger sign ----------


def test_charger_sign_turned_round_by_a_charging_car():
    sign = ChargerSign()
    assert sign.apply(-7000) == -7000
    # A negative reading alone, or with the car not charging, says nothing.
    assert not sign.car_reading(-7000, car_charging=False, min_power_w=300)
    assert not sign.car_reading(-7000, car_charging=None, min_power_w=300)
    assert not sign.car_reading(-100, car_charging=True, min_power_w=300)
    assert sign.car_reading(-7000, car_charging=True, min_power_w=300)
    assert sign.apply(-7000) == 7000
    assert sign.apply(None) is None


def test_charger_sign_not_turned_round_after_a_positive_reading():
    # A house meter: imports at times, and exports solar while the car charges.
    sign = ChargerSign()
    assert not sign.charger_power(2000, 300)
    assert not sign.car_reading(-1500, car_charging=True, min_power_w=300)
    assert sign.apply(-1500) == -1500


def test_charger_sign_positive_reading_kept_from_a_restart():
    # A house meter seen importing before a restart, exporting solar just after it.
    sign = ChargerSign(seen_positive=True)
    assert not sign.car_reading(-1500, car_charging=True, min_power_w=300)
    assert sign.apply(-1500) == -1500


def test_charger_sign_kept_from_a_restart_ends_on_a_positive_reading():
    sign = ChargerSign(negative=True)
    assert sign.apply(-7000) == 7000
    # Standby noise either side of zero keeps it.
    assert not sign.charger_power(5, 300)
    assert sign.negative
    assert sign.charger_power(7000, 300)
    assert sign.apply(7000) == 7000


# ---------- wake window ----------


def test_wake_window_ends_after_two_polls_with_the_car_off():
    wake = WakeWindow()
    wake.start(0, 600)
    m = WAKE_OFF_MIN_AGE
    # The poll that starts the faster rate, and every poll in the first minutes (the phone
    # connects at unlock, before the car is turned on), do not count.
    for t in (5, 60, 120, m - 1):
        assert not wake.car_seen(t, car_on=False)
    assert not wake.car_seen(m, car_on=False)
    # A second write of the same refresh does not count twice.
    assert not wake.car_seen(m + WAKE_OFF_SPACING / 2, car_on=False)
    assert wake.car_seen(m + 60, car_on=False)


def test_wake_window_car_turned_on_after_a_few_minutes_is_not_ended():
    wake = WakeWindow()
    wake.start(0, 600)
    # Phone connected at unlock, the car still off at 1 and 2 min, turned on at 3.
    assert not wake.car_seen(60, car_on=False)
    assert not wake.car_seen(120, car_on=False)
    assert not wake.car_seen(180, car_on=True)


def test_wake_window_car_on_resets_the_count():
    wake = WakeWindow()
    wake.start(0, 600)
    m = WAKE_OFF_MIN_AGE
    assert not wake.car_seen(m, car_on=False)
    assert not wake.car_seen(m + 60, car_on=True)
    assert not wake.car_seen(m + 120, car_on=False)
    assert wake.car_seen(m + 180, car_on=False)


def test_new_wake_starts_the_count_again():
    wake = WakeWindow()
    wake.start(0, 600)
    m = WAKE_OFF_MIN_AGE
    assert not wake.car_seen(m, car_on=False)
    wake.start(m + 40, 600)
    assert not wake.car_seen(m + 100, car_on=False)
    assert not wake.car_seen(2 * m + 40, car_on=False)
    assert wake.car_seen(2 * m + 100, car_on=False)


def test_wake_window_held_while_the_driver_is_in_the_car():
    wake = WakeWindow()
    wake.start(0, 600)
    # The phone stays connected: off polls count but do not end the window.
    for t in range(60, 480, 60):
        assert not wake.car_seen(t, car_on=False, held=True)
    # The phone has gone: the next poll that finds the car off ends it.
    assert wake.car_seen(480, car_on=False)


def test_wake_window_held_then_car_on_is_not_ended():
    wake = WakeWindow()
    wake.start(0, 600)
    for t in range(60, 480, 60):
        assert not wake.car_seen(t, car_on=False, held=True)
    assert not wake.car_seen(480, car_on=True, held=True)
    assert wake.active(480)


def test_wake_window_that_runs_its_length():
    wake = WakeWindow()
    wake.start(0, 600, ends_early=False)
    for t in (60, 120, 180, 240, 300, 360):
        assert not wake.car_seen(t, car_on=False)
    assert wake.active(599) and not wake.active(600)
    # Outside a window nothing counts.
    wake.end()
    assert not wake.car_seen(700, car_on=False)


def test_longer_wake_window_ends_early_later_in_step():
    wake = WakeWindow()
    # 20 min set in the options: off polls count from a third of the way in, not at 3 min.
    wake.start(0, 1200)
    for t in range(60, 360, 60):
        assert not wake.car_seen(t, car_on=False)
    assert not wake.car_seen(360, car_on=False)
    assert wake.car_seen(420, car_on=False)


def test_wake_during_a_full_length_window_keeps_it_full_length():
    wake = WakeWindow()
    # Unplugged with the phone in the car, then Android Auto finishes connecting.
    wake.start(0, 600, ends_early=False)
    wake.start(45, 600)
    assert not wake.ends_early
    for t in range(60, 645, 60):
        assert not wake.car_seen(t, car_on=False)
    assert wake.active(644) and not wake.active(645)


def test_wake_during_a_window_keeps_the_later_end():
    wake = WakeWindow()
    wake.start(0, 1200)
    wake.start(60, 600)
    assert wake.until == 1200


# ---------- stale data ----------


@pytest.mark.parametrize(
    ("before", "before_at", "after", "after_at", "expected"),
    [
        # The refresh wrote the same reading again.
        ("T1", 100.0, "T1", 101.0, True),
        # Nothing written during the poll (debounced, or the refresh failed).
        ("T1", 100.0, "T1", 100.0, False),
        # A new reading.
        ("T1", 100.0, "T2", 101.0, False),
        # Unavailable or no entity on either side says nothing.
        ("unavailable", 100.0, "unavailable", 101.0, False),
        ("T1", 100.0, "unavailable", 101.0, False),
        (None, None, "T1", 101.0, False),
    ],
)
def test_poll_brought_nothing(before, before_at, after, after_at, expected):
    assert poll_brought_nothing(before, before_at, after, after_at) is expected
