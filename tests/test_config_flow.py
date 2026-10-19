"""Tests for the Zone Heating config flow, subentries and entities."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.zone_heating.const import (
    CONF_NAME,
    CONF_OFF_DELAY,
    CONF_ON_DELAY,
    CONF_TARGETS,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_user_flow_creates_hub(hass: HomeAssistant) -> None:
    """The user flow creates a single hub config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].title == "Zone Heating"


async def test_single_instance_only(hass: HomeAssistant) -> None:
    """A second hub cannot be created."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, unique_id="zone_heating_hub"
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zone_subentry_flow_creates_device_and_entities(
    hass: HomeAssistant,
) -> None:
    """Adding a zone subentry creates one device with all per-zone entities."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, title="Zone Heating", unique_id="zone_heating_hub"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Start and complete the zone subentry flow.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Woonkamer",
            CONF_TARGETS: ["climate.living"],
            CONF_ON_DELAY: 1,
            CONF_OFF_DELAY: 10,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # The subentry is stored on the hub entry.
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    subentries = [
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_ZONE
    ]
    assert len(subentries) == 1
    subentry = subentries[0]
    assert subentry.data[CONF_NAME] == "Woonkamer"

    # Adding the subentry triggers the hub's update listener, which reloads the
    # entry so the new zone's device and entities are created automatically.
    await hass.async_block_till_done()

    # The always-present per-zone entities exist with the expected entity_ids.
    expected = [
        "climate.woonkamer",
        "number.woonkamer_away_temp",
        "number.woonkamer_standby_temp",
        "number.woonkamer_active_temp",
        "number.woonkamer_boost_temp",
        "sensor.woonkamer_control_source",
    ]
    for entity_id in expected:
        assert hass.states.get(entity_id) is not None, f"missing {entity_id}"

    # The conditional per-zone binary sensors are NOT created: this zone was
    # configured without a presence source or a window sensor (spec §1.1).
    assert hass.states.get("binary_sensor.woonkamer_present") is None
    assert hass.states.get("binary_sensor.woonkamer_window_open") is None

    # The per-zone number defaults (away < standby < active).
    assert float(hass.states.get("number.woonkamer_away_temp").state) == 16.0
    assert float(hass.states.get("number.woonkamer_standby_temp").state) == 18.0
    assert float(hass.states.get("number.woonkamer_active_temp").state) == 20.0
    # Boost temp is per-zone (default 23) and replaces the former global number.
    assert float(hass.states.get("number.woonkamer_boost_temp").state) == 23.0

    # The per-zone entities are all attached to exactly one zone device,
    # and that device is linked to the zone subentry.
    ent_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    device_ids = set()
    for entity_id in expected:
        reg_entry = ent_reg.async_get(entity_id)
        assert reg_entry is not None
        assert reg_entry.device_id is not None
        assert reg_entry.config_subentry_id == subentry.subentry_id
        device_ids.add(reg_entry.device_id)

    assert len(device_ids) == 1
    device = device_reg.async_get(device_ids.pop())
    assert device is not None
    assert device.name == "Woonkamer"
