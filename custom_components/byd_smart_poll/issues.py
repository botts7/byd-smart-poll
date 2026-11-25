"""Repairs issues for missing or broken signals. The decisions are in signal_health.py."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .signal_health import Configured, EntityStatus, issues_for

CHECK_INTERVAL = timedelta(minutes=30)

# Setup attempts that could not find the car before a Repairs issue says so. Home Assistant
# keeps retrying after that, so the issue goes once the car is back.
SETUP_TRIES_BEFORE_ISSUE = 3
# hass.data key: failed setup attempts in a row, by entry id.
_SETUP_FAILURES = f"{DOMAIN}_setup_failures"
# BydEntitiesError key to issue translation key.
_SETUP_ISSUES = {
    "device_not_found": "car_not_found",
    "not_byd_device": "car_not_found",
    "missing_entities": "car_entities_missing",
    "entities_disabled": "car_entities_disabled",
}


@callback
def _entry_issue_ids(hass: HomeAssistant, entry_id: str) -> set[str]:
    prefix = f"{entry_id}_"
    return {
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith(prefix)
    }


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Clear the entry's issues and its setup failure count (removed or disabled)."""
    # Left behind, a re-enabled entry would raise the setup issue on its first failure.
    hass.data.get(_SETUP_FAILURES, {}).pop(entry_id, None)
    for issue_id in _entry_issue_ids(hass, entry_id):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def async_setup_failed(hass: HomeAssistant, entry_id: str, car: str, error: str) -> None:
    """Count a setup attempt that could not use the car, and raise an issue after a few."""
    failures: dict[str, int] = hass.data.setdefault(_SETUP_FAILURES, {})
    failures[entry_id] = failures.get(entry_id, 0) + 1
    if failures[entry_id] < SETUP_TRIES_BEFORE_ISSUE:
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{entry_id}_setup",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=_SETUP_ISSUES.get(error, "car_not_found"),
        translation_placeholders={"car": car},
    )


@callback
def async_setup_succeeded(hass: HomeAssistant, entry_id: str) -> None:
    hass.data.get(_SETUP_FAILURES, {}).pop(entry_id, None)
    ir.async_delete_issue(hass, DOMAIN, f"{entry_id}_setup")


class IssueWatcher:
    """Keeps this entry's Repairs issues in line with the configured signals."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        car_name: str,
        configured: Configured,
        on_change: Callable[[], None],
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._car_name = car_name
        self.configured = configured
        self._on_change = on_change
        self.statuses: dict[str, EntityStatus] = {}
        self.charger_implausible = False
        self._running = False
        # Set by the controller when the cable sensor said connected while the car was on or
        # moving, until the sensor next turns off (see CableCheck).
        self.cable_implausible = False
        self._active: set[str] = set()
        # For registered entities that have no state at all yet.
        self._no_state_since: dict[str, float] = {}
        self._unsubs: list[Callable[[], None]] = []

    @callback
    def async_start(self) -> None:
        # Issues are kept across reloads, so one the user ignored stays ignored (deleting and
        # creating it again would bring it back). Pick up the ones from before, so any that no
        # longer apply are still removed on the first check.
        self._active = _entry_issue_ids(self.hass, self._entry_id)
        # Other integrations' entities may not exist until Home Assistant has started.
        self._unsubs.append(async_at_started(self.hass, self._started))

    @callback
    def _started(self, _hass: HomeAssistant) -> None:
        self._running = True
        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, CHECK_INTERVAL))
        watched = list(self.configured.roles())
        if watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watched, self._async_state_changed)
            )
        self.async_check()

    @callback
    def async_set_charger_implausible(self, implausible: bool) -> None:
        if implausible == self.charger_implausible:
            return
        self.charger_implausible = implausible
        # Before Home Assistant has started, other entities may be missing; the first check
        # then picks this up.
        if self._running:
            self.async_check()

    @callback
    def async_set_cable_implausible(self, implausible: bool) -> None:
        if implausible == self.cable_implausible:
            return
        self.cable_implausible = implausible
        if self._running:
            self.async_check()

    @callback
    def async_stop(self) -> None:
        self._running = False
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _async_tick(self, _now: datetime) -> None:
        self.async_check()

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        old = event.data["old_state"]
        new = event.data["new_state"]
        # Only appearing, disappearing or availability changes matter here.
        if (
            old is None
            or new is None
            or (old.state == STATE_UNAVAILABLE) != (new.state == STATE_UNAVAILABLE)
        ):
            self.async_check()

    @callback
    def async_check(self) -> None:
        now = dt_util.utcnow().timestamp()
        self.statuses = {e: self._status(e, now) for e in self.configured.roles()}
        wanted = {
            f"{self._entry_id}_{issue.issue_id}": issue
            for issue in issues_for(
                self.configured,
                self.statuses,
                charger_implausible=self.charger_implausible,
                cable_implausible=self.cable_implausible,
            )
        }
        for issue_id, issue in wanted.items():
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=issue.translation_key,
                translation_placeholders={"car": self._car_name, **dict(issue.placeholders)},
            )
        for issue_id in self._active - wanted.keys():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._active = set(wanted)
        self._on_change()

    def _status(self, entity_id: str, now: float) -> EntityStatus:
        registered = er.async_get(self.hass).async_get(entity_id)
        state = self.hass.states.get(entity_id)
        if registered is None and state is None:
            return EntityStatus(found=False)
        if registered is not None and registered.disabled_by is not None:
            return EntityStatus(disabled=True)
        if state is not None and state.state != STATE_UNAVAILABLE:
            self._no_state_since.pop(entity_id, None)
            return EntityStatus()
        if state is not None:
            since = state.last_changed.timestamp()
        else:
            since = self._no_state_since.setdefault(entity_id, now)
        return EntityStatus(unavailable_for=now - since)
