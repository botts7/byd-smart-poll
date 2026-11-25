"""Which configured signals are working, for Repairs and the polling mode sensor.

Pure logic, no HA imports. The HA side (issues.py) looks up each entity and passes in a
simple status; this decides which issues apply and which signals count as in use.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum

# Shorter outages are normal (a wallbox rebooting, a phone out of range overnight).
UNAVAILABLE_LIMIT = 6 * 3600

NO_CHARGER_POWER = "no_charger_power"
CHARGER_NOT_THIS_CAR = "charger_not_this_car"
CABLE_IMPLAUSIBLE = "cable_implausible"


class Role(StrEnum):
    """What a configured entity is used for. The value is shown in Repairs."""

    CHARGER_POWER = "charger power"
    CABLE = "cable connected"
    PHONE = "phone bluetooth"
    WAKE_HINT = "wake hint"


@dataclass(frozen=True)
class EntityStatus:
    found: bool = True
    disabled: bool = False
    # Seconds the entity has been unavailable, or None while it is available.
    unavailable_for: float | None = None


@dataclass(frozen=True)
class Configured:
    charger_power: str | None = None
    charger_connected: str | None = None
    # Only the phones actually used, that is none without a car Bluetooth name.
    phone_bluetooth: tuple[str, ...] = ()
    wake_hints: tuple[str, ...] = ()
    # The user said the car is not charged at home, so no charger sensor is expected.
    no_home_charger: bool = False

    def roles(self) -> dict[str, Role]:
        """Each configured entity once, with the first role it has."""
        roles: dict[str, Role] = {}
        for role, entity_ids in (
            (Role.CHARGER_POWER, (self.charger_power,)),
            (Role.CABLE, (self.charger_connected,)),
            (Role.PHONE, self.phone_bluetooth),
            (Role.WAKE_HINT, self.wake_hints),
        ):
            for entity_id in entity_ids:
                if entity_id:
                    roles.setdefault(entity_id, role)
        return roles


@dataclass(frozen=True)
class Issue:
    issue_id: str
    translation_key: str
    placeholders: tuple[tuple[str, str], ...] = ()


def _problem(status: EntityStatus, role: Role) -> str | None:
    """The issue translation key for an entity, or None if it works."""
    if not status.found:
        return "signal_not_found"
    if status.disabled:
        # The companion app disables the HA entity when the sensor is off on the phone.
        return "phone_sensor_disabled" if role is Role.PHONE else "signal_disabled"
    if status.unavailable_for is not None and status.unavailable_for >= UNAVAILABLE_LIMIT:
        return "signal_unavailable"
    return None


def issues_for(
    configured: Configured,
    statuses: Mapping[str, EntityStatus],
    *,
    charger_implausible: bool = False,
    cable_implausible: bool = False,
) -> set[Issue]:
    """The issues that apply. An entity with no status yet (not checked) counts as working.

    `charger_implausible` is ChargerCheck.implausible: the charger showed power while the car
    said it was not charging. `cable_implausible`: the cable sensor said connected while the
    car was on or moving.
    """
    issues: set[Issue] = set()
    if not configured.charger_power:
        if not configured.no_home_charger:
            issues.add(Issue(NO_CHARGER_POWER, NO_CHARGER_POWER))
    elif charger_implausible:
        issues.add(
            Issue(
                CHARGER_NOT_THIS_CAR,
                CHARGER_NOT_THIS_CAR,
                (("entity_id", configured.charger_power),),
            )
        )
    if cable_implausible and configured.charger_connected:
        issues.add(
            Issue(
                CABLE_IMPLAUSIBLE,
                CABLE_IMPLAUSIBLE,
                (("entity_id", configured.charger_connected),),
            )
        )
    for entity_id, role in configured.roles().items():
        problem = _problem(statuses.get(entity_id, EntityStatus()), role)
        if problem is not None:
            issues.add(
                Issue(
                    f"signal_missing_{entity_id}",
                    problem,
                    (("entity_id", entity_id), ("use", role.value)),
                )
            )
    return issues


def usable_state(state: str | None, *, numeric: bool = False) -> bool:
    """Whether an entity's state can be used now: known, and a number where one is needed."""
    if state is None or state in ("unavailable", "unknown"):
        return False
    if not numeric:
        return True
    try:
        return math.isfinite(float(state))
    except ValueError:
        return False


def signals_in_use(
    configured: Configured,
    statuses: Mapping[str, EntityStatus],
    *,
    odometer: bool,
    car_battery_power: bool,
    usable: Collection[str] | None = None,
) -> list[str]:
    """Short names of the signals that are configured and working, for the mode sensor.

    `usable` holds the configured entities whose state can be used right now (see
    usable_state), or None to go by the statuses alone. An entity that exists but is
    unavailable or not a number is not in use, even before it counts as a Repairs issue.
    `odometer` and `car_battery_power` say whether those BYD sensors have a number now.
    """

    def working(entity_id: str | None, role: Role) -> bool:
        if not entity_id or (usable is not None and entity_id not in usable):
            return False
        return _problem(statuses.get(entity_id, EntityStatus()), role) is None

    used: list[str] = []
    if working(configured.charger_power, Role.CHARGER_POWER):
        used.append("charger power")
    if working(configured.charger_connected, Role.CABLE):
        used.append("cable connected")
    phones = sum(working(e, Role.PHONE) for e in configured.phone_bluetooth)
    if phones:
        used.append(f"phone bluetooth ({phones})")
    hints = sum(working(e, Role.WAKE_HINT) for e in configured.wake_hints)
    if hints:
        used.append(f"wake hints ({hints})")
    if odometer:
        used.append("odometer check")
    if car_battery_power:
        used.append("car battery power")
    return used
