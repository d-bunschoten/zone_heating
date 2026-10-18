"""HA integration tests for the auto coordinator (spec §3, §6).

These mock ``tado_hijack.set_mode`` and simulate the source states, then assert
the backend calls + ``zone_state`` for Away / Standby / Active and that no
redundant call is made when nothing changes.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.zone_heating.backend_tado import (
    ATTR_HVAC_MODE,
    TADO_DOMAIN,
    TADO_SERVICE_SET_MODE,
)
from custom_components.zone_heating.const import (
    CONF_ACTIVE_SCHEDULE,
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

AWAY_TEMP = 16.0
STANDBY_TEMP = 18.0
ACTIVE_TEMP = 20.0


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


async def _setup(
    hass: HomeAssistant, *, home: str, zone: str
) -> tuple[MockConfigEntry, list[ServiceCall]]:
    """Set the sources, create the hub+zone and return (entry, mocked calls)."""
    hass.states.async_set(HOME_SOURCE, home)
    hass.states.async_set(ZONE_SOURCE, zone)

    calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)

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
    return entry, calls


def _last(calls: list[ServiceCall]) -> ServiceCall:
    return calls[-1]


async def test_away_when_house_not_present(hass: HomeAssistant) -> None:
    """House away -> Away at away_temp, regardless of zone presence (spec §3)."""
    _, calls = await _setup(hass, home="off", zone="on")

    assert len(calls) == 1
    assert _last(calls).data[ATTR_ENTITY_ID] == [TARGET]
    assert _last(calls).data[ATTR_HVAC_MODE] == "heat"
    assert _last(calls).data[ATTR_TEMPERATURE] == AWAY_TEMP

    state = hass.states.get("climate.woonkamer")
    assert state.attributes["zone_state"] == "Away"
    assert state.attributes["target_setpoint"] == AWAY_TEMP


async def test_standby_when_home_but_zone_empty(hass: HomeAssistant) -> None:
    """Home but nobody in the zone -> Standby at standby_temp (spec §3)."""
    _, calls = await _setup(hass, home="on", zone="off")

    assert len(calls) == 1
    assert _last(calls).data[ATTR_TEMPERATURE] == STANDBY_TEMP

    state = hass.states.get("climate.woonkamer")
    assert state.attributes["zone_state"] == "Standby"


async def test_active_when_present(hass: HomeAssistant) -> None:
    """Home and present (no active schedule) -> Active at active_temp (spec §3)."""
    _, calls = await _setup(hass, home="on", zone="on")

    assert len(calls) == 1
    assert _last(calls).data[ATTR_HVAC_MODE] == "heat"
    assert _last(calls).data[ATTR_TEMPERATURE] == ACTIVE_TEMP

    state = hass.states.get("climate.woonkamer")
    assert state.attributes["zone_state"] == "Active"
    assert state.attributes["present"] is True
    assert state.attributes["target_entities"] == [TARGET]


async def test_transitions_standby_to_active_to_away(
    hass: HomeAssistant,
) -> None:
    """Driving the sources walks the zone through the stands (spec §3, §6)."""
    _, calls = await _setup(hass, home="on", zone="off")
    assert hass.states.get("climate.woonkamer").attributes["zone_state"] == (
        "Standby"
    )
    assert calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP
    n_standby = len(calls)

    # Someone enters the zone -> Active.
    hass.states.async_set(ZONE_SOURCE, "on")
    await hass.async_block_till_done()
    assert hass.states.get("climate.woonkamer").attributes["zone_state"] == (
        "Active"
    )
    assert calls[-1].data[ATTR_TEMPERATURE] == ACTIVE_TEMP
    assert len(calls) > n_standby

    # Everyone leaves the house -> Away (wins over zone presence).
    hass.states.async_set(HOME_SOURCE, "off")
    await hass.async_block_till_done()
    assert hass.states.get("climate.woonkamer").attributes["zone_state"] == (
        "Away"
    )
    assert calls[-1].data[ATTR_TEMPERATURE] == AWAY_TEMP


async def test_no_redundant_call_when_nothing_changes(
    hass: HomeAssistant,
) -> None:
    """A source change that does not change the setpoint emits no new call."""
    _, calls = await _setup(hass, home="on", zone="off")
    assert len(calls) == 1  # initial Standby write

    # Toggle an unrelated-to-setpoint change: zone source flaps off->off via a
    # redundant state write. The debounced present stays off, decision unchanged.
    hass.states.async_set(ZONE_SOURCE, "off", {"changed": 1})
    await hass.async_block_till_done()
    assert len(calls) == 1

    # Now force a real change (present) then revert to Standby: going back to the
    # same Standby setpoint must not re-issue a call beyond the Active one.
    hass.states.async_set(ZONE_SOURCE, "on")
    await hass.async_block_till_done()
    assert len(calls) == 2  # Active write
    assert calls[-1].data[ATTR_TEMPERATURE] == ACTIVE_TEMP

    hass.states.async_set(ZONE_SOURCE, "off")
    await hass.async_block_till_done()
    assert len(calls) == 3  # back to Standby setpoint (different from Active)
    assert calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP

    # Re-assert Standby again -> no extra call (setpoint unchanged).
    hass.states.async_set(ZONE_SOURCE, "off", {"noise": 2})
    await hass.async_block_till_done()
    assert len(calls) == 3


ACTIVE_SCHEDULE = "schedule.living_active"


async def _setup_with_active_schedule(
    hass: HomeAssistant, *, schedule_state: str, block_temp: float | None
) -> tuple[MockConfigEntry, list[ServiceCall]]:
    """Home + present, with an active_schedule gate in the given state."""
    hass.states.async_set(HOME_SOURCE, "on")
    hass.states.async_set(ZONE_SOURCE, "on")
    attrs = {"temperature": block_temp} if block_temp is not None else {}
    hass.states.async_set(ACTIVE_SCHEDULE, schedule_state, attrs)

    calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)

    subentry = ConfigSubentryData(
        data={
            CONF_NAME: "Woonkamer",
            CONF_TARGETS: [TARGET],
            CONF_PRESENCE_ENTITY: ZONE_SOURCE,
            CONF_ON_DELAY: 0,
            CONF_OFF_DELAY: 0,
            CONF_ACTIVE_SCHEDULE: ACTIVE_SCHEDULE,
        },
        subentry_type=SUBENTRY_TYPE_ZONE,
        title="Woonkamer",
        unique_id=None,
    )
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
        subentries_data=[subentry],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, calls


async def test_active_schedule_on_block_uses_block_temp(
    hass: HomeAssistant,
) -> None:
    """Present + active schedule on-block -> Active at the block temp (spec §3)."""
    _, calls = await _setup_with_active_schedule(
        hass, schedule_state="on", block_temp=21.5
    )
    assert calls[-1].data[ATTR_TEMPERATURE] == 21.5
    assert hass.states.get("climate.woonkamer").attributes["zone_state"] == (
        "Active"
    )


async def test_active_schedule_gap_falls_to_standby(
    hass: HomeAssistant,
) -> None:
    """Present but active schedule gap -> gate closed -> Standby (spec §1.1)."""
    _, calls = await _setup_with_active_schedule(
        hass, schedule_state="off", block_temp=None
    )
    assert calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP
    assert hass.states.get("climate.woonkamer").attributes["zone_state"] == (
        "Standby"
    )


async def test_master_mode_off_is_hands_off(hass: HomeAssistant) -> None:
    """When master mode != auto the coordinator does not drive (F4 guard)."""
    entry, calls = await _setup(hass, home="on", zone="on")
    assert len(calls) == 1  # Active write under auto

    # Switch master mode away from auto; subsequent source changes do nothing.
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.heating_mode", "option": "off"},
        blocking=True,
    )
    await hass.async_block_till_done()
    before = len(calls)

    hass.states.async_set(HOME_SOURCE, "off")
    await hass.async_block_till_done()
    assert len(calls) == before  # no new calls while not in auto
