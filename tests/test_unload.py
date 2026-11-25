"""Unloading the entry: Repairs issues go when it is disabled, and stay over a reload."""

import asyncio
import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

integration = importlib.import_module("bsp.__init__")


def _unload(monkeypatch, disabled_by):
    deleted: list[str] = []
    stopped: list[bool] = []
    monkeypatch.setattr(integration, "async_delete_entry_issues", lambda _h, e: deleted.append(e))

    async def stop(*, restore: bool) -> None:
        stopped.append(restore)

    async def unload_platforms(_entry, _platforms) -> bool:
        return True

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_unload_platforms=unload_platforms))
    entry = SimpleNamespace(
        entry_id="entry", disabled_by=disabled_by, runtime_data=SimpleNamespace(async_stop=stop)
    )
    assert asyncio.run(integration.async_unload_entry(hass, entry))
    return deleted, stopped


def test_disabling_the_entry_restores_and_clears_its_repairs_issues(monkeypatch):
    deleted, stopped = _unload(monkeypatch, "user")
    assert stopped == [True]
    assert deleted == ["entry"]


def test_a_reload_keeps_the_intervals_and_the_issues(monkeypatch):
    deleted, stopped = _unload(monkeypatch, None)
    assert stopped == [False]
    assert deleted == []
