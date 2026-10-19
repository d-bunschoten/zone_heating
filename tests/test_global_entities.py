"""Tests for the global (hub) entities and their defaults (spec §1.2)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.zone_heating.const import (
    DEFAULT_WINDOW_CLOSE_DELAY,
    DEFAULT_WINDOW_OPEN_DELAY,
    DOMAIN,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry


def test_window_delay_defaults() -> None:
    """Spec §3: window open-delay defaults to 1 min, close-delay to 5 min."""
    assert DEFAULT_WINDOW_OPEN_DELAY == 1
    assert DEFAULT_WINDOW_CLOSE_DELAY == 5


async def test_home_present_defaults_on_without_source(
    hass: HomeAssistant,
) -> None:
    """Spec §1.2: with no house source, heating_home_present reads ON (never Away)."""
    await _setup_hub(hass)
    state = hass.states.get("binary_sensor.heating_home_present")
    assert state is not None
    assert state.state == "on"


async def _setup_hub(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, title="Zone Heating", unique_id="zone_heating_hub"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_global_entities_exist_with_defaults(hass: HomeAssistant) -> None:
    """All global entities exist with the spec defaults, even without zones."""
    await _setup_hub(hass)

    # select.heating_mode default auto.
    mode = hass.states.get("select.heating_mode")
    assert mode is not None
    assert mode.state == "auto"
    assert mode.attributes["options"] == ["auto", "fallback", "off"]

    # number defaults.
    assert float(hass.states.get("number.heating_boost_temp").state) == 23.0
    assert float(hass.states.get("number.heating_boost_duration").state) == 60.0
    assert (
        float(hass.states.get("number.heating_override_timeout").state) == 120.0
    )

    # Home-present defaults ON without a configured house source (spec §1.2): Away
    # then never occurs, so a sourceless zone runs Standby/Active, not Away.
    assert hass.states.get("binary_sensor.heating_home_present").state == "on"
    assert hass.states.get("binary_sensor.heating_in_fallback").state == "off"


async def test_inert_without_zones(hass: HomeAssistant) -> None:
    """Without zones, no per-zone entities are created (spec §9, inert)."""
    await _setup_hub(hass)

    # No climate entities and no per-zone control_source sensors exist.
    assert list(hass.states.async_entity_ids("climate")) == []
    zone_like = [
        eid
        for eid in hass.states.async_entity_ids()
        if "control_source" in eid
    ]
    assert zone_like == []
