"""HA integration tests for F3 manual control (spec §3 heat/off, §4, §5).

These mock ``tado_hijack.set_mode`` and drive the ``climate.<zone>`` services to
assert: Custom (set_temperature), the forced presets (set_preset_mode), the boost
and respect-window timeouts expiring back to auto, sticky ``away`` surviving a
house-Away event, Off, and the shadow-state Custom detection on an external
underlying-setpoint change (vs. an HA-own change which must be ignored).
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall

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
    CONF_TARGETS,
    DEFAULT_BOOST_TEMP,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

HOME_SOURCE = "input_boolean.home_source"
ZONE_SOURCE = "input_boolean.zone_source"
TARGET = "climate.living"
CLIMATE = "climate.woonkamer"

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
    hass.states.async_set(HOME_SOURCE, home)
    hass.states.async_set(ZONE_SOURCE, zone)
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
        subentries_data=[_zone_subentry()],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, calls


async def _set_temperature(hass: HomeAssistant, temperature: float) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_TEMPERATURE: temperature},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _set_preset(hass: HomeAssistant, preset: str) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_PRESET_MODE: preset},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _set_hvac(hass: HomeAssistant, mode: str) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_HVAC_MODE: mode},
        blocking=True,
    )
    await hass.async_block_till_done()


def _advance(hass: HomeAssistant, freezer, minutes: float) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)


# --- set_temperature -> Custom ----------------------------------------------
async def test_set_temperature_enters_custom(hass: HomeAssistant) -> None:
    """Dialing a temp -> Custom: hvac heat, preset none, set_mode heat + T."""
    _, calls = await _setup(hass, home="on", zone="off")  # Standby 18 first
    await _set_temperature(hass, 19.5)

    assert calls[-1].data[ATTR_HVAC_MODE] == "heat"
    assert calls[-1].data[ATTR_TEMPERATURE] == 19.5

    state = hass.states.get(CLIMATE)
    assert state.state == "heat"
    assert state.attributes["zone_state"] == "Custom"
    assert state.attributes["preset_mode"] is None
    assert state.attributes["target_setpoint"] == 19.5
    assert state.attributes["override_active"] is True


# --- preset_mode uses HA-standard presets (F5.6, nice icons) ----------------
async def test_preset_modes_are_ha_standard(hass: HomeAssistant) -> None:
    """``climate.<zone>`` exposes HA-standard presets: away/eco/comfort/boost."""
    await _setup(hass, home="on", zone="off")
    state = hass.states.get(CLIMATE)
    assert state.attributes["preset_modes"] == ["away", "eco", "comfort", "boost"]


async def test_auto_standby_preset_is_eco(hass: HomeAssistant) -> None:
    """In auto, the Standby stand reports preset ``eco`` (zone_state stays Standby)."""
    await _setup(hass, home="on", zone="off")  # present but Active not allowed
    state = hass.states.get(CLIMATE)
    assert state.state == "auto"
    assert state.attributes["zone_state"] == "Standby"
    assert state.attributes["preset_mode"] == "eco"


async def test_auto_active_preset_is_comfort(hass: HomeAssistant) -> None:
    """In auto, the Active stand reports preset ``comfort`` (zone_state stays Active)."""
    await _setup(hass, home="on", zone="on")  # present + Active allowed
    state = hass.states.get(CLIMATE)
    assert state.state == "auto"
    assert state.attributes["zone_state"] == "Active"
    assert state.attributes["preset_mode"] == "comfort"


# --- set_preset_mode -> forced stand ----------------------------------------
async def test_preset_boost(hass: HomeAssistant) -> None:
    _, calls = await _setup(hass, home="on", zone="off")
    await _set_preset(hass, "boost")

    assert calls[-1].data[ATTR_HVAC_MODE] == "heat"
    assert calls[-1].data[ATTR_TEMPERATURE] == DEFAULT_BOOST_TEMP
    state = hass.states.get(CLIMATE)
    assert state.state == "heat"
    assert state.attributes["zone_state"] == "Boost"
    assert state.attributes["preset_mode"] == "boost"


async def test_preset_active(hass: HomeAssistant) -> None:
    _, calls = await _setup(hass, home="on", zone="off")  # Standby base
    # HA-standard preset ``comfort`` maps onto the Active stand (nice icons).
    await _set_preset(hass, "comfort")

    assert calls[-1].data[ATTR_TEMPERATURE] == ACTIVE_TEMP
    state = hass.states.get(CLIMATE)
    assert state.attributes["zone_state"] == "Active"
    assert state.attributes["preset_mode"] == "comfort"


async def test_preset_standby(hass: HomeAssistant) -> None:
    _, calls = await _setup(hass, home="on", zone="on")  # Active base
    # HA-standard preset ``eco`` maps onto the Standby stand (nice icons).
    await _set_preset(hass, "eco")

    assert calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP
    state = hass.states.get(CLIMATE)
    assert state.attributes["zone_state"] == "Standby"
    assert state.attributes["preset_mode"] == "eco"


async def test_preset_away(hass: HomeAssistant) -> None:
    _, calls = await _setup(hass, home="on", zone="on")
    await _set_preset(hass, "away")

    assert calls[-1].data[ATTR_TEMPERATURE] == AWAY_TEMP
    state = hass.states.get(CLIMATE)
    assert state.attributes["zone_state"] == "Away"
    assert state.attributes["preset_mode"] == "away"
    # Sticky -> no respect-window timer.
    assert state.attributes["override_remaining_min"] is None


# --- timers expire back to auto ---------------------------------------------
async def test_boost_expires_to_auto(hass: HomeAssistant, freezer) -> None:
    _, calls = await _setup(hass, home="on", zone="on")  # Active
    await _set_preset(hass, "boost")
    assert hass.states.get(CLIMATE).state == "heat"

    _advance(hass, freezer, 61)  # boost duration default 60 min
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == "auto"
    assert state.attributes["zone_state"] == "Active"


async def test_override_timeout_expires_to_auto(
    hass: HomeAssistant, freezer
) -> None:
    _, calls = await _setup(hass, home="on", zone="off")  # Standby
    await _set_preset(hass, "comfort")
    assert hass.states.get(CLIMATE).state == "heat"

    _advance(hass, freezer, 121)  # override timeout default 120 min
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == "auto"
    assert state.attributes["zone_state"] == "Standby"


# --- sticky away vs. non-sticky stands on house-Away ------------------------
async def test_away_preset_is_sticky_through_house_away(
    hass: HomeAssistant,
) -> None:
    _, calls = await _setup(hass, home="on", zone="on")
    await _set_preset(hass, "away")
    assert hass.states.get(CLIMATE).state == "heat"

    # The house goes Away: the away preset must persist (spec §3).
    hass.states.async_set(HOME_SOURCE, "off")
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == "heat"
    assert state.attributes["zone_state"] == "Away"
    assert state.attributes["preset_mode"] == "away"


async def test_non_sticky_stand_ends_on_house_away(
    hass: HomeAssistant,
) -> None:
    _, calls = await _setup(hass, home="on", zone="on")
    await _set_preset(hass, "comfort")
    assert hass.states.get(CLIMATE).state == "heat"

    hass.states.async_set(HOME_SOURCE, "off")
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == "auto"  # ended -> back to auto
    assert state.attributes["zone_state"] == "Away"  # auto now picks Away


# --- off is sticky ----------------------------------------------------------
async def test_off_turns_zone_off_and_is_sticky(hass: HomeAssistant) -> None:
    _, calls = await _setup(hass, home="on", zone="on")  # Active first
    await _set_hvac(hass, "off")

    assert calls[-1].data[ATTR_HVAC_MODE] == "off"
    assert ATTR_TEMPERATURE not in calls[-1].data
    state = hass.states.get(CLIMATE)
    assert state.state == "off"
    assert state.attributes["zone_state"] == "Off"

    # House-Away must not revive it.
    hass.states.async_set(HOME_SOURCE, "off")
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == "off"


# --- Custom detection (shadow-state, spec §5) -------------------------------
async def test_external_setpoint_change_detected_as_custom(
    hass: HomeAssistant, freezer
) -> None:
    """A physical-knob change on the target -> Custom (spec §5)."""
    _, calls = await _setup(hass, home="on", zone="on")  # Active, commanded 20
    # Past both debounce windows (own-command 10s, presence 30s).
    freezer.tick(timedelta(seconds=40))

    hass.states.async_set(TARGET, "heat", {ATTR_TEMPERATURE: 24.0})
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == "heat"
    assert state.attributes["zone_state"] == "Custom"
    assert state.attributes["preset_mode"] is None
    assert state.attributes["target_setpoint"] == 24.0


async def test_ha_own_change_not_detected_as_custom(
    hass: HomeAssistant,
) -> None:
    """The device echoing HA's own setpoint must not flip to Custom (spec §5)."""
    _, calls = await _setup(hass, home="on", zone="on")  # Active, commanded 20

    # Device echoes exactly what HA commanded, within the own-command window.
    hass.states.async_set(TARGET, "heat", {ATTR_TEMPERATURE: ACTIVE_TEMP})
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == "auto"
    assert state.attributes["zone_state"] == "Active"
