"""Tests for linking configured schedule helpers to the zone device (spec §1.1).

A zone references existing HA ``schedule`` helpers via ``standby_schedule`` /
``active_schedule``. Those are UI helpers with ``device_id=None``, so they appear
nowhere near the zone. On setup the coordinator attaches **only the schedule
helpers** to the zone's device so they show up in its entity list, and detaches a
schedule helper that is still linked to the device but no longer configured.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

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
    CONF_STANDBY_SCHEDULE,
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


def _zone_subentry(extra: dict | None = None) -> ConfigSubentryData:
    data = {
        CONF_NAME: "Woonkamer",
        CONF_TARGETS: [TARGET],
        CONF_PRESENCE_ENTITY: ZONE_SOURCE,
        CONF_ON_DELAY: 0,
        CONF_OFF_DELAY: 0,
    }
    if extra:
        data.update(extra)
    return ConfigSubentryData(
        data=data,
        subentry_type=SUBENTRY_TYPE_ZONE,
        title="Woonkamer",
        unique_id=None,
    )


def _make_entry(extra: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Zone Heating",
        unique_id="zone_heating_hub",
        options={
            CONF_HOME_PRESENCE_ENTITY: HOME_SOURCE,
            CONF_HOME_ON_DELAY: 0,
            CONF_HOME_OFF_DELAY: 0,
        },
        subentries_data=[_zone_subentry(extra)],
    )


def _zone_sid(entry: MockConfigEntry) -> str:
    return next(
        s.subentry_id
        for s in entry.subentries.values()
        if s.subentry_type == SUBENTRY_TYPE_ZONE
    )


def _mock_tado(hass: HomeAssistant) -> None:
    hass.states.async_set(HOME_SOURCE, "on")
    hass.states.async_set(ZONE_SOURCE, "off")
    async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
    async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE)


async def test_configured_schedule_helper_gets_zone_device(
    hass: HomeAssistant,
) -> None:
    """A configured schedule helper is attached to the zone's device (spec §1.1)."""
    _mock_tado(hass)
    registry = er.async_get(hass)
    # A UI schedule helper starts with no device (device_id None).
    sched = registry.async_get_or_create(
        "schedule",
        "schedule",
        "standby_uid",
        suggested_object_id="woonkamer_standby",
    )
    assert sched.device_id is None

    entry = _make_entry({CONF_STANDBY_SCHEDULE: sched.entity_id})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The schedule helper now hangs under the zone device.
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, _zone_sid(entry)), entry.entry_id
    )
    assert device is not None
    assert registry.async_get(sched.entity_id).device_id == device.id


async def test_unconfigured_schedule_helper_is_detached(
    hass: HomeAssistant,
) -> None:
    """Removing a schedule helper from the config unlinks it from the device."""
    _mock_tado(hass)
    registry = er.async_get(hass)
    sched = registry.async_get_or_create(
        "schedule",
        "schedule",
        "standby_uid",
        suggested_object_id="woonkamer_standby",
    )

    # First set up WITH the schedule helper configured -> it is attached.
    entry = _make_entry({CONF_STANDBY_SCHEDULE: sched.entity_id})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, _zone_sid(entry)), entry.entry_id
    )
    assert registry.async_get(sched.entity_id).device_id == device.id

    # Now reconfigure the zone to drop the schedule helper, and reload.
    subentry = next(
        s
        for s in entry.subentries.values()
        if s.subentry_type == SUBENTRY_TYPE_ZONE
    )
    new_data = dict(subentry.data)
    new_data.pop(CONF_STANDBY_SCHEDULE)
    hass.config_entries.async_update_subentry(entry, subentry, data=new_data)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # The helper is detached from the device again (device_id back to None).
    assert registry.async_get(sched.entity_id).device_id is None
