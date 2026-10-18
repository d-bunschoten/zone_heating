"""Tests for the global (hub) options flow (spec §1.2, §1.3, §7)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.zone_heating.const import (
    CONF_DEAD_MANS_SWITCH,
    CONF_HOME_OFF_DELAY,
    CONF_OVERLAY_TERMINATION,
    DOMAIN,
    OVERLAY_TIMER,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_options_flow_stores_global_config(hass: HomeAssistant) -> None:
    """The options flow stores the global structural configuration."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, title="Zone Heating", unique_id="zone_heating_hub"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HOME_OFF_DELAY: 15,
            CONF_DEAD_MANS_SWITCH: False,
            CONF_OVERLAY_TERMINATION: OVERLAY_TIMER,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_HOME_OFF_DELAY] == 15
    assert entry.options[CONF_DEAD_MANS_SWITCH] is False
    assert entry.options[CONF_OVERLAY_TERMINATION] == OVERLAY_TIMER
