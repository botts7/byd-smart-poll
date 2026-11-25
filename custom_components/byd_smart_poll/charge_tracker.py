"""Times polls during a charge from the energy added, so a poll lands just after SoC ticks over.

Pure logic, no Home Assistant imports. Time is in seconds (monotonic), energy in kWh, power in kW.

How it works:
- Energy is integrated from the charging power (the wallbox if configured, otherwise the car's
  own battery power from its last reading, held constant between readings).
- The car only reports whole percent. When a reading shows SoC went up, the tick happened
  somewhere between the previous reading and this one (the observed window).
- That window is narrowed with the tick predicted from the previous one plus one percent's
  worth of energy. Without this, a late poll leaves the tick anywhere in a wide window and
  the next poll lands early, costing extra polls.
- The next poll is due at the latest the next tick can be, so it does not land early.
- If a poll does land early (SoC unchanged) the next one follows after a small step.
- kWh per percent is learned from the observed windows only (not the predictions, which
  would feed the estimate back into itself), between the first and latest tick of the
  session. Its error shrinks as the session goes on, so it is used once below 15 %.
- It is learned and kept for two SoC bands, below and above a band edge (80 % by default):
  near full a car takes more energy per percent (a Sealion 7 took about 0.90 kWh per percent
  from 40 to 60 % and 1.0 to 1.04 above 75 %). Each band learns only from ticks inside it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

MIN_KWH_PER_PCT = 0.15
MAX_KWH_PER_PCT = 3.0
# Without a battery size: about right for a 50 kWh pack.
DEFAULT_KWH_PER_PCT = 0.6
# Battery energy per kWh from the wall, for the first guess from the battery size.
CHARGE_EFFICIENCY = 0.9
# Small enough for plug-in hybrids (8.3 kWh in some DM-i models).
MIN_BATTERY_KWH = 5.0
MAX_BATTERY_KWH = 250.0
# Percent from the band edge up count as the upper band (see Bands).
DEFAULT_BAND_EDGE = 80.0
MIN_BAND_EDGE = 50.0
MAX_BAND_EDGE = 95.0
BELOW = "below"
ABOVE = "above"

# Polls before the first tick is seen are spaced at half a percent, to find the phase quickly.
FIRST_STEP = 0.5
# After an early poll, close to where the tick is expected.
CLOSE_STEP = 0.15
# Aim a little past the latest expected tick, for noise in the energy measurement.
TICK_MARGIN = 0.05
LEARN_MAX_REL_ERROR = 0.15
# Relative error assumed for kWh per percent before the session has learned it.
PRIOR_REL_ERROR = 0.25
# Blend of a finished session's estimate into the stored value, for a session that covered
# FULL_WEIGHT_PCT percent or more of the band. A shorter one counts for less in proportion: a
# 9 % top-up has wider error than a 42 % overnight charge and must not move it as far.
SESSION_WEIGHT = 0.5
FULL_WEIGHT_PCT = 40.0
# Charging that starts again within this long (seconds) of pausing continues the same session.
# Solar charging stops and starts with clouds: each new session polled at once, and each pause
# polled for the final SoC, two polls per cloud.
RESUME_WINDOW = 20 * 60


def clamp_kwh_per_pct(value: float) -> float:
    return min(MAX_KWH_PER_PCT, max(MIN_KWH_PER_PCT, value))


def kwh_per_pct_override(value: float | None) -> float | None:
    """The "energy per 1%" setting typed in, clamped, or None when it is not set."""
    return clamp_kwh_per_pct(float(value)) if value else None


def seed_kwh_per_pct(override: float | None, battery_kwh: float | None) -> float:
    """The kWh per percent to start learning from, for both bands.

    The "energy per 1%" setting when set, else worked out from the usable battery size and
    charging losses (an 82.5 kWh pack: 0.92 kWh), else DEFAULT_KWH_PER_PCT.
    """
    if (typed := kwh_per_pct_override(override)) is not None:
        return typed
    if battery_kwh:
        return clamp_kwh_per_pct(float(battery_kwh) / 100 / CHARGE_EFFICIENCY)
    return DEFAULT_KWH_PER_PCT


def session_weight(pct: float) -> float:
    """How far a finished session moves a band's stored value, from the percent it covered."""
    return SESSION_WEIGHT * min(1.0, max(0.0, pct) / FULL_WEIGHT_PCT)


@dataclass(frozen=True)
class Bands:
    """kWh per percent below and above the band edge.

    The percent from SoC p to p + 1 is in the upper band when p is at or above the edge.
    """

    below: float
    above: float
    edge: float = DEFAULT_BAND_EDGE

    @classmethod
    def single(cls, value: float, edge: float = DEFAULT_BAND_EDGE) -> Bands:
        return cls(value, value, edge)

    def clamped(self) -> Bands:
        return replace(
            self, below=clamp_kwh_per_pct(self.below), above=clamp_kwh_per_pct(self.above)
        )

    def get(self, band: str) -> float:
        return self.above if band == ABOVE else self.below

    def at(self, soc: float | None) -> float:
        """kWh per percent for the percent starting at `soc` (the lower band if unknown)."""
        return self.above if soc is not None and soc >= self.edge else self.below

    def split(self, soc_from: float, soc_to: float) -> tuple[float, float]:
        """The percent from soc_from up to soc_to that lie below and above the edge."""
        below = max(0.0, min(soc_to, self.edge) - soc_from)
        above = max(0.0, soc_to - max(soc_from, self.edge))
        return below, above

    def energy(self, soc_from: float, soc_to: float) -> float:
        """kWh to charge from soc_from to soc_to."""
        below, above = self.split(soc_from, soc_to)
        return below * self.below + above * self.above

    def soc_after(self, soc: float, energy: float) -> float:
        """The SoC reached from `soc` with `energy` kWh added."""
        if soc < self.edge:
            to_edge = (self.edge - soc) * self.below
            if energy <= to_edge:
                return soc + energy / self.below
            return self.edge + (energy - to_edge) / self.above
        return soc + energy / self.above


def stored_kwh_per_pct(stored: Mapping[str, Any]) -> tuple[float, float] | None:
    """The learned kWh per percent below and above the edge kept in the store, or None.

    Stores from before the two bands kept one value ("kwh_per_pct"): it is used for both, and
    the bands part from there as charges above the edge are seen. The bands keep that value
    equal to the lower one, so if it differs a version from before the bands learned it after
    a downgrade: it is the newer, and replaces the lower band.
    """
    try:
        if stored.get("kwh_per_pct_below") is not None:
            below = float(stored["kwh_per_pct_below"])
            above = float(stored.get("kwh_per_pct_above", below))
            if stored.get("kwh_per_pct") is not None:
                below = float(stored["kwh_per_pct"])
            return below, above
        if stored.get("kwh_per_pct") is not None:
            single = float(stored["kwh_per_pct"])
            return single, single
    except (TypeError, ValueError):
        pass
    return None


# The "energy per 1%" setting the learned values started from (None: not set).
OVERRIDE_KEY = "kwh_per_pct_override"
# Before that key: the configured value they started from, the setting or the default.
LEGACY_SETTING_KEY = "kwh_per_pct_setting"


def stored_override(stored: Mapping[str, Any]) -> float | None:
    """The "energy per 1%" setting the stored learned values started from, or None.

    A store from before OVERRIDE_KEY kept the configured value instead. In 0.2.0 that was the
    field's value, saved as 0.6 when left alone and dropped from the options since (see
    migrate_options), so 0.6 there was no typed setting.
    """
    try:
        if OVERRIDE_KEY in stored:
            value = stored[OVERRIDE_KEY]
            return None if value is None else float(value)
        legacy = stored.get(LEGACY_SETTING_KEY)
        if legacy is None or math.isclose(float(legacy), DEFAULT_KWH_PER_PCT):
            return None
        return clamp_kwh_per_pct(float(legacy))
    except (TypeError, ValueError):
        return None


def starting_kwh_per_pct(
    learned: tuple[float, float] | None,
    override_from: float | None,
    override: float | None,
    configured: float,
) -> tuple[float, float]:
    """The kWh per percent to start from, below and above the edge.

    `configured` (the setting, else from the battery size, else the default) is only the
    starting guess while nothing has been learned. After that the learned values are kept,
    also when a battery size is entered later: what charging measured is better than a guess
    from it. Only typing a new "energy per 1%" (`override`, differing from `override_from`,
    the one the learned values started from) starts both bands again from it.
    """
    if learned is None:
        return configured, configured
    if override is not None and (
        override_from is None or not math.isclose(override, override_from)
    ):
        return override, override
    return learned


@dataclass(frozen=True)
class Learned:
    """What a session has learned about one band so far."""

    value: float
    rel_error: float
    # Percent between the first and latest tick it was learned from.
    pct: float


@dataclass(frozen=True)
class Tick:
    soc: float
    # Best estimate of where the tick is, narrowed with the prediction.
    lo: float
    hi: float
    # What the readings alone showed.
    seen_lo: float
    seen_hi: float

    @property
    def energy(self) -> float:
        return (self.lo + self.hi) / 2


class ChargeTracker:
    def __init__(self, kwh_per_pct: float | Bands = DEFAULT_KWH_PER_PCT) -> None:
        prior = kwh_per_pct if isinstance(kwh_per_pct, Bands) else Bands.single(kwh_per_pct)
        # What a session starts from: the stored values, with each finished session blended in.
        self.prior = prior.clamped()
        self.reset()

    def reset(self) -> None:
        """Start a new charging session."""
        self.energy = 0.0
        self.learned: dict[str, Learned] = {}
        # Zero means "poll now": the first reading of a session is wanted straight away.
        self.next_poll_energy: float = 0.0
        self._power_kw = 0.0
        self._ts: float | None = None
        self._soc: float | None = None
        self._soc_energy = 0.0
        # The first tick seen in each band, which that band learns from.
        self._first_tick: dict[str, Tick] = {}
        self._last_tick: Tick | None = None
        # When charging paused with the session kept (see pause), and whether finish has run.
        self.paused_at: float | None = None
        self._finished = False

    @property
    def bands(self) -> Bands:
        """kWh per percent in use: what this session learned, else the prior, per band."""
        below = self.learned.get(BELOW)
        above = self.learned.get(ABOVE)
        return replace(
            self.prior,
            below=below.value if below else self.prior.below,
            above=above.value if above else self.prior.above,
        )

    @property
    def kwh_per_pct(self) -> float:
        """kWh per percent in use for the car's last reading."""
        return self.bands.at(self._soc)

    @property
    def session_estimate(self) -> float | None:
        """What this session learned for the band of the car's last reading, if anything.

        Only for tests and diagnostics: the polls and estimates use bands.
        """
        band = ABOVE if self._soc is not None and self._soc >= self.prior.edge else BELOW
        learned = self.learned.get(band)
        return learned.value if learned else None

    @property
    def power_kw(self) -> float:
        return self._power_kw

    @property
    def last_tick(self) -> Tick | None:
        return self._last_tick

    def advance(self, ts: float) -> None:
        if self._ts is not None and ts > self._ts:
            self.energy += self._power_kw * (ts - self._ts) / 3600
        if self._ts is None or ts > self._ts:
            self._ts = ts

    def set_power(self, ts: float, power_kw: float) -> None:
        self.advance(ts)
        self._power_kw = max(0.0, power_kw)

    def pause(self, ts: float) -> None:
        """Charging stopped, maybe only for a while: no more energy, but the session is kept."""
        self.set_power(ts, 0.0)
        self.paused_at = ts

    def resume(self, ts: float, power_kw: float) -> bool:
        """Charging started. Returns True if it continues the paused session.

        Within RESUME_WINDOW of a pause the session goes on where it was: the next poll stays
        due when the energy says so, rather than straight away. Otherwise a new one starts,
        after finishing a paused one (see prior).
        """
        continued = (
            self.paused_at is not None
            and not self._finished
            and ts - self.paused_at <= RESUME_WINDOW
        )
        if not continued:
            if self.paused_at is not None:
                # Paused too long: that session is over, and what it learned is kept.
                self.finish()
            self.reset()
        self.paused_at = None
        self.set_power(ts, power_kw)
        return continued

    def poll_due(self, ts: float) -> bool:
        self.advance(ts)
        return self.energy >= self.next_poll_energy

    def seconds_until_due(self, ts: float) -> float | None:
        self.advance(ts)
        if self.energy >= self.next_poll_energy:
            return 0.0
        if self._power_kw <= 0:
            return None
        return (self.next_poll_energy - self.energy) / self._power_kw * 3600

    def on_reading(self, ts: float, soc: float) -> None:
        """A fresh SoC reading arrived from the car."""
        self.advance(ts)
        e = self.energy
        bands = self.bands
        if self._soc is None or soc < self._soc:
            self._soc, self._soc_energy = soc, e
            self._last_tick = None
            self._first_tick = {}
            self.next_poll_energy = e + FIRST_STEP * bands.at(soc)
            return

        if soc == self._soc:
            # Early: the tick is still ahead. The old value held up to here.
            self._soc_energy = e
            step = CLOSE_STEP if self._last_tick is not None else FIRST_STEP
            self.next_poll_energy = e + step * bands.at(soc)
            return

        seen_lo, seen_hi = self._soc_energy, e
        lo, hi = seen_lo, seen_hi
        prev = self._last_tick
        if prev is not None:
            # Percents on each side of the edge count at their own band's rate and error.
            predicted = prev.energy + bands.energy(prev.soc, soc)
            below, above = bands.split(prev.soc, soc)
            spread = (
                (prev.hi - prev.lo) / 2
                + below * bands.below * self._rel_error(BELOW)
                + above * bands.above * self._rel_error(ABOVE)
            )
            lo, hi = max(lo, predicted - spread), min(hi, predicted + spread)
            if lo > hi:
                # The prediction disagrees with what was seen: trust the readings.
                lo, hi = seen_lo, seen_hi
        tick = Tick(soc=soc, lo=lo, hi=hi, seen_lo=seen_lo, seen_hi=seen_hi)
        # A band learns from its first and latest tick with only its own percents between
        # them. A tick at the edge ends the lower band and starts the upper one.
        for band, inside in ((BELOW, soc <= bands.edge), (ABOVE, soc >= bands.edge)):
            if not inside:
                continue
            first = self._first_tick.get(band)
            if first is None:
                self._first_tick[band] = tick
            else:
                self._learn(band, first, tick)
        self._last_tick = tick
        self._soc, self._soc_energy = soc, e
        rate = self.bands.at(soc)
        self.next_poll_energy = max(
            tick.energy + rate * (1 + TICK_MARGIN),
            e + CLOSE_STEP * rate,
        )

    def _rel_error(self, band: str) -> float:
        learned = self.learned.get(band)
        return learned.rel_error if learned else PRIOR_REL_ERROR

    def _learn(self, band: str, first: Tick, latest: Tick) -> None:
        d_soc = latest.soc - first.soc
        d_energy = (latest.seen_lo + latest.seen_hi - first.seen_lo - first.seen_hi) / 2
        if d_soc <= 0 or d_energy <= 0:
            return
        error = (first.seen_hi - first.seen_lo + latest.seen_hi - latest.seen_lo) / 2
        if error / d_energy > LEARN_MAX_REL_ERROR:
            return
        self.learned[band] = Learned(
            value=clamp_kwh_per_pct(d_energy / d_soc), rel_error=error / d_energy, pct=d_soc
        )

    def estimated_soc(self, ts: float, *, stale: bool = False) -> float | None:
        """SoC between readings, from the energy added since the last tick.

        Never reaches the next whole percent before the car reports it, unless the car's
        data is stale (it stopped answering, eg an internet outage). Then it keeps counting
        from the energy going in, so automations still see the battery fill up.
        """
        if self._soc is None:
            return None
        self.advance(ts)
        bands = self.bands
        ticked = self._last_tick is not None and self._last_tick.soc == self._soc
        if stale:
            # Without a tick the reading's own energy is the base: an underestimate of up to
            # one percent, never an overestimate.
            base = self._last_tick.energy if ticked and self._last_tick else self._soc_energy
            return min(100.0, bands.soc_after(self._soc, max(0.0, self.energy - base)))
        if not ticked or self._last_tick is None:
            return self._soc
        fraction = (self.energy - self._last_tick.energy) / bands.at(self._soc)
        return min(100.0, self._soc + min(max(fraction, 0.0), 0.99))

    def kept_kwh_per_pct(self) -> Bands:
        """The kWh per percent finish would keep now, without ending the session.

        Each band the session learned is blended into the prior by how many percent it
        covered (session_weight). Stored when charging pauses: Home Assistant does not unload
        on shutdown, so a restart before the pause runs out would otherwise lose what the
        session learned.
        """
        if not self.learned or self._finished:
            return self.prior
        kept: dict[str, float] = {}
        for band in (BELOW, ABOVE):
            prior = self.prior.get(band)
            learned = self.learned.get(band)
            if learned is None:
                kept[band] = prior
                continue
            weight = session_weight(learned.pct)
            kept[band] = clamp_kwh_per_pct(prior * (1 - weight) + learned.value * weight)
        return replace(self.prior, below=kept[BELOW], above=kept[ABOVE])

    def finish(self) -> Bands:
        """End the session. Returns the kWh per percent to keep for the next one.

        Only the first call counts, so a session is never blended in twice.
        """
        self.paused_at = None
        self.prior = self.kept_kwh_per_pct()
        self._finished = True
        return self.prior
