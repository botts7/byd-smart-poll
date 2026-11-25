"""Putting the BYD poll intervals back when Smart Poll is removed.

The entry is gone by then, so nothing else can retry. number.set_value skips an unavailable
number without an error (the BYD integration reloading, its cloud failing), so only reading
the numbers back tells whether the restore took. If it did not, each number that did not take
is tried again when it comes back, until Home Assistant restarts or Smart Poll is set up
again for the car, and the log says what to set by hand.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .modes import Intervals

_LOGGER = logging.getLogger(__name__)

_MISSING = (STATE_UNAVAILABLE, STATE_UNKNOWN)
# hass.data key: the cancel of each car's retry listener, by VIN.
_RETRIES = f"{DOMAIN}_restore_retries"


def _value(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in _MISSING:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _targets(numbers: tuple[str, str], restore: Intervals) -> dict[str, int]:
    return dict(zip(numbers, (restore.telemetry, restore.gps), strict=True))


async def _async_write(hass: HomeAssistant, targets: dict[str, int]) -> dict[str, int]:
    """Write each number that does not hold its value yet. Returns those that still do not."""
    for entity_id, value in targets.items():
        if _value(hass, entity_id) == value:
            continue
        try:
            await hass.services.async_call(
                "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
            )
        except Exception:  # noqa: BLE001 - reported below, with what to set by hand
            _LOGGER.debug("Could not restore %s to %s", entity_id, value, exc_info=True)
    return {e: v for e, v in targets.items() if _value(hass, e) != v}


def cancel_restore_retry(hass: HomeAssistant, key: str) -> None:
    """Stop the retry an earlier removal of this car left running, if any.

    For Smart Poll set up again for the same car before a restart: the old retry would
    otherwise write the original intervals over the new entry's when the numbers come back.
    """
    cancel = hass.data.get(_RETRIES, {}).pop(key, None)
    if cancel is not None:
        cancel()


async def async_restore_intervals(
    hass: HomeAssistant, numbers: tuple[str, str], restore: Intervals, *, key: str
) -> bool:
    """Write the original intervals back. Returns True if the numbers hold them now.

    If not, a listener writes each number that did not take when that number next comes back
    from unavailable. Only then, and only that number, so a value set by hand meanwhile is
    left alone. A warning names the values. `key` (the car's VIN) lets a new entry for
    the same car stop the listener (see cancel_restore_retry).
    """
    telemetry, gps = numbers
    cancel_restore_retry(hass, key)
    _LOGGER.info("Restoring BYD intervals %s s / %s s", restore.telemetry, restore.gps)
    pending = await _async_write(hass, _targets(numbers, restore))
    if not pending:
        return True
    _LOGGER.warning(
        "Could not put the BYD poll intervals back (BYD Vehicle may be reloading or "
        "unavailable). Trying again when they come back, until Home Assistant restarts. If "
        "they are still not back by then, set %s to %s and %s to %s by hand",
        telemetry,
        restore.telemetry,
        gps,
        restore.gps,
    )
    unsub: Callable[[], None] | None = None

    def _cancel() -> None:
        nonlocal unsub
        if unsub is not None:
            unsub()
            unsub = None
        retries = hass.data.get(_RETRIES, {})
        if retries.get(key) is _cancel:
            del retries[key]

    async def _async_changed(event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        old = event.data["old_state"]
        if unsub is None or entity_id not in pending:
            return
        if old is not None and old.state not in _MISSING:
            return
        if _value(hass, entity_id) is None:
            return
        if await _async_write(hass, {entity_id: pending[entity_id]}):
            return
        del pending[entity_id]
        if not pending:
            _LOGGER.info(
                "BYD poll intervals restored to %s s / %s s", restore.telemetry, restore.gps
            )
            _cancel()

    unsub = async_track_state_change_event(hass, list(pending), _async_changed)
    hass.data.setdefault(_RETRIES, {})[key] = _cancel
    return False
