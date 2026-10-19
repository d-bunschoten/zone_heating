"""HA integration tests for Tado-write resilience (spec §8, §9, F5.2).

A failing or missing Tado backend service (e.g. ``tado_hijack.set_mode`` /
``set_schedule`` not registered, or raising) must be **logged as a warning and
skipped** — never propagated. The smart layer keeps loading (entry ``loaded``,
not ``setup_error``); the existing Tado schedule/overlay stays the vangnet. A
failed baseline write does not persist its signature, so a later attempt retries.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.zone_heating.backend_tado import (
    TADO_DOMAIN,
    TADO_SERVICE_SET_MODE,
    TADO_SERVICE_SET_SCHEDULE,
)
from custom_components.zone_heating.const import (
    CONF_HOME_OFF_DELAY,
    CONF_HOME_ON_DELAY,
    CONF_HOME_PRESENCE_ENTITY,
    CONF_NAME,
    CONF_OFF_DELAY,
    CONF_ON_DELAY,
    CONF_PRESENCE_ENTITY,
    CONF_TARGETS,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

HOME_SOURCE = "input_boolean.home_source"
ZONE_SOURCE = "input_boolean.zone_source"
TARGET = "climate.living"


def _zone_subentry() -> ConfigSubentryData:
    return ConfigSubentryData(
        data={
            CONF_NAME: "Woonkamer",
            CONF_TARGETS: [TARGET],
            CONF_PRESENCE_ENTITY: ZONE_SOURCE,
            CONF_ON_DELAY: 0,
            CONF_OFF_DELAY: 0,
        },
        subentry_type=SUBENTRY_TYPE_ZONE,
        title="Woonkamer",
        unique_id=None,
    )


async def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set(HOME_SOURCE, "on")
    hass.states.async_set(ZONE_SOURCE, "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Zone Heating",
        unique_id="zone_heating_hub",
        options={
            CONF_HOME_PRESENCE_ENTITY: HOME_SOURCE,
            CONF_HOME_ON_DELAY: 0,
            CONF_HOME_OFF_DELAY: 0,
        },
        subentries_data=[_zone_subentry()],
    )
    entry.add_to_hass(hass)
    return entry


def _zone_sid(entry: MockConfigEntry) -> str:
    return next(
        s.subentry_id
        for s in entry.subentries.values()
        if s.subentry_type == SUBENTRY_TYPE_ZONE
    )


async def test_setup_loaded_when_tado_services_unregistered(
    hass: HomeAssistant, caplog
) -> None:
    """Setup still loads when tado_hijack.set_mode/set_schedule do not exist."""
    # No Tado services registered at all -> every backend call hits ServiceNotFound.
    entry = await _make_entry(hass)

    with caplog.at_level(logging.WARNING, logger="custom_components.zone_heating"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Entry is loaded, not setup_error.
    assert entry.state is ConfigEntryState.LOADED
    # A warning was logged instead of an exception propagating.
    assert "mislukt" in caplog.text
    # The smart layer is up: the zone's climate entity exists.
    assert hass.states.get("climate.woonkamer") is not None


async def test_setup_loaded_when_tado_services_raise(
    hass: HomeAssistant, caplog
) -> None:
    """A raising set_mode/set_schedule is logged + skipped; entry stays loaded."""

    async def _raise(call: ServiceCall) -> None:
        raise HomeAssistantError("tado backend down")

    hass.services.async_register(TADO_DOMAIN, TADO_SERVICE_SET_MODE, _raise)
    hass.services.async_register(TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE, _raise)

    entry = await _make_entry(hass)

    with caplog.at_level(logging.WARNING, logger="custom_components.zone_heating"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert "mislukt" in caplog.text

    # The failed baseline write did NOT persist a signature, so a later attempt
    # retries it instead of deduping it away (spec §8).
    coordinator = entry.runtime_data.coordinator
    assert coordinator._baseline_sigs == {}


async def test_retry_writes_baseline_after_backend_recovers(
    hass: HomeAssistant,
) -> None:
    """Once the backend works again a reload writes the baseline (retry, spec §8)."""
    failing = {"on": True}

    async def _maybe_raise(call: ServiceCall) -> None:
        if failing["on"]:
            raise HomeAssistantError("tado backend down")

    # Both services fail on the first setup.
    hass.services.async_register(TADO_DOMAIN, TADO_SERVICE_SET_MODE, _maybe_raise)
    hass.services.async_register(
        TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE, _maybe_raise
    )

    entry = await _make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    coordinator = entry.runtime_data.coordinator
    sid = _zone_sid(entry)
    assert sid not in coordinator._baseline_sigs  # nothing persisted yet

    # Backend recovers; re-register set_schedule as a capturing mock and retry
    # via a reload (unchanged baseline would normally dedupe, but nothing was
    # persisted, so the retry writes it).
    failing["on"] = False
    sched_calls = async_mock_service(
        hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE
    )

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    # The retry wrote the baseline and persisted its signature this time.
    assert len(sched_calls) >= 1
    assert sched_calls[-1].data["blocks"][0]["temperature"] == 18.0
    assert _zone_sid(entry) in entry.runtime_data.coordinator._baseline_sigs
