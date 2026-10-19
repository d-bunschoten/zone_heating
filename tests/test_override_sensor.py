"""Tests for the per-zone override-ends timestamp sensor (spec §1.1, §3).

``sensor.<zone>_override_ends`` is a ``timestamp`` sensor carrying the end of the
respect-window / boost, so the HA UI can show a live countdown. It has a value
only while a respect-window/boost runs; in ``auto`` or a sticky ``away`` preset it
is ``unknown`` (no timer).
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

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
    DEFAULT_BOOST_DURATION,
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
CLIMATE = "climate.woonkamer"
OVERRIDE_ENDS = "sensor.woonkamer_override_ends"


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


async def _setup(hass: HomeAssistant, *, zone: str) -> MockConfigEntry:
    hass.states.async_set(HOME_SOURCE, "on")
    hass.states.async_set(ZONE_SOURCE, zone)
    async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
    async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE)
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
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _set_preset(hass: HomeAssistant, preset: str) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_PRESET_MODE: preset},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_sensor_exists_timestamp_and_unknown_in_auto(
    hass: HomeAssistant,
) -> None:
    """The sensor exists, is a timestamp, and is unknown in auto (no timer)."""
    await _setup(hass, zone="off")  # Standby, auto -> no respect-window

    state = hass.states.get(OVERRIDE_ENDS)
    assert state is not None
    assert state.attributes["device_class"] == "timestamp"
    assert state.state == "unknown"


async def test_sensor_has_timestamp_when_boost_active(
    hass: HomeAssistant,
) -> None:
    """A boost gives a timestamp ~= now + boost duration (spec §1.1)."""
    await _setup(hass, zone="on")  # Active base
    before = dt_util.utcnow()
    await _set_preset(hass, "boost")

    state = hass.states.get(OVERRIDE_ENDS)
    ends = dt_util.parse_datetime(state.state)
    assert ends is not None
    # End is ~ now + boost duration (60 min); allow a minute of slack.
    expected = before + timedelta(minutes=DEFAULT_BOOST_DURATION)
    assert abs((ends - expected).total_seconds()) < 60


async def test_sensor_unknown_for_sticky_away(hass: HomeAssistant) -> None:
    """The sticky away preset has no timer -> the sensor stays unknown (spec §3)."""
    await _setup(hass, zone="on")
    await _set_preset(hass, "away")

    assert hass.states.get(OVERRIDE_ENDS).state == "unknown"
