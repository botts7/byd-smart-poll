import math
from statistics import mean

import pytest
from bsp.charge_tracker import (
    ABOVE,
    BELOW,
    CLOSE_STEP,
    DEFAULT_KWH_PER_PCT,
    MAX_KWH_PER_PCT,
    RESUME_WINDOW,
    Bands,
    ChargeTracker,
    Learned,
    clamp_kwh_per_pct,
    kwh_per_pct_override,
    seed_kwh_per_pct,
    session_weight,
    starting_kwh_per_pct,
    stored_kwh_per_pct,
    stored_override,
)
from bsp.modes import Mode, Settings, decide_mode

MIN_GAP = 60  # seconds between polls, as enforced by the controller


def simulate(
    *,
    true_kpp: float,
    prior: float,
    power_kw,
    hours: float,
    start_soc: float = 40.0,
    phase: float = 0.37,
):
    """Charge a simulated car and poll whenever the tracker says so.

    `true_kpp` is the car's kWh per percent, or a function of SoC giving it.

    Returns (tracker, polls, ticks, staleness) where staleness is, for every real tick, the
    seconds until a poll saw it.
    """
    kpp = true_kpp if callable(true_kpp) else (lambda _soc: true_kpp)
    tracker = ChargeTracker(prior)
    soc = start_soc
    energy = phase * kpp(soc)  # energy into the current percent at the start
    tick_times: list[float] = []
    seen: list[float] = []
    last_poll = -math.inf
    polls = 0
    reported = soc
    for t in range(0, int(hours * 3600)):
        p = power_kw(t)
        tracker.set_power(t, p)
        energy += p / 3600
        while energy >= kpp(soc):
            energy -= kpp(soc)
            soc += 1
            tick_times.append(t)
        if tracker.poll_due(t) and t - last_poll >= MIN_GAP:
            last_poll = t
            polls += 1
            if soc != reported:
                seen.extend([t] * int(soc - reported))
                reported = soc
            tracker.on_reading(t, soc)
    staleness = [s - k for k, s in zip(tick_times, seen, strict=False)]  # last tick may be unseen
    return tracker, polls, len(tick_times), staleness


def test_steady_solar_charge_polls_about_once_per_percent():
    tracker, polls, ticks, stale = simulate(
        true_kpp=0.82, prior=0.6, power_kw=lambda t: 1.9, hours=6
    )
    assert ticks >= 13
    assert tracker.session_estimate == pytest.approx(0.82, rel=0.05)
    # Ignore the first few ticks while the phase is found and kWh/% learned.
    assert polls / ticks < 2.0
    assert mean(stale[3:]) < 3 * 60
    assert max(stale[3:]) < 6 * 60


def test_varying_solar_power_stays_current():
    # Clouds: power swings between 1.2 and 4.6 kW over a 20 minute cycle.
    def power(t):
        return 2.9 + 1.7 * math.sin(t / 600 * math.pi)

    tracker, polls, ticks, stale = simulate(true_kpp=0.82, prior=0.6, power_kw=power, hours=5)
    assert tracker.session_estimate == pytest.approx(0.82, rel=0.07)
    assert polls / ticks < 2.0
    assert mean(stale[3:]) < 3 * 60
    assert max(stale[3:]) < 6 * 60


def test_prior_too_high_still_learns():
    # A 44 kWh Dolphin with the default guess far too high.
    tracker, polls, ticks, stale = simulate(
        true_kpp=0.45, prior=1.2, power_kw=lambda t: 7.0, hours=2
    )
    assert tracker.session_estimate == pytest.approx(0.45, rel=0.07)
    assert mean(stale[4:]) < 3 * 60
    assert max(stale[4:]) < 7 * 60


def test_seven_kw_wallbox():
    tracker, polls, ticks, stale = simulate(
        true_kpp=0.82, prior=0.82, power_kw=lambda t: 7.2, hours=3
    )
    # At 7 kW a percent takes ~7 min; the 60 s gap limits how close polls can follow.
    assert polls / ticks < 1.6
    assert max(stale[2:]) < 3 * 60


def test_first_poll_due_at_session_start():
    tracker = ChargeTracker(0.8)
    assert tracker.poll_due(0)


def test_no_power_never_due_after_reading():
    tracker = ChargeTracker(0.8)
    tracker.on_reading(0, 50)
    tracker.set_power(0, 0)
    assert not tracker.poll_due(10_000)
    assert tracker.seconds_until_due(10_000) is None


def test_estimated_soc_never_reaches_next_percent_early():
    tracker = ChargeTracker(0.8)
    tracker.set_power(0, 3.6)  # 1 kWh per 1000 s
    tracker.on_reading(0, 50)
    tracker.on_reading(400, 51)  # tick between 0 and 400 s
    est = tracker.estimated_soc(400 + 3000)
    assert 51 < est < 52


def test_finish_blends_session_into_prior():
    tracker = ChargeTracker(0.6)
    tracker.learned[BELOW] = Learned(0.8, 0.05, pct=40)
    assert tracker.finish().below == pytest.approx(0.7)
    assert tracker.prior.below == pytest.approx(0.7)
    # Nothing seen above the edge: kept as it was.
    assert tracker.prior.above == pytest.approx(0.6)


def test_finish_without_estimate_keeps_prior():
    tracker = ChargeTracker(0.6)
    assert tracker.finish() == Bands(0.6, 0.6)


def test_finish_twice_blends_once():
    tracker = ChargeTracker(0.6)
    tracker.learned[BELOW] = Learned(0.8, 0.05, pct=40)
    tracker.finish()
    assert tracker.finish().below == pytest.approx(0.7)


def test_kept_value_is_what_finish_keeps_without_ending_the_session():
    tracker = ChargeTracker(0.6)
    tracker.learned[BELOW] = Learned(0.8, 0.05, pct=40)
    tracker.pause(0)
    assert tracker.kept_kwh_per_pct().below == pytest.approx(0.7)
    # Nothing blended in yet: finishing later still blends once, from the same prior.
    assert tracker.prior.below == pytest.approx(0.6)
    assert tracker.finish().below == pytest.approx(0.7)
    assert tracker.kept_kwh_per_pct().below == pytest.approx(0.7)


def test_resume_within_the_window_continues_the_session():
    # Solar: 3.6 kW (1 kWh per 1000 s), a tick seen, then a cloud.
    tracker = ChargeTracker(0.8)
    tracker.set_power(0, 3.6)
    tracker.on_reading(0, 50)
    tracker.on_reading(600, 51)
    due_at = tracker.next_poll_energy
    tracker.pause(700)
    energy = tracker.energy
    # No energy counted while paused.
    assert not tracker.poll_due(700 + 600)
    assert tracker.energy == pytest.approx(energy)
    assert tracker.resume(700 + 600, 3.6)
    # Not polled straight away: still due when the energy says so.
    assert tracker.next_poll_energy == due_at
    assert not tracker.poll_due(700 + 601)
    assert tracker.last_tick is not None and tracker.last_tick.soc == 51


def test_resume_after_the_window_starts_a_new_session():
    tracker = ChargeTracker(0.8)
    tracker.set_power(0, 3.6)
    tracker.on_reading(0, 50)
    tracker.pause(100)
    assert not tracker.resume(100 + RESUME_WINDOW + 1, 3.6)
    assert tracker.last_tick is None
    assert tracker.poll_due(100 + RESUME_WINDOW + 1)


def test_resume_without_a_pause_or_after_finish_starts_afresh():
    tracker = ChargeTracker(0.8)
    tracker.set_power(0, 3.6)
    tracker.on_reading(0, 50)
    assert not tracker.resume(10, 3.6)
    tracker.on_reading(20, 50)
    tracker.pause(30)
    tracker.finish()
    assert not tracker.resume(40, 3.6)
    assert tracker.poll_due(40)


def test_soc_drop_restarts_phase():
    tracker = ChargeTracker(0.8)
    tracker.set_power(0, 3.6)
    tracker.on_reading(0, 50)
    tracker.on_reading(500, 51)
    tracker.on_reading(600, 49)
    assert tracker.last_tick is None


def test_clamp():
    assert clamp_kwh_per_pct(0.01) == 0.15
    assert clamp_kwh_per_pct(9) == 3.0


@pytest.mark.parametrize(
    ("vehicle_on", "charging", "waking", "mode"),
    [
        (True, True, True, Mode.DRIVING),
        (False, True, True, Mode.CHARGING),
        (False, False, True, Mode.WAKING),
        (False, False, False, Mode.PARKED),
    ],
)
def test_decide_mode(vehicle_on, charging, waking, mode):
    assert decide_mode(vehicle_on=vehicle_on, charging=charging, waking=waking) is mode


def test_charging_and_parked_keep_gps_slow():
    s = Settings()
    assert s.intervals(Mode.CHARGING).gps == s.parked_gps
    assert s.intervals(Mode.WAKING).gps == s.parked_gps
    assert s.intervals(Mode.DRIVING).gps == s.driving_gps
    assert s.intervals(Mode.PARKED).telemetry == s.parked_telemetry


def test_pick_originals_keeps_user_values():
    from bsp.modes import Intervals, pick_originals

    assert pick_originals(Intervals(600, 900), Settings()) == Intervals(600, 900)


def test_pick_originals_ignores_our_own_values():
    from bsp.modes import BYD_DEFAULTS, Intervals, pick_originals

    s = Settings()
    assert pick_originals(Intervals(3600, 28800), s) == BYD_DEFAULTS
    assert pick_originals(s.intervals(Mode.DRIVING), s) == BYD_DEFAULTS
    assert pick_originals(None, s) == BYD_DEFAULTS


def test_estimate_keeps_counting_when_car_data_is_stale():
    # 2026-10-29: the car's last answer was 67 % at 04:42 while it kept charging at 7 kW
    # until 06:00. It reported 77 % once it answered again.
    tracker = ChargeTracker(0.91)
    tracker.set_power(0, 7.0)
    tracker.on_reading(0, 66)
    tracker.on_reading(470, 67)  # tick seen
    t = 470 + 78 * 60
    assert tracker.estimated_soc(t) < 68  # normal: never past the next percent
    # The car read 77 % once it answered again.
    assert tracker.estimated_soc(t, stale=True) == pytest.approx(77.0, abs=1.0)


def test_stale_estimate_without_a_tick_never_overestimates():
    tracker = ChargeTracker(1.0)
    tracker.set_power(0, 3.6)  # 1 kWh per 1000 s
    tracker.on_reading(0, 50)
    assert tracker.estimated_soc(2000, stale=True) == pytest.approx(52.0)
    assert tracker.estimated_soc(2000) == 50


def test_data_stale_after_two_refreshes_without_news():
    from bsp.signals import data_stale

    assert not data_stale(0)
    assert not data_stale(1)
    assert data_stale(2)


def test_starting_kwh_per_pct_keeps_the_learned_value():
    assert starting_kwh_per_pct((0.72, 0.8), None, None, 0.6) == (0.72, 0.8)
    # Nothing learned yet: the setting, the guess from the battery size or the default.
    assert starting_kwh_per_pct(None, None, None, 0.6) == (0.6, 0.6)
    assert starting_kwh_per_pct(None, None, None, 0.92) == (0.92, 0.92)


def test_starting_kwh_per_pct_keeps_the_learned_value_when_a_battery_size_is_entered():
    # The battery size gives a new starting guess, but what charging learned is better.
    assert starting_kwh_per_pct((0.88, 1.0), None, None, 0.9167) == (0.88, 1.0)


def test_starting_kwh_per_pct_starts_again_when_energy_per_percent_is_typed():
    assert starting_kwh_per_pct((0.62, 0.7), None, 0.85, 0.85) == (0.85, 0.85)
    assert starting_kwh_per_pct((0.62, 0.7), 0.8, 0.85, 0.85) == (0.85, 0.85)
    # The same setting as the learned values started from: kept.
    assert starting_kwh_per_pct((0.62, 0.7), 0.85, 0.85, 0.85) == (0.62, 0.7)


def test_kwh_per_pct_override_only_when_typed():
    assert kwh_per_pct_override(None) is None
    assert kwh_per_pct_override(0) is None
    assert kwh_per_pct_override(0.85) == 0.85
    assert kwh_per_pct_override(9.0) == MAX_KWH_PER_PCT


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ({}, None),
        ({"kwh_per_pct_override": None}, None),
        ({"kwh_per_pct_override": 0.85}, 0.85),
        # 0.2.0 saved the field's value, 0.6 when left alone (dropped from the options since).
        ({"kwh_per_pct_setting": 0.6}, None),
        ({"kwh_per_pct_setting": 0.9}, 0.9),
        # The newer key wins.
        ({"kwh_per_pct_override": None, "kwh_per_pct_setting": 0.9}, None),
        ({"kwh_per_pct_override": "x"}, None),
    ],
)
def test_stored_override(stored, expected):
    assert stored_override(stored) == expected


# ---------- two bands ----------


def sealion(soc: float) -> float:
    """kWh per percent measured on a Sealion 7: about 10 % more from 80 % up."""
    return 0.90 if soc < 80 else 1.0


def test_two_bands_are_learned_apart():
    tracker, polls, ticks, stale = simulate(
        true_kpp=sealion, prior=0.6, power_kw=lambda t: 7.0, hours=6, start_soc=50
    )
    assert tracker.learned[BELOW].value == pytest.approx(0.90, rel=0.03)
    assert tracker.learned[ABOVE].value == pytest.approx(1.0, rel=0.05)
    # Each band only from its own percents.
    assert tracker.learned[BELOW].pct <= 80 - 51
    assert tracker.learned[ABOVE].pct >= 10
    assert max(stale[3:]) < 3 * 60


def test_each_band_is_used_on_its_own_side_of_the_edge():
    # A session across the edge: one value for both would land between the two.
    tracker, *_ = simulate(
        true_kpp=sealion, prior=0.95, power_kw=lambda t: 7.0, hours=6, start_soc=50
    )
    assert tracker.bands.at(70) == pytest.approx(0.90, rel=0.03)
    assert tracker.bands.at(90) == pytest.approx(1.0, rel=0.05)


def test_rate_in_use_follows_the_battery_level():
    tracker = ChargeTracker(Bands(0.9, 1.0, 80))
    tracker.on_reading(0, 79)
    assert tracker.kwh_per_pct == pytest.approx(0.9)
    tracker.on_reading(10, 80)
    assert tracker.kwh_per_pct == pytest.approx(1.0)


def test_a_tick_at_the_edge_ends_the_lower_band_and_starts_the_upper_one():
    tracker = ChargeTracker(Bands(0.9, 1.0, 80))
    tracker.set_power(0, 3.6)  # 1 kWh per 1000 s
    tracker.on_reading(0, 77)
    soc, ts = 77, 450.0
    while soc < 90:
        # A poll just before and just after each tick, so each one is seen to the second.
        tracker.on_reading(ts - 1, soc)
        soc += 1
        tracker.on_reading(ts, soc)
        ts += (0.9 if soc < 80 else 1.0) * 1000
    # 78 to 80 below, and 80 to 90 above: the upper band does not lose its first percent.
    assert tracker.learned[BELOW].pct == 2
    assert tracker.learned[BELOW].value == pytest.approx(0.9)
    assert tracker.learned[ABOVE].pct == 10
    assert tracker.learned[ABOVE].value == pytest.approx(1.0)


def test_polls_above_the_edge_are_timed_with_the_upper_band():
    tracker = ChargeTracker(Bands(0.9, 1.2, 80))
    tracker.set_power(0, 3.6)  # 1 kWh per 1000 s
    tracker.on_reading(0, 84)
    tracker.on_reading(100, 85)  # tick between 0 and 100 s
    assert tracker.next_poll_energy == pytest.approx(0.05 + 1.2 * 1.05)


def test_bands_split_energy_and_soc():
    bands = Bands(0.9, 1.0, 80)
    assert bands.split(78, 82) == (2, 2)
    assert bands.split(81, 83) == (0, 2)
    assert bands.energy(78, 82) == pytest.approx(3.8)
    assert bands.soc_after(78, 3.8) == pytest.approx(82)
    assert bands.soc_after(78, 0.9) == pytest.approx(79)
    assert bands.soc_after(85, 1.0) == pytest.approx(86)


def test_stale_estimate_crosses_the_edge_at_each_band_rate():
    tracker = ChargeTracker(Bands(0.9, 1.2, 80))
    tracker.set_power(0, 3.6)  # 1 kWh per 1000 s
    tracker.on_reading(0, 78)
    # 3.0 kWh: 1.8 to reach 80, then 1.2 for one more percent.
    assert tracker.estimated_soc(3000, stale=True) == pytest.approx(81.0)


def test_short_session_moves_the_stored_value_less():
    def kept_after(pct: float) -> float:
        tracker = ChargeTracker(0.9)
        tracker.learned[BELOW] = Learned(1.1, 0.05, pct=pct)
        return tracker.finish().below

    short, long = kept_after(9), kept_after(42)
    assert 0.9 < short < long
    assert long == pytest.approx(1.0)  # 40 % or more: halfway
    assert short - 0.9 == pytest.approx((long - 0.9) * 9 / 40)


def test_session_weight():
    assert session_weight(0) == 0
    assert session_weight(20) == pytest.approx(0.25)
    assert session_weight(80) == pytest.approx(0.5)


def test_upper_band_rate_is_used_between_ticks_above_the_edge():
    tracker = ChargeTracker(Bands(0.5, 1.0, 80))
    tracker.on_reading(0, 84)
    tracker.set_power(0, 3.6)  # 1 kWh per 1000 s
    # Ticks to 85 somewhere in the first kWh: placed at 0.5 kWh.
    tracker.on_reading(1000, 85)
    # Half a kWh past the tick is half a percent at 1.0 kWh per percent, not a whole one.
    assert tracker.estimated_soc(1000) == pytest.approx(85.5)
    # An early reading still at 85: the next poll a close step on at the upper rate.
    tracker.on_reading(1500, 85)
    assert tracker.next_poll_energy == pytest.approx(1.5 + CLOSE_STEP * 1.0)


def test_only_the_bands_a_session_learned_move():
    tracker = ChargeTracker(Bands(0.9, 1.0, 80))
    tracker.learned[ABOVE] = Learned(1.1, 0.05, pct=40)
    kept = tracker.finish()
    assert kept.below == pytest.approx(0.9)
    assert kept.above == pytest.approx(1.05)


def test_stored_values_from_before_the_bands_are_used_for_both():
    assert stored_kwh_per_pct({"kwh_per_pct": 0.93}) == (0.93, 0.93)
    assert stored_kwh_per_pct({"kwh_per_pct_below": 0.9, "kwh_per_pct_above": 1.0}) == (0.9, 1.0)
    # The bands are stored with the old key kept equal to the lower band.
    stored = {"kwh_per_pct": 0.9, "kwh_per_pct_below": 0.9, "kwh_per_pct_above": 1.0}
    assert stored_kwh_per_pct(stored) == (0.9, 1.0)
    # An old key that differs was learned by an older version after a downgrade: it is newer,
    # and replaces the lower band.
    stored = {"kwh_per_pct": 0.85, "kwh_per_pct_below": 0.9, "kwh_per_pct_above": 1.0}
    assert stored_kwh_per_pct(stored) == (0.85, 1.0)
    assert stored_kwh_per_pct({}) is None
    assert stored_kwh_per_pct({"kwh_per_pct": "bad"}) is None


# ---------- first guess ----------


def test_seed_from_the_battery_size():
    # An 82.5 kWh pack with 10 % charging losses.
    assert seed_kwh_per_pct(None, 82.5) == pytest.approx(0.9167, abs=1e-4)
    assert seed_kwh_per_pct(None, 44.9) == pytest.approx(0.4989, abs=1e-4)


def test_seed_setting_overrides_the_battery_size():
    assert seed_kwh_per_pct(0.85, 82.5) == 0.85


def test_seed_without_either_is_the_default():
    assert seed_kwh_per_pct(None, None) == DEFAULT_KWH_PER_PCT
    assert seed_kwh_per_pct(0, None) == DEFAULT_KWH_PER_PCT


def test_seed_is_kept_in_range():
    assert seed_kwh_per_pct(None, 500) == MAX_KWH_PER_PCT
