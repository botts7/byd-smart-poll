from bsp.modes import (
    JUST_PARKED_POLLS,
    Mode,
    just_parked_poll_wanted,
    just_parked_polls,
    keeps_just_parked_polls,
)
from bsp.signals import (
    CHARGE_DATA_MAX_AGE,
    CHARGER_CHECK_PCT,
    CHARGER_CHECK_READINGS,
    CHARGER_IDLE_RESET,
    CHARGER_UNKNOWN_HOLD,
    OLDER_READING_WINDOW,
    ChargerCheck,
    NewestReading,
    car_charge_data_stale,
    car_data_changed,
    charge_poll_due,
    charger_unknown_hold_left,
    charger_unknown_holds,
    charging_signal,
    implausible_for,
    kept_stale_charge,
)

MIN_W = 300.0

# ---------- charging signal ----------


def _signal(
    charger, *, cable=None, car=None, since_idle=True, home=None, unknown=False, stale=False
):
    return charging_signal(
        charger,
        MIN_W,
        cable_connected=cable,
        car_charging=car,
        car_reading_since_idle=since_idle,
        home=home,
        charger_unknown=unknown,
        car_data_stale=stale,
    )


def test_charger_over_threshold_is_charging():
    assert _signal(7200.0, cable=False, car=False) is True


def test_no_charger_uses_the_car():
    assert _signal(None, car=True) is True
    assert _signal(None, car=False) is False
    assert _signal(None, car=None) is None


def test_public_charge_seen_with_home_charger_idle():
    # Home wallbox at 0 W, cable not in at home (or no cable sensor), car says charging.
    assert _signal(0.0, cable=False, car=True) is True
    assert _signal(0.0, cable=None, car=True) is True
    # Away from home, or where the car is cannot be told.
    assert _signal(0.0, cable=None, car=True, home=False) is True
    assert _signal(0.0, cable=None, car=True, home=None) is True


def test_idle_home_charger_with_the_car_at_home_is_final():
    # No cable sensor, a solar or schedule pause at home: the car can still say charging while
    # the charger waits to give it current.
    assert _signal(0.0, cable=None, car=True, home=True) is False
    assert _signal(0.0, cable=False, car=True, home=True) is False
    # A charger over the threshold still counts at home.
    assert _signal(7200.0, cable=None, car=False, home=True) is True


def test_idle_home_charger_with_cable_in_is_final():
    assert _signal(0.0, cable=True, car=True) is False


def test_car_reading_from_before_the_charger_went_idle_does_not_count():
    # Unplugged after a home charge: the car's last reading still says charging.
    assert _signal(0.0, cable=False, car=True, since_idle=False) is False


def test_unavailable_home_charger_keeps_what_was_known():
    # After a restart the wallbox is unavailable for a while, and the car's last reading is
    # from a charge that has paused or ended: not taken as charging at home or plugged in.
    assert _signal(None, car=True, home=True, unknown=True) is None
    assert _signal(None, car=True, cable=True, unknown=True) is None
    assert _signal(None, car=False, home=True, unknown=True) is None


def test_unavailable_home_charger_away_uses_the_car():
    # A public charge while the home charger is down still counts.
    assert _signal(None, car=True, home=False, unknown=True) is True
    assert _signal(None, car=True, home=None, cable=False, unknown=True) is True
    assert _signal(None, car=False, home=False, unknown=True) is False


def test_stale_car_data_is_not_charging():
    # Asleep with its last upload saying charging: no charger, an idle home charger away, or
    # an unavailable one away.
    assert _signal(None, car=True, stale=True) is False
    assert _signal(0.0, cable=False, car=True, home=False, stale=True) is False
    assert _signal(None, car=True, home=False, unknown=True, stale=True) is False
    # Unknown stays unknown, and the charger's own power still counts.
    assert _signal(None, car=None, stale=True) is None
    assert _signal(7200.0, car=False, stale=True) is True


def test_charge_data_stale_only_when_old_and_our_poll_found_nothing():
    # A rewrite or two with no new reading says nothing: only our own charge polls count.
    assert not car_charge_data_stale(CHARGE_DATA_MAX_AGE * 10, 0)
    # A poll that brought nothing new while the reading is still recent: a slow upload.
    assert not car_charge_data_stale(CHARGE_DATA_MAX_AGE - 1, 3)
    assert car_charge_data_stale(CHARGE_DATA_MAX_AGE, 1)


def test_kept_stale_charge_only_for_the_same_reading():
    kept = {"reading": "2026-11-14T08:00:00+00:00", "fresh": 1000.0, "polls": 2}
    # The same reading after a restart: its age counts from when it arrived, downtime too.
    assert kept_stale_charge(kept, "2026-11-14T08:00:00+00:00", 4600.0) == (3600.0, 2)
    # A newer reading, none yet (BYD still loading) or nothing kept: start afresh.
    assert kept_stale_charge(kept, "2026-11-14T08:05:00+00:00", 4600.0) is None
    assert kept_stale_charge(kept, None, 4600.0) is None
    assert kept_stale_charge(None, "2026-11-14T08:00:00+00:00", 4600.0) is None
    assert kept_stale_charge({"reading": "x"}, "x", 4600.0) is None
    # A clock set back does not give a negative age.
    assert kept_stale_charge(kept, "2026-11-14T08:00:00+00:00", 0.0) == (0.0, 2)


def test_charger_unknown_holds_only_for_a_while():
    assert charger_unknown_holds(0.0)
    assert charger_unknown_holds(CHARGER_UNKNOWN_HOLD - 1)
    # Down for longer (its gateway or broker): the car's own data decides.
    assert not charger_unknown_holds(CHARGER_UNKNOWN_HOLD)
    # Removed or renamed: no charger at all.
    assert not charger_unknown_holds(None)


def test_charger_unknown_hold_left():
    assert charger_unknown_hold_left(0.0) == CHARGER_UNKNOWN_HOLD
    assert charger_unknown_hold_left(CHARGER_UNKNOWN_HOLD - 60) == 60
    assert charger_unknown_hold_left(CHARGER_UNKNOWN_HOLD) is None
    assert charger_unknown_hold_left(None) is None


def test_idle_charger_and_car_unknown_is_not_charging():
    assert _signal(0.0, cable=False, car=None) is False
    assert _signal(0.0, cable=False, car=False) is False


# ---------- charger check ----------

KWH_PER_PCT = 0.6
# Seconds at 7 kW until a tick is overdue, plus a minute.
LATE = CHARGER_CHECK_PCT * KWH_PER_PCT / 7.0 * 3600 + 60


def _check(soc=50.0, ts=0.0, power=7000.0):
    check = ChargerCheck()
    check.charger_power(ts, power, MIN_W, soc)
    return check


def _read(check, ts, soc=50.0, charging=False, power=-200.0, away=False, cable_in=False):
    check.reading(
        ts,
        soc=soc,
        car_charging=charging,
        car_power_w=power,
        min_power_w=MIN_W,
        kwh_per_pct=KWH_PER_PCT,
        away=away,
        cable_in=cable_in,
    )


def test_charger_doubted_after_two_late_readings_car_not_charging():
    check = _check()
    _read(check, LATE)
    assert check.trusted and not check.implausible
    _read(check, LATE + 60)
    assert CHARGER_CHECK_READINGS == 2
    assert not check.trusted and check.implausible


def test_early_readings_of_a_real_charge_do_not_count():
    # Just plugged in: the first readings still show the car from before (flag off, no power).
    check = _check()
    _read(check, 0.0)
    _read(check, 160.0)
    # Then the first tick.
    _read(check, 400.0, soc=51.0)
    assert check.trusted and not check.implausible
    # Readings later in the same charge count from that tick, so these are early too.
    _read(check, 700.0, soc=51.0)
    _read(check, 900.0, soc=51.0)
    assert check.trusted and not check.implausible


def test_full_battery_is_not_charging_but_no_verdict():
    # A full car on a shared wallbox, or balancing at 100 %, or the cabin heated or cooled on
    # the cable: not charging this car right now, but no Repairs verdict on the charger.
    check = _check(soc=100.0)
    _read(check, LATE, soc=100.0)
    assert check.trusted
    _read(check, LATE + 60, soc=99.0)
    assert check.full and not check.trusted and not check.implausible
    # Signal as the controller makes it: an untrusted charger is left out, so the car decides.
    assert _signal(None, car=False) is False


def test_full_early_readings_do_not_count():
    # Plugged in at 99 %: the first readings still show the car from before.
    check = _check(soc=99.0)
    _read(check, 60.0, soc=99.0)
    _read(check, 120.0, soc=99.0)
    assert check.trusted and not check.full


def test_full_ends_when_the_charger_goes_idle():
    check = _check(soc=100.0)
    _read(check, LATE, soc=100.0)
    _read(check, LATE + 60, soc=100.0)
    assert check.full
    # Short dips keep it (the other car's charge pausing).
    check.charger_power(LATE + 70, 0.0, MIN_W, 100.0)
    check.charger_power(LATE + 130, 7000.0, MIN_W, 100.0)
    assert check.full
    check.charger_power(LATE + 140, 0.0, MIN_W, 100.0)
    check.charger_power(LATE + 140 + CHARGER_IDLE_RESET, 7000.0, MIN_W, 100.0)
    assert check.trusted and not check.full


def test_full_ends_when_the_car_charges_or_drops_below_full():
    check = _check(soc=100.0)
    _read(check, LATE, soc=100.0)
    _read(check, LATE + 60, soc=100.0)
    _read(check, LATE + 120, soc=100.0, charging=True, power=7000.0)
    assert check.trusted and not check.full
    # Below full again (driven, or drained on the cable): the count starts again, so a charger
    # that is not charging this car can then be doubted.
    check = _check(soc=100.0)
    _read(check, LATE, soc=100.0)
    _read(check, LATE + 60, soc=100.0)
    check.car_soc(100.0)
    assert check.full
    check.car_soc(97.0)
    assert check.trusted and not check.full
    _read(check, 2 * LATE + 60, soc=97.0)
    assert check.trusted
    _read(check, 2 * LATE + 120, soc=97.0)
    assert check.implausible


def test_cable_in_is_drawing_not_a_verdict():
    # At its charge limit (80 %) with the cabin heated on the cable: about 2 kW, flag off, no
    # rise. The cable says it is this car's charger, so no lasting verdict and no Repairs.
    check = _check(soc=80.0, power=2000.0)
    late = CHARGER_CHECK_PCT * KWH_PER_PCT / 2.0 * 3600 + 60
    _read(check, late, soc=80.0, cable_in=True)
    _read(check, late + 60, soc=80.0, cable_in=True)
    assert check.drawing and not check.trusted and not check.implausible
    # A short dip keeps it, like full.
    check.charger_power(late + 70, 0.0, MIN_W, 80.0)
    check.charger_power(late + 130, 2000.0, MIN_W, 80.0)
    assert check.drawing
    # The evening's charge, after the heating has stopped: trusted again.
    check.charger_power(late + 140, 0.0, MIN_W, 80.0)
    check.charger_power(late + 140 + CHARGER_IDLE_RESET, 7000.0, MIN_W, 80.0)
    assert check.trusted and not check.drawing


def test_drawing_ends_when_the_car_says_charging():
    check = _check(soc=80.0, power=2000.0)
    late = CHARGER_CHECK_PCT * KWH_PER_PCT / 2.0 * 3600 + 60
    _read(check, late, soc=80.0, cable_in=True)
    _read(check, late + 60, soc=80.0, cable_in=True)
    assert check.drawing
    _read(check, late + 120, soc=80.0, charging=True, power=7000.0, cable_in=True)
    assert check.trusted and not check.drawing


def test_drawing_ends_when_the_soc_rises():
    check = _check(soc=80.0, power=2000.0)
    late = CHARGER_CHECK_PCT * KWH_PER_PCT / 2.0 * 3600 + 60
    _read(check, late, soc=80.0, cable_in=True)
    _read(check, late + 60, soc=80.0, cable_in=True)
    assert check.drawing
    # A parked reading with the same SoC keeps it, one with a risen SoC ends it.
    check.car_soc(80.0)
    assert check.drawing
    check.car_soc(None)
    assert check.drawing
    check.car_soc(81.0)
    assert check.trusted and not check.drawing


def test_cable_out_or_doubted_still_gives_a_verdict():
    check = _check()
    _read(check, LATE, cable_in=False)
    _read(check, LATE + 60, cable_in=False)
    assert check.implausible and not check.drawing


def test_short_dips_keep_the_count():
    # A house meter or a plug with a cycling load at about 1 kW, dipping under 300 W for 2 min
    # every 10 min. Each rise is a CHARGING entry with a poll, the car not charging.
    check = _check(power=1000.0)
    ts = 0.0
    readings = 0
    while not check.implausible and readings < 30:
        ts += 600.0
        check.charger_power(ts, 100.0, MIN_W, 50.0)
        ts += 120.0
        check.charger_power(ts, 1000.0, MIN_W, 50.0)
        _read(check, ts)
        readings += 1
    assert check.implausible and not check.trusted
    # The early readings did not count, the late ones did.
    assert readings > CHARGER_CHECK_READINGS


def test_long_idle_starts_the_count_again():
    check = _check()
    _read(check, LATE)
    restart = LATE + 10 + CHARGER_IDLE_RESET
    check.charger_power(LATE + 10, 0.0, MIN_W, 50.0)
    check.charger_power(restart, 7000.0, MIN_W, 50.0)
    # Late again, but only one miss since the restart.
    _read(check, restart + LATE)
    assert check.trusted


def test_doubted_charger_stays_doubted_after_dropping_below_threshold():
    check = _check()
    _read(check, LATE)
    _read(check, LATE + 60)
    check.charger_power(LATE + 70, 0.0, MIN_W, 50.0)
    check.charger_power(LATE + 70 + 2 * CHARGER_IDLE_RESET, 7000.0, MIN_W, 50.0)
    assert not check.trusted and check.implausible


def test_doubted_charger_trusted_again_when_seen_charging_this_car():
    check = _check()
    _read(check, LATE)
    _read(check, LATE + 60)
    # The car's own data shows a charge while the charger shows power.
    _read(check, LATE + 120, charging=True, power=7000.0)
    assert check.trusted and not check.implausible
    # Or its SoC rises, after a restart.
    check = ChargerCheck(implausible=True)
    check.charger_power(0.0, 7000.0, MIN_W, 50.0)
    _read(check, 600.0, soc=51.0)
    assert check.trusted and not check.implausible


def test_charging_away_from_home_says_nothing_about_the_charger():
    check = ChargerCheck(implausible=True)
    check.charger_power(0.0, 1000.0, MIN_W, 50.0)
    _read(check, 600.0, soc=51.0, charging=True, power=50000.0, away=True)
    assert check.implausible


def test_rising_soc_keeps_the_charger_trusted():
    check = _check(50.0)
    _read(check, LATE, soc=51.0)
    _read(check, 2 * LATE, soc=52.0)
    assert check.trusted


def test_car_saying_charging_resets_the_count():
    check = _check()
    _read(check, LATE)
    _read(check, LATE + 30, charging=True, power=7000.0)
    _read(check, 2 * LATE + 30)
    assert check.trusted
    # Charging power alone counts too, with the flag off.
    _read(check, 3 * LATE, power=6800.0)
    _read(check, 4 * LATE)
    assert check.trusted


def test_unknown_charging_flag_does_not_count_against_the_charger():
    check = _check()
    for i in range(3):
        _read(check, LATE + i * 60, charging=None, power=None)
    assert check.trusted


def test_soc_unknown_at_start_counts_from_the_first_reading():
    check = _check(soc=None)
    _read(check, 60.0)
    _read(check, 300.0)
    assert check.trusted
    _read(check, 60.0 + LATE)
    _read(check, 120.0 + LATE)
    assert not check.trusted


def test_implausible_kept_across_restarts():
    assert ChargerCheck(implausible=True).implausible
    assert not ChargerCheck(implausible=True).trusted


def test_unavailable_charger_changes_nothing():
    check = _check()
    _read(check, LATE)
    _read(check, LATE + 60)
    check.charger_power(LATE + 70, None, MIN_W, 50.0)
    assert not check.trusted


def test_stored_verdict_only_for_the_same_charger():
    assert implausible_for("sensor.wallbox_power", "sensor.wallbox_power")
    # Another sensor picked, as the Repairs issue asks, or the charger removed.
    assert not implausible_for("sensor.wallbox_power", "sensor.car_charger_power")
    assert not implausible_for("sensor.wallbox_power", None)
    assert not implausible_for(None, "sensor.wallbox_power")
    # Stored by an earlier build as a plain flag.
    assert not implausible_for(True, "sensor.wallbox_power")


# ---------- car data freshness ----------


def test_unavailable_flap_is_not_new_car_data():
    # After unplugging, the car's last reading still says charging. A cloud error makes it
    # unavailable, then the same reading comes back: that must not count as since idle.
    assert not car_data_changed("on", "unavailable")
    assert not car_data_changed("unavailable", "on")
    assert not car_data_changed("unknown", "on")
    assert not car_data_changed(None, "on")
    assert not car_data_changed("on", "on")
    assert car_data_changed("off", "on")
    assert car_data_changed("0.0", "7.2")


def test_same_reading_back_is_not_newer():
    newest = NewestReading()
    assert newest.is_newer(1000.0)
    newest.note(1000.0, False)
    assert not newest.is_newer(1000.0)
    assert not newest.is_newer(990.0)
    assert newest.is_newer(1060.0)
    assert not newest.is_newer(None)
    # A clock that jumps back a long way is a new reading, as for note().
    assert newest.is_newer(1000.0 - OLDER_READING_WINDOW - 1)


# ---------- just parked polls ----------


def test_just_parked_polls_after_a_drive():
    assert just_parked_polls(Mode.DRIVING, Mode.PARKED) == JUST_PARKED_POLLS
    assert just_parked_polls(Mode.DRIVING, Mode.WAKING) == JUST_PARKED_POLLS
    assert JUST_PARKED_POLLS == (180.0, 480.0)


def test_no_just_parked_polls_otherwise():
    # Straight into charging is already polled per percent.
    assert just_parked_polls(Mode.DRIVING, Mode.CHARGING) == ()
    assert just_parked_polls(Mode.PARKED, Mode.WAKING) == ()
    assert just_parked_polls(Mode.CHARGING, Mode.PARKED) == ()
    assert just_parked_polls(None, Mode.PARKED) == ()


def test_drive_or_charge_cancels_just_parked_polls():
    assert keeps_just_parked_polls(Mode.PARKED)
    assert keeps_just_parked_polls(Mode.WAKING)
    assert not keeps_just_parked_polls(Mode.DRIVING)
    assert not keeps_just_parked_polls(Mode.CHARGING)


def _wanted(mode=Mode.PARKED, *, enabled=True, trusted=False, cable=None, home=None):
    return just_parked_poll_wanted(
        mode, enabled=enabled, charger_trusted=trusted, cable_connected=cable, home=home
    )


def test_just_parked_poll_fires_when_parked():
    assert _wanted()
    # Away from home, a home charger says nothing.
    assert _wanted(trusted=True, home=False, cable=False)
    # Without a working charger sensor, home does not matter.
    assert _wanted(trusted=False, home=True, cable=True)


def test_just_parked_poll_skipped():
    assert not _wanted(enabled=False)
    # "Expecting a drive" already polls fast, and a drive or a charge cancels it.
    assert not _wanted(Mode.WAKING)
    assert not _wanted(Mode.DRIVING)
    assert not _wanted(Mode.CHARGING)
    assert not _wanted(None)
    # At home, or plugged in, with a working charger sensor: a charge shows there.
    assert not _wanted(trusted=True, home=True)
    assert not _wanted(trusted=True, cable=True)


def test_charge_poll_due_on_energy():
    assert charge_poll_due(True, charging=True, power_kw=7.0, reading_age=0)
    assert not charge_poll_due(
        False, charging=True, power_kw=7.0, reading_age=10 * CHARGE_DATA_MAX_AGE
    )


def test_charge_poll_due_with_no_power_once_the_reading_is_old():
    # A charge seen only in the car's data, with no power: no energy ever adds up.
    age = CHARGE_DATA_MAX_AGE
    assert not charge_poll_due(False, charging=True, power_kw=0.0, reading_age=age - 1)
    assert charge_poll_due(False, charging=True, power_kw=0.0, reading_age=age)
    # A solar dip riding out the stop delay is not polled for.
    assert not charge_poll_due(False, charging=False, power_kw=0.0, reading_age=10 * age)
