"""Tests for the global (hub) entities and their defaults (spec §1.2)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.zone_heating.const import DOMAIN

from pytest_homeassistant_custom_component.common import MockConfigEntry


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

    # binary sensors exist and default to off (no control logic in F1).
    assert hass.states.get("binary_sensor.heating_home_present").state == "off"
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
