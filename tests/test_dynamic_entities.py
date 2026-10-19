"""Tests for dynamic per-zone binary sensors (spec §1.1, F5.2).

``binary_sensor.<zone>_present`` is created only when the zone has a presence
source (entity or template); ``binary_sensor.<zone>_window_open`` only when a
window sensor is configured. A removed source has its orphaned registry entry
cleaned up. The state machine still works without these entities: the coordinator
reads ``present``/``window_open`` as ``False`` when the entity is absent.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er

from custom_components.zone_heating.backend_tado import (
    ATTR_HVAC_MODE,
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
    CONF_PRESENCE_TEMPLATE,
    CONF_TARGETS,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    KEY_PRESENT,
    KEY_WINDOW_OPEN,
    SUBENTRY_TYPE_ZONE,
)

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

HOME_SOURCE = "input_boolean.home_source"
ZONE_SOURCE = "input_boolean.zone_source"
WINDOW_SOURCE = "binary_sensor.raam"
TARGET = "climate.living"

PRESENT = "binary_sensor.woonkamer_present"
WINDOW_OPEN = "binary_sensor.woonkamer_window_open"
CLIMATE = "climate.woonkamer"

STANDBY_TEMP = 18.0


def _zone_subentry(extra: dict | None = None) -> ConfigSubentryData:
    data = {
        CONF_NAME: "Woonkamer",
        CONF_TARGETS: [TARGET],
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


async def _setup(
    hass: HomeAssistant, *, subentry_extra: dict | None = None
) -> tuple[MockConfigEntry, list[ServiceCall]]:
    hass.states.async_set(HOME_SOURCE, "on")
    hass.states.async_set(ZONE_SOURCE, "on")
    hass.states.async_set(WINDOW_SOURCE, "off")

    calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
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
        subentries_data=[_zone_subentry(subentry_extra)],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, calls


def _zone_sid(entry: MockConfigEntry) -> str:
    return next(
        s.subentry_id
        for s in entry.subentries.values()
        if s.subentry_type == SUBENTRY_TYPE_ZONE
    )


async def test_no_sources_creates_neither_sensor(hass: HomeAssistant) -> None:
    """A zone without presence/window source gets neither binary sensor (spec §1.1)."""
    await _setup(hass)

    assert hass.states.get(PRESENT) is None
    assert hass.states.get(WINDOW_OPEN) is None
    # The always-present entities are unaffected.
    assert hass.states.get(CLIMATE) is not None
    assert hass.states.get("sensor.woonkamer_control_source") is not None


async def test_state_machine_works_without_sensors(
    hass: HomeAssistant,
) -> None:
    """No presence/window entity -> present/window read False -> Standby (spec §1.1)."""
    _, calls = await _setup(hass)

    # Home present but no zone presence entity -> present=False -> Standby.
    state = hass.states.get(CLIMATE)
    assert state.attributes["zone_state"] == "Standby"
    assert state.attributes["window_open"] is False
    assert calls[-1].data[ATTR_HVAC_MODE] == "heat"
    assert calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP


async def test_presence_entity_creates_present_sensor(
    hass: HomeAssistant,
) -> None:
    """A presence entity source -> the _present sensor is created (spec §1.1)."""
    await _setup(hass, subentry_extra={CONF_PRESENCE_ENTITY: ZONE_SOURCE})
    assert hass.states.get(PRESENT) is not None
    assert hass.states.get(WINDOW_OPEN) is None


async def test_presence_template_creates_present_sensor(
    hass: HomeAssistant,
) -> None:
    """A presence *template* source also creates the _present sensor (spec §1.1)."""
    await _setup(
        hass,
        subentry_extra={
            CONF_PRESENCE_TEMPLATE: "{{ is_state('" + ZONE_SOURCE + "', 'on') }}"
        },
    )
    assert hass.states.get(PRESENT) is not None


async def test_window_sensor_creates_window_sensor(
    hass: HomeAssistant,
) -> None:
    """A window sensor source -> the _window_open sensor is created (spec §1.1)."""
    await _setup(hass, subentry_extra={CONF_WINDOW_SENSOR: WINDOW_SOURCE})
    assert hass.states.get(WINDOW_OPEN) is not None
    assert hass.states.get(PRESENT) is None


async def test_orphan_present_sensor_removed_when_source_absent(
    hass: HomeAssistant,
) -> None:
    """A leftover _present registry entry is cleaned up when the source is gone."""
    hass.states.async_set(HOME_SOURCE, "on")
    async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
    async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE)

    # A zone configured WITHOUT a presence source...
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Zone Heating",
        unique_id="zone_heating_hub",
        options={CONF_HOME_PRESENCE_ENTITY: HOME_SOURCE},
        subentries_data=[_zone_subentry()],
    )
    entry.add_to_hass(hass)
    sid = _zone_sid(entry)

    # ...but a stale _present entity lingers in the registry from an earlier config.
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{sid}_{KEY_PRESENT}",
        config_entry=entry,
        config_subentry_id=sid,
        suggested_object_id="woonkamer_present",
    )
    assert (
        registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{sid}_{KEY_PRESENT}"
        )
        is not None
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Setup removed the now-orphaned registry entry (no unavailable leftover).
    assert (
        registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{sid}_{KEY_PRESENT}"
        )
        is None
    )
    assert (
        registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{sid}_{KEY_WINDOW_OPEN}"
        )
        is None
    )
