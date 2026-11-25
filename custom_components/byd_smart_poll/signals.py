"""Signals worked out from other entities, and the parked drain meter. Pure logic, no HA imports.

Time is in seconds (any monotonic or epoch base, as long as it is consistent).
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)
_DEVICE = re.compile(r"^\s*((?:[0-9A-F]{2}:){5}[0-9A-F]{2})\s*(?:\((.*)\))?\s*$", re.IGNORECASE)


def phone_connected_to_car(
    connected_devices: Iterable[object] | None, car: str, *, partial: bool = False
) -> bool:
    """Whether a phone's connected Bluetooth devices include the car.

    The HA companion app lists devices as "AA:BB:CC:DD:EE:FF (Name)". `car` is either the
    car's MAC or its whole Bluetooth name, case-insensitively. Not part of a name: "BYD Seal"
    must not match a phone connected to a "BYD SEALION 7". `partial` lets a part of the name
    match, only for a name saved before whole names were needed and not upgraded yet.
    """
    car = car.strip()
    if not car or not connected_devices:
        return False
    by_mac = is_mac(car)
    wanted = car.casefold()
    for device in connected_devices:
        text = str(device)
        if by_mac:
            if text.upper().startswith(car.upper()):
                return True
            continue
        match = _DEVICE.match(text)
        # An entry without a MAC is taken as a bare name.
        name = (match.group(2) or "") if match else text
        name = name.strip().casefold()
        if name == wanted or (partial and wanted in name):
            return True
    return False


@dataclass(frozen=True)
class PairedDevice:
    mac: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.mac})" if self.name else self.mac


def paired_devices(
    device_lists: Iterable[Iterable[object] | None], *, every_name: bool = False
) -> list[PairedDevice]:
    """Bluetooth devices paired with any of the phones, once each by MAC, sorted by name.

    Each list is a companion app "paired_devices" attribute. Two drivers paired with the same
    car list it twice, sometimes under different names, so the first name seen is kept, or
    with `every_name` each name the phones use for it.
    """
    found: dict[tuple[str, str], PairedDevice] = {}
    for devices in device_lists:
        for device in devices or ():
            match = _DEVICE.match(str(device))
            if match is None:
                continue
            mac = match.group(1).upper()
            name = (match.group(2) or "").strip()
            found.setdefault((mac, name.casefold() if every_name else ""), PairedDevice(mac, name))
    return sorted(found.values(), key=lambda d: (d.name.casefold(), d.mac))


def likely_car(devices: Sequence[PairedDevice], car_name: str | None) -> PairedDevice | None:
    """The paired device that is most likely this car.

    The car's own name is tried first, so with two BYDs paired the right one is picked, then
    anything with BYD in its name.
    """
    for needle in (car_name, "BYD"):
        needle = (needle or "").strip().casefold()
        if not needle:
            continue
        for device in devices:
            if needle in device.name.casefold():
                return device
    return None


def is_mac(text: str) -> bool:
    return bool(_MAC.match(text.strip()))


def device_for_typed_name(car: str, devices: Sequence[PairedDevice]) -> PairedDevice | None:
    """The paired device a typed car Bluetooth name means, or None if it is unclear.

    One with exactly that name, else the only one whose name contains it. Before whole names
    were needed, a part of the name ("BYD SEALION") matched, and entries may still hold one.
    """
    wanted = car.strip().casefold()
    if not wanted:
        return None
    for device in devices:
        if device.name.casefold() == wanted:
            return device
    partial = [device for device in devices if wanted in device.name.casefold()]
    # One device listed under two names is still one device.
    return partial[0] if len({device.mac for device in partial}) == 1 else None


# Refreshes in a row that brought back the car's previous reading before its data counts as
# stale. One alone can be a slow reply; two means the car or the connection is not answering.
STALE_REFRESHES = 2


def data_stale(stale_refreshes: int) -> bool:
    """Whether the car's data is stale: Smart Poll's polls refresh but nothing new comes."""
    return stale_refreshes >= STALE_REFRESHES


def poll_brought_nothing(
    before: str | None,
    before_reported: float | None,
    after: str | None,
    after_reported: float | None,
) -> bool:
    """Whether one of Smart Poll's own polls brought back the car's previous reading.

    `before` and `after` are the car's timestamp state either side of the poll, and the
    `_reported` values when that state was last written (Home Assistant's last_reported,
    epoch seconds). Only a poll whose refresh really wrote the timestamp again counts: a
    poll the BYD integration put off (its refresh is debounced) wrote nothing yet, and says
    nothing about the car. Rewrites from anything else (an HVAC reconcile, an update from
    another integration, a car that only pushes its data) are not looked at at all, so
    they cannot make a car that is answering look stale.
    """
    if not known_state(before) or not known_state(after) or after != before:
        return False
    return (
        before_reported is not None
        and after_reported is not None
        and (after_reported > before_reported)
    )


# A charge seen only in the car's data is no longer believed once its newest reading is this
# old and a charge poll since brought nothing newer. A charging car uploads far more often.
CHARGE_DATA_MAX_AGE = 30 * 60


def car_charge_data_stale(reading_age: float, stale_charge_polls: int) -> bool:
    """Whether the car's last reading is too old to say it is still charging.

    `reading_age` is the seconds since the newest reading arrived, on Home Assistant's clock
    (the car's own clock is not in step with it). `stale_charge_polls` counts our own charge
    polls that brought nothing new since. Not the BYD refreshes in general: the BYD
    integration also rewrites its entities without a new reading (HVAC reconcile, an
    update_entity from elsewhere), and some cars only push, so a real public charge would be
    dropped after one or two of those.
    """
    return stale_charge_polls >= 1 and reading_age >= CHARGE_DATA_MAX_AGE


def kept_stale_charge(
    stored: object, reading: str | None, now_epoch: float
) -> tuple[float, int] | None:
    """The car reading's age and stale charge polls kept from before a restart, or None.

    `stored` is what was saved when a charge poll brought nothing new: the car's timestamp
    state, when the reading arrived (epoch seconds, Home Assistant's clock) and the count.
    Only for the same reading (`reading`, the timestamp state now): a newer one starts
    afresh. Without this, every restart counted the old reading's age from the restart, so
    a charge flag left on in the car's data (away, after an unplugged public charge) gave
    another CHARGE_DATA_MAX_AGE of charging and one more poll each time.
    """
    if not isinstance(stored, dict) or reading is None or stored.get("reading") != reading:
        return None
    try:
        fresh = float(stored["fresh"])
        polls = int(stored["polls"])
    except (KeyError, TypeError, ValueError):
        return None
    return max(0.0, now_epoch - fresh), polls


def charge_poll_due(
    energy_due: bool, *, charging: bool, power_kw: float, reading_age: float
) -> bool:
    """Whether a charge poll should go out now.

    Usually when the energy added says the next percent has ticked over (`energy_due`). A
    charge seen only in the car's data with no power (the flag on, battery power 0 or not
    there) adds no energy, so after the session's first reading no poll was due again and
    car_charge_data_stale could never count one: the car stayed charging for good, on the
    30 min safety rate. So while the signal still says charging (`charging`, not a solar
    dip riding out the stop delay) with no power, a poll is also due once the newest
    reading is CHARGE_DATA_MAX_AGE old (`reading_age`, seconds).
    """
    return energy_due or (charging and power_kw <= 0 and reading_age >= CHARGE_DATA_MAX_AGE)


# How long a trusted charger without a reading keeps the charging state as it was. Chargers on
# MQTT go unavailable for a moment on every Home Assistant restart. One down for longer (its
# gateway or broker) no longer holds it: the car's own data decides, as without a charger.
CHARGER_UNKNOWN_HOLD = 10 * 60


def charger_unknown_holds(unknown_for: float | None) -> bool:
    """Whether a charger with no reading still holds the charging state (see charging_signal).

    `unknown_for` is how long it has been unavailable or unknown, or None if the entity is
    gone (removed or renamed), which counts as no charger at all.
    """
    return unknown_for is not None and unknown_for < CHARGER_UNKNOWN_HOLD


def charger_unknown_hold_left(unknown_for: float | None) -> float | None:
    """Seconds until a charger with no reading stops holding the charging state, or None.

    Nothing else may change while the charger is down, so the end of the hold has to be
    checked on its own (as vehicle_on_keep_left).
    """
    if unknown_for is None or not charger_unknown_holds(unknown_for):
        return None
    return CHARGER_UNKNOWN_HOLD - unknown_for


def car_says_charging(
    charging: bool | None, battery_power_w: float | None, vehicle_on: bool, min_power_w: float
) -> bool | None:
    """Whether the car's own data says it is charging, or None if it does not say.

    Only while the car is off: driving, regenerative braking shows as charging power (seen
    2026-11-03, +16 kW mid-drive while the charger sensor was unknown), and while the car is
    on it is in driving mode anyway.
    """
    if vehicle_on:
        return False
    if charging is None and battery_power_w is None:
        return None
    return charging is True or (battery_power_w is not None and battery_power_w >= min_power_w)


# A battery power at least this big, with the car's charging flag on, shows the sign the car
# gives charging. Well over what heating or cooling the cabin or battery draws while plugged in.
SIGN_LEARN_MIN_W = 1000.0


def charging_shows_negative(
    previous: bool | None,
    charging: bool | None,
    battery_power_w: float | None,
    vehicle_on: bool,
    *,
    charger_idle: bool = False,
) -> bool | None:
    """Whether this car reports charging as negative battery power, learned from its readings.

    `previous` is what was learned so far (None: not yet). Only a reading with the car off,
    its charging flag on and a battery power of SIGN_LEARN_MIN_W or more either way counts;
    the latest such reading wins, so one odd reading does not stick for good.
    `charger_idle`: the cable is in (or not known to be out, at home) and the charger (trusted
    or not) shows less than the charging threshold, so nothing is going in whatever the flag
    says (a solar pause). A big battery power then is the car drawing from its battery
    (battery or cabin heating), not charging.
    """
    if (
        vehicle_on
        or charger_idle
        or charging is not True
        or battery_power_w is None
        or abs(battery_power_w) < SIGN_LEARN_MIN_W
    ):
        return previous
    return battery_power_w < 0


def charge_power_w(
    charger_w: float | None,
    car_w: float | None,
    *,
    cable_connected: bool | None,
    car_says: bool | None,
    min_power_w: float,
    charging_negative: bool | None = None,
) -> float:
    """Power going into the battery, in W, for timing charge polls.

    `charger_w` is the trusted charger's power (None without one, unavailable or not
    trusted), `car_w` the car's own battery power from its last reading, `car_says`
    car_says_charging. The charger is used, except during a public charge: an idle home
    charger with the cable not in says nothing about a car the car itself says is charging,
    so its battery power is used. During a solar dip at home (cable in, or the car not
    saying charging) the charger is still right. Never negative; 0 without either.

    Some models report charging as negative battery power (the Sealion 7 as positive):
    `charging_negative` is the sign learned from the car's readings (charging_shows_negative).
    While the car says it is charging, power the learned way round counts, so a car that
    says charging while drawing from its battery (heating the cabin at a slow charger) adds
    nothing. Before the sign is known, the size of the power counts, whatever the sign. Only
    while the car says charging: a parked car drawing from its battery is negative too, and
    car_says_charging is never True while the car is on, so regenerative braking does not
    count either.
    """
    if charger_w is not None and (
        charger_w >= min_power_w or cable_connected is True or car_says is not True
    ):
        return max(0.0, charger_w)
    if car_w is None:
        return 0.0
    if car_says is not True or charging_negative is False:
        return max(0.0, car_w)
    if charging_negative is True:
        return max(0.0, -car_w)
    return abs(car_w)


def charging_signal(
    charger_power_w: float | None,
    min_power_w: float,
    *,
    cable_connected: bool | None,
    car_charging: bool | None,
    car_reading_since_idle: bool,
    home: bool | None = None,
    charger_unknown: bool = False,
    car_data_stale: bool = False,
) -> bool | None:
    """Whether the car is charging, from the charger and the car's own data.

    `charger_power_w` is None without a charger, while it is unavailable, or while it is not
    trusted (see ChargerCheck). `charger_unknown` is True when a trusted charger is configured
    but has had no reading for a short while (charger_unknown_holds): then at home, or with
    the cable in, the charger is still the one to go by once it is back, so None (unknown)
    keeps what was known. Chargers on MQTT go unavailable on every Home Assistant restart,
    and the car's last reading (from a charge that has since paused or ended) cost two polls
    each time. `car_charging` is car_says_charging. A charger at or over the
    threshold means charging. Below it, the car may still be charging somewhere else (public
    AC or DC), so the car's own data counts, but only while the car is not known to be at home
    (`home`) with the home cable not in, and only from a reading taken after the charger went
    idle: the car's last reading from a home charge still says charging after it is unplugged.
    At home, an idle charger is final: the car can say charging while the charger pauses.

    The car's data only counts while it is not stale (`car_data_stale`): a car that went to
    sleep with its last upload saying charging (unplugged at a destination charger, say)
    would otherwise stay charging for good, polled every 30 min.
    """
    if charger_power_w is not None and charger_power_w >= min_power_w:
        return True
    if charger_power_w is None:
        if charger_unknown and (cable_connected is True or home is True):
            return None
        car = car_charging
    elif cable_connected is True or home is True or not car_reading_since_idle:
        return False
    else:
        car = car_charging is True
    return False if car and car_data_stale else car


# Fresh car readings that must say "not charging" before the charger is doubted.
CHARGER_CHECK_READINGS = 2
# Only readings after this many percent's worth of charger energy without a rise count: the
# first readings of a real charge often still show the car from before it was plugged in, and
# the first tick can need up to one percent plus charging losses and an untuned kWh per percent.
CHARGER_CHECK_PCT = 2.0
# At or above this SoC the battery level cannot rise, and balancing at full or the cabin being
# heated or cooled on the cable draws power, so readings there only say "not charging this car
# right now" (ChargerCheck.full), never that the charger is the wrong one.
CHARGER_CHECK_MAX_SOC = 99.0
# A charger idle this long has finished whatever it was doing, so the count starts again.
# Shorter dips (a house meter or a plug with a cycling load) keep it.
CHARGER_IDLE_RESET = 15 * 60


class ChargerCheck:
    """Whether the charger power sensor is really charging this car.

    A shared wallbox, a smart plug with other loads, or a whole-house meter shows power while
    this car is not charging, which would poll the car every few minutes for nothing. Fed the
    charger's power and the car's fresh readings while the charger says charging: once the
    charger has delivered CHARGER_CHECK_PCT percent's worth of energy since the car's SoC last
    rose, CHARGER_CHECK_READINGS readings where the car says it is not charging and its SoC
    has not risen mark it implausible.

    An implausible charger is not trusted (also across restarts, for Repairs) until a reading
    shows it charging this car: its power is over the threshold while the car says charging
    or its SoC rises. Until then the car's own data is used, as without a charger.

    With the car full (CHARGER_CHECK_MAX_SOC) the same readings set `full` instead: the charger
    is not charging this car right now, which is no verdict on the charger. It is not trusted
    until it has been idle for CHARGER_IDLE_RESET, the car says charging, or a reading finds
    the battery below full again.

    With the cable sensor (believed, see CableCheck) saying the car is plugged in, the same
    readings set `drawing` instead: the car is drawing from this charger without charging (at
    its charge limit, with the cabin or battery being heated or cooled on the cable), so the
    charger is this car's. Like `full` it is no verdict: not stored, no Repairs issue, and it
    ends the same way (idle for CHARGER_IDLE_RESET or the car says charging), or when a
    reading finds the SoC risen.
    """

    def __init__(self, implausible: bool = False) -> None:
        self.implausible = implausible
        self.full = False
        self.drawing = False
        self._started = False
        # The car's SoC when the charger started or the SoC last rose, and the charger's
        # energy (kWh) since then.
        self._soc: float | None = None
        self._energy = 0.0
        self._power_w = 0.0
        self._ts: float | None = None
        self._idle_since: float | None = None
        self._misses = 0

    @property
    def trusted(self) -> bool:
        return not self.implausible and not self.full and not self.drawing

    @property
    def energy_kwh(self) -> float:
        """The charger's energy since it started or the car's SoC last rose, for the log."""
        return self._energy

    def _advance(self, ts: float) -> None:
        if self._ts is not None and ts > self._ts:
            self._energy += self._power_w * (ts - self._ts) / 3_600_000
        if self._ts is None or ts > self._ts:
            self._ts = ts

    def _restart(self, soc: float | None) -> None:
        self._started = True
        self._soc = soc
        self._energy = 0.0
        self._misses = 0
        self.full = self.drawing = False

    def charger_power(
        self, ts: float, power_w: float | None, min_power_w: float, soc: float | None
    ) -> None:
        """The charger's power changed. `soc` is the car's last known SoC."""
        if power_w is None:
            return
        self._advance(ts)
        self._power_w = max(0.0, power_w)
        if power_w < min_power_w:
            if self._idle_since is None:
                self._idle_since = ts
            return
        idle_for = ts - self._idle_since if self._idle_since is not None else None
        self._idle_since = None
        if not self._started or (idle_for is not None and idle_for >= CHARGER_IDLE_RESET):
            # Charging started (or was first seen): SoC must rise from here.
            self._restart(soc)

    def car_soc(self, soc: float | None) -> None:
        """Any fresh car reading, also while not charging (reading only runs while charging).

        One below full starts the count again after `full`, and a risen SoC after `drawing`:
        the car charged since. Otherwise a charge that starts straight after heating or
        cooling on the cable, with no idle stretch between, is only seen once a reading says
        charging (the next parked poll, up to the parked rate).
        """
        if soc is None:
            return
        if self.full and soc < CHARGER_CHECK_MAX_SOC:
            self._restart(soc)
        elif self.drawing and self._soc is not None and soc > self._soc:
            self._restart(soc)

    def reading(
        self,
        ts: float,
        *,
        soc: float | None,
        car_charging: bool | None,
        car_power_w: float | None,
        min_power_w: float,
        kwh_per_pct: float,
        away: bool = False,
        cable_in: bool = False,
    ) -> None:
        """A fresh car reading arrived while the charger shows charging power.

        `away` is True when the car is known to be away from home: then a charging car says
        nothing about the charger either way. `cable_in` is True when a believed cable sensor
        says the car is plugged into this charger.
        """
        if not self._started:
            self._restart(soc)
        self._advance(ts)
        if self._soc is None:
            # SoC was unknown when the charger started: it must rise from here.
            self._soc = soc
            self._energy = 0.0
        rose = soc is not None and self._soc is not None and soc > self._soc
        car_says = car_charging is True or (car_power_w is not None and car_power_w >= min_power_w)
        if rose or car_says:
            if not away:
                self._restart(soc)
                self.implausible = False
            return
        # Only a car that says outright it is not charging counts against the charger.
        if not self.trusted or car_charging is not False:
            return
        if self._energy < CHARGER_CHECK_PCT * kwh_per_pct:
            return
        self._misses += 1
        if self._misses >= CHARGER_CHECK_READINGS:
            if soc is not None and soc >= CHARGER_CHECK_MAX_SOC:
                self.full = True
            elif cable_in:
                self.drawing = True
            else:
                self.implausible = True


class ChargerSign:
    """Whether the charger power sensor shows power going into the car as negative.

    Some meters do (a clamp or energy monitor on the charger circuit that counts import as
    negative). Taking every reading as its absolute value would be wrong for a sensor that
    goes negative for something else (solar export on a house meter, a car feeding the house),
    so the sign is only turned round once it is clear: the sensor at or below minus the
    charging threshold while a fresh car reading says it is charging, with no reading at or
    above the threshold ever seen. One reading at or above the threshold ends it again. Both
    are kept across restarts (stored as the charger's entity id, see implausible_for): Home
    Assistant restarts often, and a house meter exporting solar while the car charges on
    surplus just after one would otherwise be turned round.
    """

    def __init__(self, negative: bool = False, seen_positive: bool = False) -> None:
        self.negative = negative
        self.seen_positive = seen_positive

    def charger_power(self, power_w: float | None, min_power_w: float) -> bool:
        """Feed a raw charger reading. Returns True if the sign was turned back."""
        if power_w is None or power_w < min_power_w:
            return False
        self.seen_positive = True
        changed, self.negative = self.negative, False
        return changed

    def car_reading(
        self, power_w: float | None, *, car_charging: bool | None, min_power_w: float
    ) -> bool:
        """A fresh car reading, with the charger's raw power now. True if it was turned round."""
        if self.negative or self.seen_positive or car_charging is not True:
            return False
        if power_w is None or power_w > -min_power_w:
            return False
        self.negative = True
        return True

    def apply(self, power_w: float | None) -> float | None:
        """The charger's power with power into the car positive."""
        if power_w is None or not self.negative:
            return power_w
        return -power_w


def implausible_for(stored: object, charger: str | None) -> bool:
    """Whether the stored "charger not this car" verdict applies to the charger configured now.

    It is stored as the charger's entity id, so picking another sensor (as the Repairs issue
    asks) or removing the charger starts afresh.
    """
    return bool(charger) and stored == charger


def known_state(state: str | None) -> bool:
    return state not in (None, "unavailable", "unknown")


def car_data_changed(old: str | None, new: str | None) -> bool:
    """Whether a car entity really changed, not just went to or came back from unavailable.

    A BYD cloud error makes the entities unavailable, then the same last reading comes back,
    which is not new data from the car.
    """
    return known_state(old) and known_state(new) and old != new


OLDER_READING_WINDOW = 15 * 60


class NewestReading:
    """The newest car reading seen, so an older one that arrives after it can be ignored.

    BYD's cloud sometimes answers a poll with a cached reading older than the one the car
    just sent: on 2026-11-05 a reading from 22 s earlier turned "car on" off for 0.3 s as a
    drive started. Car times are epoch seconds. A reading more than OLDER_READING_WINDOW
    older is taken as new, so a car clock that jumps cannot leave this stuck.
    """

    def __init__(self) -> None:
        self.time: float | None = None
        self.vehicle_on: bool | None = None

    def _older(self, car_time: float | None) -> bool:
        return (
            car_time is not None
            and self.time is not None
            and self.time - OLDER_READING_WINDOW <= car_time < self.time
        )

    def is_newer(self, car_time: float | None) -> bool:
        """Whether a reading is newer than the newest one seen (not the same one again)."""
        return car_time is not None and car_time != self.time and not self._older(car_time)

    def note(self, car_time: float, vehicle_on: bool) -> bool:
        """Record a reading. False if it is older than the newest one, so ignore it."""
        if self._older(car_time):
            return False
        self.time = car_time
        self.vehicle_on = vehicle_on
        return True

    def vehicle_on_now(self, reported: bool, car_time: float | None) -> bool:
        """Whether the car is on, going by the newest reading rather than an older one."""
        if self._older(car_time) and self.vehicle_on is not None:
            return self.vehicle_on
        return reported


# How long an unavailable or unknown "car on" sensor keeps a drive going. Longer and it is
# taken as off, so a sensor that stays broken cannot hold the driving rate for good.
VEHICLE_ON_UNKNOWN_KEEP = 30 * 60


def vehicle_on_or_keep(reported: bool | None, *, driving: bool, unknown_for: float | None) -> bool:
    """Whether the car is on, with an unavailable or unknown sensor kept as it was for a while.

    The BYD sensor goes unavailable while its integration reloads. Read as off, that would
    end a drive and drop to the parked rate mid-drive, so unknown keeps the current mode.
    unknown_for is how long it has been unknown, or None if the sensor is gone (disabled or
    removed), which counts as off.
    """
    if reported is not None:
        return reported
    return driving and unknown_for is not None and unknown_for < VEHICLE_ON_UNKNOWN_KEEP


def vehicle_on_keep_left(unknown_for: float | None) -> float | None:
    """Seconds until an unknown "car on" sensor stops keeping a drive, or None if it does not.

    Nothing else may change while the BYD integration is down, so the end of the keep has to
    be checked on its own.
    """
    if unknown_for is None or unknown_for >= VEHICLE_ON_UNKNOWN_KEEP:
        return None
    return VEHICLE_ON_UNKNOWN_KEEP - unknown_for


# The car on this long while the cable sensor says connected means the sensor is wrong. Not
# straight away: the car can be switched on while plugged in, to sit in it with the air on.
CAR_ON_WHILE_PLUGGED = 15 * 60
# The car moving while the cable sensor says connected means the sensor is wrong once it still
# says so this long after the first moving reading. Some chargers report the unplug minutes
# late (cloud chargers that poll, OCPP, gateways), so a normal drive-off can look like this.
MOVED_WHILE_PLUGGED = 10 * 60
# Readings that arrive this soon after the cable connected are not used: they can still be
# from the drive home (late cloud replies), with an odometer from before parking.
CONNECT_SETTLE = 5 * 60
# A single odometer step only counts as the start of a move if the next one follows within
# this long. One left over from the drive home, hours before a drive-off, must not count, or
# a charger that reports the unplug a minute late would be flagged.
RISE_STILL_MOVING = 30 * 60


class CableCheck:
    """Whether the charger's "cable connected" sensor can be believed for this car.

    A sensor stuck on, or one that belongs to a charger another car also uses (or means the
    cable is in the charger, not the car), would block every wake hint and keep the car "at
    home" for good. If the car moves, or stays on, while it keeps saying connected, it is
    ignored until it next turns off. Times are epoch seconds on Home Assistant's clock, when
    the cable changed or the reading arrived: the car's own clock is not in step with it.
    """

    def __init__(self, implausible: bool = False) -> None:
        # Restored across restarts: kept until the sensor next turns off.
        self.implausible = implausible
        self._connected: bool | None = None
        self._connected_at: float | None = None
        self._odometer: float | None = None
        self._on_since: float | None = None
        # When the odometer first went up, and when that was confirmed as a move.
        self._rose_at: float | None = None
        self._moved_at: float | None = None

    def cable(self, connected: bool | None, now: float) -> None:
        """Feed the cable sensor. Unknown keeps what was known."""
        if connected is None or connected == self._connected:
            return
        self._connected = connected
        self._connected_at = now if connected else None
        self._odometer = self._on_since = self._rose_at = self._moved_at = None
        if not connected:
            self.implausible = False

    def reading(
        self, *, vehicle_on: bool, odometer: float | None, time: float, charging: bool = False
    ) -> bool:
        """Feed a new car reading. Returns True when the cable sensor has just been found wrong.

        Only readings that arrive a while after the cable connected count: the odometer can be
        out of date from before (a drive home nobody saw), and that is no reason to doubt the
        cable. One step of the odometer is not taken as a move on its own (it can be the last
        bit of the drive home); it has to go up again, or go up with the car on. The car being
        on while it charges does not count either: power going in shows the cable is in it.
        """
        if self._connected is not True or self.implausible:
            return False
        if self._connected_at is not None and time < self._connected_at + CONNECT_SETTLE:
            return False
        if odometer is not None:
            if self._odometer is None:
                self._odometer = odometer
            elif odometer > self._odometer:
                self._odometer = odometer
                if self._moved_at is None:
                    if vehicle_on or self._rose_at is not None:
                        recent = (
                            self._rose_at is not None and time - self._rose_at < RISE_STILL_MOVING
                        )
                        self._moved_at = self._rose_at if recent else time
                    else:
                        self._rose_at = time
        if vehicle_on and not charging:
            if self._on_since is None:
                self._on_since = time
        else:
            self._on_since = None
        # A late unplug report turns the cable off before either of these runs out.
        moved = self._moved_at is not None and time - self._moved_at >= MOVED_WHILE_PLUGGED
        long_on = self._on_since is not None and time - self._on_since >= CAR_ON_WHILE_PLUGGED
        if moved or long_on:
            self.implausible = True
        return self.implausible

    def trusted(self, connected: bool | None) -> bool | None:
        """The cable sensor's state, or None (unknown) while it is not believed."""
        return None if self.implausible else connected


# Wake hints in these domains count when they turn on.
TOGGLE_DOMAINS = ("binary_sensor", "input_boolean", "switch")
# These hold the time they were last pressed, so a new time is a press.
PRESS_DOMAINS = ("input_button", "button")
# Any other wake hint (a sensor, an input_select) counts when it changes to one of these (any
# case). Not "home": a tracker arriving home comes just after a drive, and one that is home
# at every unplug would start a full window each time (see wake_hint_on).
WAKE_STATES = ("on", "connected", "true")


def _wake_state(state: str) -> bool:
    return state.casefold() in WAKE_STATES


def is_wake_hint(domain: str, old: str | None, new: str | None) -> bool:
    """Whether a wake hint's state change means the car is about to be used.

    A toggle or binary sensor (the iPhone CarPlay helper, Android Auto) counts when it turns
    on, not off. A button or input_button counts when pressed. Anything else counts only when
    it changes to a state that means connected or on: a sensor holding a count of Bluetooth
    devices, or a tracker updating its place, used to wake the car on every change.
    Going to or coming back from unavailable is not a wake: every entity does that on a Home
    Assistant restart. Nor is unknown, except a button never pressed before.
    """
    if new is None or not known_state(new) or old is None or old == "unavailable":
        return False
    if domain in PRESS_DOMAINS:
        return new != old
    if not known_state(old):
        return False
    if domain in TOGGLE_DOMAINS:
        return new == "on" and old != "on"
    return _wake_state(new) and not _wake_state(old)


def wake_hint_on(domain: str, state: str | None) -> bool:
    """Whether a wake hint is in its "about to be used" state now (buttons never are)."""
    if domain in PRESS_DOMAINS or not known_state(state) or state is None:
        return False
    return state == "on" if domain in TOGGLE_DOMAINS else _wake_state(state)


def wake_allowed(cable_connected: bool | None) -> bool:
    """Whether a phone or wake hint may start "expecting a drive".

    The car cannot be driven while the cable is in, and phones reconnect to a parked car
    (seen 2026-10-24: 7 min of fast polling after plugging in). Unplugging checks the hints
    again, so a real drive is still seen.
    """
    return cable_connected is not True


def at_home(
    *,
    cable_connected: bool | None,
    car_zone_home: bool | None,
    moved_since_fix: bool,
    previous: bool | None,
) -> bool | None:
    """Whether the car is parked at home, without waking it.

    The charger cable is the best signal: connected means home. Disconnected only means the
    cable is out, so the car's last GPS fix decides, unless the odometer has moved since that
    fix (a drive nobody reported), in which case the fix is out of date. When where the car
    is cannot be told (no GPS fix, for example after a failed GPS fetch, and the cable is out
    or unknown), the previous answer is kept.
    """
    if cable_connected:
        return True
    if moved_since_fix:
        return False
    if car_zone_home is not None:
        return car_zone_home
    return previous


class OdometerWatch:
    """Notices a drive that nothing reported, from the odometer at parked readings."""

    def __init__(self) -> None:
        self._last: float | None = None
        self._drove = False

    def note_drive(self) -> None:
        """A drive was seen. The first parked reading after it moved, and that is expected."""
        self._drove = True

    def update(self, odometer: float | None, *, parked: bool) -> bool:
        """Feed a reading. Returns True if the car moved since the last one with no drive seen."""
        if odometer is None:
            return False
        missed = parked and not self._drove and self._last is not None and odometer > self._last
        self._last = odometer
        if parked:
            self._drove = False
        return missed


@dataclass(frozen=True)
class Sample:
    ts: float
    soc: float
    power_w: float | None
    parked: bool


# A parked poll that finds the car drawing more than this from its battery counts as awake.
AWAKE_POWER_W = -50.0
AWAKE_WINDOW = 24 * 3600
# An awake reading with no awake one after it stands for at most this long (see awake_share).
AWAKE_HOLD = 15 * 60.0
DRAIN_WINDOW = 72 * 3600
# Drain is only reported once this much parked time has been seen, because SoC is whole percent.
DRAIN_MIN_HOURS = 12.0


class DrainMeter:
    """Car readings, for the parked awake share, parked drain and poll count."""

    def __init__(self, samples: Iterable[Sample] = ()) -> None:
        self.samples: deque[Sample] = deque(samples)

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)
        while self.samples and sample.ts - self.samples[0].ts > DRAIN_WINDOW:
            self.samples.popleft()

    def polls(self, now: float, window: float = 24 * 3600) -> int:
        return sum(1 for s in self.samples if now - s.ts <= window)

    def awake_share(self, now: float, max_gap: float) -> float | None:
        """Share of parked time in the last 24 h that readings found the car drawing power.

        Each parked reading stands for the time until the next reading of any kind, at most
        `max_gap` (the parked poll interval, also for the last one, which runs until now).
        Counting readings instead let the minute polls of a waking window and the polls just
        after parking, which find the car still awake from the drive, weigh as much as an
        hour parked each: a day of short stops showed about 30 % for a car awake 1 to 2 % of
        the time. The cap keeps a long gap (Home Assistant down) from standing for one reading.

        An awake reading followed by one that is not (or by none yet) stands for at most
        AWAKE_HOLD: the car goes to sleep a few minutes after a drive, so the last just-parked
        poll finding it awake must not count as awake for the whole parked interval (a 4 h
        interval made each drive add 4 h awake). Two awake readings in a row keep the full gap.
        """
        start = now - AWAKE_WINDOW
        samples = list(self.samples)
        total = awake = 0.0
        counted = 0
        for i, s in enumerate(samples):
            if not s.parked or s.power_w is None:
                continue
            nxt = samples[i + 1] if i + 1 < len(samples) else None
            is_awake = s.power_w < AWAKE_POWER_W
            hold = max_gap
            if is_awake and not (
                nxt is not None
                and nxt.parked
                and nxt.power_w is not None
                and nxt.power_w < AWAKE_POWER_W
            ):
                hold = min(max_gap, AWAKE_HOLD)
            end = nxt.ts if nxt is not None else now
            span = min(end, s.ts + hold, now) - max(s.ts, start)
            if span <= 0:
                continue
            counted += 1
            total += span
            if is_awake:
                awake += span
        if counted < 3:
            return None
        return awake / total

    def parked_drain_per_day(self) -> float | None:
        """SoC lost per day over parked stretches in the last 72 h.

        A stretch is a run of parked readings; driving or charging ends it. Only the drop
        within each stretch counts, so charging in between does not hide anything.
        """
        hours = 0.0
        drop = 0.0
        run: list[Sample] = []
        for s in [*self.samples, None]:
            if s is not None and s.parked:
                run.append(s)
                continue
            if len(run) >= 2:
                hours += (run[-1].ts - run[0].ts) / 3600
                drop += max(0.0, run[0].soc - run[-1].soc)
            run = []
        if hours < DRAIN_MIN_HOURS:
            return None
        return drop / hours * 24
