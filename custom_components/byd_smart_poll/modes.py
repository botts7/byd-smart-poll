"""Polling modes and the intervals each one uses. Pure logic, no Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .const import CHARGING_MIN_POWER_W, CHARGING_STOP_DELAY, WAKE_WINDOW


class Mode(StrEnum):
    PARKED = "parked"
    CHARGING = "charging"
    DRIVING = "driving"
    # Parked, but something hinted the car is about to be used (a phone connected to it).
    # Polls at the driving rate for a short window so a drive start is seen quickly.
    WAKING = "waking"


@dataclass(frozen=True)
class Intervals:
    """Poll intervals in seconds, as written to the BYD integration's number entities."""

    telemetry: int
    gps: int


@dataclass(frozen=True)
class Settings:
    # Parked away from home, or when where the car is cannot be told.
    parked_telemetry: int = 3600
    # Parked at home. At home the charger and phones report charging and drives without
    # waking the car, so this can be longer. Same as away until tested.
    parked_home_telemetry: int = 3600
    parked_gps: int = 28800
    driving_telemetry: int = 60
    driving_gps: int = 120
    # While charging, polls are timed from energy added (see charge_tracker). This is only the
    # safety net in case the energy estimate is wrong.
    charging_safety: int = 1800
    # Not intervals, but set in the same options step. Charger power (W) that counts as
    # charging, how long charging must stop before it counts as stopped (s), and how long a
    # wake hint polls at the driving rate (s).
    charging_min_power_w: float = CHARGING_MIN_POWER_W
    charging_stop_delay: int = CHARGING_STOP_DELAY
    wake_window: int = WAKE_WINDOW

    def intervals(self, mode: Mode, *, home: bool = False) -> Intervals:
        if mode is Mode.DRIVING:
            return Intervals(self.driving_telemetry, self.driving_gps)
        if mode is Mode.WAKING:
            # GPS stays slow: the car has not moved yet.
            return Intervals(self.driving_telemetry, self.parked_gps)
        if mode is Mode.CHARGING:
            # The car cannot move while charging, so GPS stays at the parked rate.
            return Intervals(self.charging_safety, self.parked_gps)
        telemetry = self.parked_home_telemetry if home else self.parked_telemetry
        return Intervals(telemetry, self.parked_gps)


# The BYD Vehicle integration's own defaults (DEFAULT_POLL_INTERVAL, DEFAULT_GPS_POLL_INTERVAL).
BYD_DEFAULTS = Intervals(telemetry=300, gps=300)


def pick_originals(current: Intervals | None, settings: Settings) -> Intervals:
    """The intervals to put back when smart polling is turned off or removed.

    The current values are only trusted if they are not ones this integration sets itself.
    Otherwise, for example after an earlier setup whose saved originals were lost, they
    would be our own values, and turning this off would leave the car on them.
    """
    ours = {settings.intervals(mode, home=home) for mode in Mode for home in (False, True)}
    if current is None or current in ours:
        return BYD_DEFAULTS
    return current


def poll_wait(last_poll: float, now: float, gap: float) -> float:
    """Seconds until another poll is allowed (0 if now). Polls inside the gap are put off."""
    return max(0.0, last_poll + gap - now)


def poll_action(wait: float, *, put_off: bool) -> str:
    """What to do with a poll request: "now", "later" (when the gap ends) or "skip".

    Charge polls (put_off=False) inside the gap are skipped: each is a repeat of one just
    sent (the wallbox reports every few seconds), and putting them off made an early poll
    every minute for a whole charge (2026-10-27). Every other poll (a faster rate, unplugged,
    just parked) is put off until the gap ends.
    """
    if wait <= 0:
        return "now"
    return "later" if put_off else "skip"


def stale_backoff(stale_polls: int) -> float:
    """Seconds to wait before the next charge poll after polls that brought nothing new.

    When the car stops answering, BYD hands back its last reading, so the next percent never
    seems to arrive and the poll stays due. Without this it polled every minute for over an
    hour (2026-10-29, 04:50 to 06:03). Doubles from 1 min up to 30 min.
    """
    return float(min(60 * 2 ** max(stale_polls, 0), 1800))


def counts_stale_polls(mode: Mode | None) -> bool:
    """Whether our polls that bring back the car's last reading count as stale data now.

    Only driving and charging, when the car is awake and uploads often. Parked, or expecting
    a drive that may be a false wake, a car that sleeps or only pushes its data brings back
    its last reading and is still answering.
    """
    return mode in (Mode.DRIVING, Mode.CHARGING)


def stale_count_restarts(previous: Mode | None, mode: Mode) -> bool:
    """Whether a new drive or charge starts, so the stale count starts again from zero.

    Polls that brought nothing at the end of one charge and at the start of the next (the car
    asleep for its scheduled charge) are not two in a row. A drive straight into a charge is
    one spell and keeps its count.
    """
    return counts_stale_polls(mode) and not counts_stale_polls(previous)


def drive_ended(previous: Mode | None, mode: Mode) -> bool:
    """Whether a drive just ended, so one GPS fix should be taken while the car is awake.

    The last fix while driving can be minutes before parking, and the parked GPS rate (8 h)
    takes effect at once, so without this the car shows where it was, not where it is.
    """
    return previous is Mode.DRIVING and mode is not Mode.DRIVING


# Extra polls this long after a drive ends, while the car is likely still awake.
JUST_PARKED_POLLS = (180.0, 480.0)


def just_parked_polls(previous: Mode | None, mode: Mode) -> tuple[float, ...]:
    """Delays of the extra polls to take after a drive ends, or none.

    Without a charger signal a charge that starts after parking (plugged in at home, or a
    fast charger) is only seen at the next parked poll, up to an hour later, and most fast
    charges are over by then. Two polls a few minutes after parking catch it while the car
    is still awake, so they cost little.
    """
    if drive_ended(previous, mode) and mode is not Mode.CHARGING:
        return JUST_PARKED_POLLS
    return ()


def keeps_just_parked_polls(mode: Mode) -> bool:
    """Whether pending just parked polls still apply after a change to this mode.

    A new drive or a charge makes them pointless. "Expecting a drive" keeps them: it can
    still be running from before the drive, and it ends on its own.
    """
    return mode in (Mode.PARKED, Mode.WAKING)


def just_parked_poll_wanted(
    mode: Mode | None,
    *,
    enabled: bool,
    charger_trusted: bool,
    cable_connected: bool | None,
    home: bool | None,
) -> bool:
    """Whether a just parked poll should go out now.

    Only while parked: "expecting a drive" already polls at the driving rate. With a working
    charger power sensor, a charge at home is seen without polling, so the poll is only worth
    it away from home (or where that is not known) with the cable not in.
    """
    if not enabled or mode is not Mode.PARKED:
        return False
    return not (charger_trusted and (cable_connected is True or home is True))


def decide_mode(*, vehicle_on: bool, charging: bool, waking: bool) -> Mode:
    """Pick the mode from what is known right now.

    Plugged in but not charging (finished, paused by solar, waiting for a schedule) counts as
    parked: the car draws from the traction battery then, so every wake costs SoC.
    """
    if vehicle_on:
        return Mode.DRIVING
    if charging:
        return Mode.CHARGING
    if waking:
        return Mode.WAKING
    return Mode.PARKED


# Polls in a row during "expecting a drive" that must find the car off before it ends early.
WAKE_OFF_POLLS = 2
# Only polls at least this far apart (and from the window's start) count. The poll that
# starts the faster rate goes out as the window opens, before the driver can have started
# the car, and one refresh can write the car's timestamp more than once.
WAKE_OFF_SPACING = 30.0
# Polls this early in the window do not count at all. A phone (Bluetooth, Android Auto)
# connects when the car is unlocked and the screen starts, often a few minutes before the
# driver turns the car on (setting up navigation, a call), and once it is connected no new
# event comes to start another window.
WAKE_OFF_MIN_AGE = 3 * 60.0
# A longer window set in the options moves that point on in step: a third of the way in
# (the default 10 min window keeps 3 min). Someone who sets 20 min to cover a long wait
# before driving off does not want it ended at 4 min.
WAKE_OFF_MIN_SHARE = WAKE_OFF_MIN_AGE / WAKE_WINDOW


class WakeWindow:
    """The "expecting a drive" window: the driving rate for a while after a wake hint.

    A false wake (a phone reconnecting to the parked car, a hint from another car) used to
    poll every minute for the whole window. Now WAKE_OFF_POLLS polls in a row that find the
    car off, from WAKE_OFF_MIN_AGE (or WAKE_OFF_MIN_SHARE of a longer window) into the
    window, end it early: about 4 min instead of 10 at the default driving rate. Not while
    something says the driver is still in the car (`held`: a phone still connected to it,
    or the hint that started the window still on): the phone connects at unlock, and a
    driver who sits for a while before turning the car on would otherwise have the drive
    seen only at the next parked poll, up to an hour later. Once the phone has gone, the
    next poll that finds the car off ends it. A real drive is still seen: a car turned on
    resets the count, and a new wake (the phone connecting again after a short stop)
    starts a new window. A window started by
    unplugging with a phone in the car runs its full length (`ends_early` False):
    unplugging, stowing the cable and getting in takes a few minutes, and no new phone
    event comes to start another one. A wake during such a window (Android Auto finishing
    its connection a minute after the unplug) keeps it running its full length.
    """

    def __init__(self) -> None:
        self.until = 0.0
        self.ends_early = True
        self.off_polls = 0
        self._started_at = 0.0
        self._counted_at = 0.0
        self._min_age = WAKE_OFF_MIN_AGE

    def start(self, now: float, length: float, *, ends_early: bool = True) -> None:
        if self.active(now):
            # Never cut short a window already running: one that runs its full length
            # stays so, and the later end is kept.
            ends_early = ends_early and self.ends_early
            self.until = max(self.until, now + length)
        else:
            self.until = now + length
        self.ends_early = ends_early
        self._min_age = max(WAKE_OFF_MIN_AGE, length * WAKE_OFF_MIN_SHARE)
        self.off_polls = 0
        self._started_at = now
        self._counted_at = now

    def end(self) -> None:
        self.until = 0.0
        self.off_polls = 0

    def active(self, now: float) -> bool:
        return now < self.until

    def car_seen(self, now: float, *, car_on: bool, held: bool = False) -> bool:
        """A poll during the window found the car on or off. True when the window should end.

        `held`: the driver still seems to be in the car (see the class docs). Polls that find
        the car off still count, so the first one after the phone has gone ends the window.
        """
        if not self.active(now):
            return False
        if car_on:
            self.off_polls = 0
            self._counted_at = now
            return False
        if now - self._started_at < self._min_age or now - self._counted_at < WAKE_OFF_SPACING:
            return False
        self.off_polls += 1
        self._counted_at = now
        return self.ends_early and not held and self.off_polls >= WAKE_OFF_POLLS


# Writes in a row that the BYD numbers did not take before retrying stops, until the target
# changes or the numbers come back. Stops a loop if BYD keeps a different value. The same
# limit applies to a target that held and was then set to something else from outside (an
# old interval automation, another tool): without it the two would write over each other
# for good.
MAX_WRITE_MISSES = 3


class IntervalWrites:
    """Whether the BYD poll interval numbers hold what Smart Poll wants.

    number.set_value skips an unavailable entity without an error (during a BYD reload, for
    example), so a write only counts once the numbers read back the target. Otherwise the
    car could stay on the driving rate all night after a reload mid-drive.
    """

    def __init__(self) -> None:
        # What the numbers are known to hold, written by us, or None if not known.
        self.applied: Intervals | None = None
        # The last target written, or None if nothing was written since start or a reset.
        self.target: Intervals | None = None
        self.misses = 0
        # Times the target held and something else then set the numbers to another value.
        self.overwrites = 0

    def reset(self) -> None:
        self.applied = self.target = None
        self.misses = self.overwrites = 0

    @property
    def gave_up(self) -> bool:
        """Whether something else keeps setting the numbers, so the target is not written again."""
        return self.overwrites >= MAX_WRITE_MISSES

    def _retry_left(self) -> bool:
        return self.misses < MAX_WRITE_MISSES and not self.gave_up

    def due(self, target: Intervals) -> bool:
        """Whether target needs writing."""
        if target == self.applied:
            return False
        return target != self.target or self._retry_left()

    def written(self, target: Intervals, current: Intervals | None) -> bool:
        """Record a write of target, with what the numbers read back after it.

        Returns True if they hold it now.
        """
        if target != self.target:
            self.target = target
            self.misses = self.overwrites = 0
        if current == target:
            self.applied = target
            self.misses = 0
            return True
        self.applied = None
        self.misses += 1
        return False

    def numbers_changed(self, current: Intervals | None, *, came_back: bool) -> bool:
        """The numbers changed outside a write. Returns True if the target should be written.

        current is None while either number is unavailable: nothing can be written then, so
        wait until they come back. came_back is True when a number was unavailable before,
        which gives the retries another go. A value the numbers report that is not ours (a
        BYD reload that kept an older value, or a manual edit) is written over. Each time a
        target that held is set to something else counts as an overwrite, and after
        MAX_WRITE_MISSES of them it is left alone (see gave_up) until the target changes or
        the numbers come back.
        """
        if current is None:
            self.applied = None
            return False
        if came_back:
            self.misses = self.overwrites = 0
        if current == self.target:
            self.applied = current
            self.misses = 0
            return False
        if self.target is not None and self.applied == self.target:
            self.overwrites += 1
        self.applied = None
        return self.target is not None and self._retry_left()
