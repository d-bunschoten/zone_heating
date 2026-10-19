"""HA integration tests for F5 status + robustness (spec §3, §5, §8, §9).

These mock ``tado_hijack.set_mode`` / ``set_schedule`` and simulate the tado_hijack
``switch.<zone>_schedule`` state + underlying setpoints to assert:

- ``control_source`` is a live mirror of the schedule switch (off->ha_overlay,
  on->tado_schedule) and flips live;
- ``heating_in_fallback`` turns on for an unexpected fallback and for master=fallback;
- the window-open modifier turns the zone off (Window-open) and restores the prior
  mode after the close-delay;
- an overlay expiry (switch off->on + setpoint change) is not misdetected as Custom
  and control_source becomes tado_schedule;
- a running boost + hvac_mode survive a reload, and the first evaluation makes no
  redundant write (and degrades to auto deterministically when the timer is lost);
- the derived baseline is only (re)written when it actually changed.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall, State

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
    CONF_WINDOW_CLOSE_DELAY,
    CONF_WINDOW_OPEN_DELAY,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
    mock_restore_cache_with_extra_data,
)

HOME_SOURCE = "input_boolean.home_source"
ZONE_SOURCE = "input_boolean.zone_source"
WINDOW_SOURCE = "binary_sensor.raam"
TARGET = "climate.living"
# Derived from the first target (climate.living) by the coordinator.
SWITCH = "switch.living_schedule"
CLIMATE = "climate.woonkamer"
CONTROL_SOURCE = "sensor.woonkamer_control_source"
WINDOW_OPEN = "binary_sensor.woonkamer_window_open"
IN_FALLBACK = "binary_sensor.heating_in_fallback"
MODE = "select.heating_mode"

AWAY_TEMP = 16.0
STANDBY_TEMP = 18.0
ACTIVE_TEMP = 20.0
BOOST_TEMP = 23.0


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


async def _setup(
    hass: HomeAssistant,
    *,
    home: str = "on",
    zone: str = "off",
    switch: str | None = None,
    subentry_extra: dict | None = None,
) -> tuple[MockConfigEntry, list[ServiceCall], list[ServiceCall]]:
    """Create the hub + one zone; return (entry, mode_calls, schedule_calls)."""
    hass.states.async_set(HOME_SOURCE, home)
    hass.states.async_set(ZONE_SOURCE, zone)
    if switch is not None:
        hass.states.async_set(SWITCH, switch)

    mode_calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
    sched_calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE)

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
    return entry, mode_calls, sched_calls


async def _select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: MODE, "option": option},
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


# --- control_source live mirror (spec §5) -----------------------------------
async def test_control_source_mirrors_switch_live(hass: HomeAssistant) -> None:
    """ha_overlay while the switch is off; tado_schedule once it goes on."""
    await _setup(hass, home="on", zone="off", switch="off")

    assert hass.states.get(CONTROL_SOURCE).state == "ha_overlay"
    assert hass.states.get(CLIMATE).attributes["control_source"] == "ha_overlay"

    # The schedule switch flips on (zone now follows Tado) -> mirror flips live.
    hass.states.async_set(SWITCH, "on")
    await hass.async_block_till_done()
    assert hass.states.get(CONTROL_SOURCE).state == "tado_schedule"
    assert hass.states.get(CLIMATE).attributes["control_source"] == "tado_schedule"


async def test_control_source_unknown_without_switch(
    hass: HomeAssistant,
) -> None:
    """No resolvable switch state -> control_source degrades to unknown."""
    await _setup(hass, home="on", zone="off")  # SWITCH never set
    assert hass.states.get(CONTROL_SOURCE).state in ("unknown", "unavailable")


# --- heating_in_fallback (spec §9) ------------------------------------------
async def test_in_fallback_on_unexpected_schedule(hass: HomeAssistant) -> None:
    """In auto, a zone back on its Tado schedule flips in_fallback on."""
    await _setup(hass, home="on", zone="off", switch="off")
    assert hass.states.get(IN_FALLBACK).state == "off"

    hass.states.async_set(SWITCH, "on")  # overlay lapsed -> on Tado schedule
    await hass.async_block_till_done()
    assert hass.states.get(IN_FALLBACK).state == "on"


async def test_in_fallback_on_master_fallback(hass: HomeAssistant) -> None:
    """Master mode fallback sets in_fallback on (zones run the Tado baseline)."""
    await _setup(hass, home="on", zone="off", switch="off")
    assert hass.states.get(IN_FALLBACK).state == "off"

    await _select(hass, "fallback")
    assert hass.states.get(IN_FALLBACK).state == "on"


# --- window-open modifier (spec §3) -----------------------------------------
async def test_window_open_turns_off_and_restores_mode(
    hass: HomeAssistant, freezer
) -> None:
    """Open -> zone off (Window-open, hvac off); close -> prior heat stand returns."""
    _, mode_calls, _ = await _setup(
        hass,
        home="on",
        zone="on",
        subentry_extra={
            CONF_WINDOW_SENSOR: WINDOW_SOURCE,
            CONF_WINDOW_OPEN_DELAY: 0,
            CONF_WINDOW_CLOSE_DELAY: 5,
        },
    )
    hass.states.async_set(WINDOW_SOURCE, "off")
    await hass.async_block_till_done()

    # Take a manual stand so we can prove the *prior mode* is restored.
    await _set_preset(hass, "active")
    assert hass.states.get(CLIMATE).state == "heat"
    assert hass.states.get(CLIMATE).attributes["zone_state"] == "Active"

    # Window opens -> safety override regardless of hvac_mode (spec §3).
    hass.states.async_set(WINDOW_SOURCE, "on")
    await hass.async_block_till_done()
    state = hass.states.get(CLIMATE)
    assert state.state == "off"  # hvac_mode shown as off
    assert state.attributes["zone_state"] == "Window-open"
    assert state.attributes["window_open"] is True
    assert mode_calls[-1].data[ATTR_HVAC_MODE] == "off"

    # Window closes -> after the close-delay the zone returns to its heat stand.
    hass.states.async_set(WINDOW_SOURCE, "off")
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == "off"  # still off during close-delay

    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == "heat"  # prior mode restored
    assert state.attributes["zone_state"] == "Active"
    assert state.attributes["window_open"] is False
    assert mode_calls[-1].data[ATTR_TEMPERATURE] == ACTIVE_TEMP


# --- overlay expiry is an expected fallback, not Custom (spec §5 step 3) -----
async def test_overlay_expiry_not_custom(
    hass: HomeAssistant, freezer
) -> None:
    """switch off->on + setpoint change = expected schedule fallback, not Custom."""
    _, mode_calls, _ = await _setup(
        hass, home="on", zone="on", switch="off"
    )  # Active, commanded 20, HA overlay
    assert hass.states.get(CONTROL_SOURCE).state == "ha_overlay"

    # Past both debounce windows (own-command 10s, presence 30s).
    freezer.tick(timedelta(seconds=40))

    # The overlay expires: the switch flips to the Tado schedule and the device's
    # setpoint changes to the schedule temp.
    hass.states.async_set(SWITCH, "on")
    await hass.async_block_till_done()
    hass.states.async_set(TARGET, "heat", {ATTR_TEMPERATURE: STANDBY_TEMP})
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.attributes["zone_state"] != "Custom"
    assert state.state != "heat"  # not flipped into a manual stand
    assert state.attributes["control_source"] == "tado_schedule"
    assert hass.states.get(CONTROL_SOURCE).state == "tado_schedule"
    assert hass.states.get(IN_FALLBACK).state == "on"


# --- restart / restore (spec §9) --------------------------------------------
async def test_restart_restores_boost_without_redundant_write(
    hass: HomeAssistant,
) -> None:
    """A running boost + hvac_mode survive a restart; no redundant first write."""
    # The device already holds the boost setpoint from before the restart.
    hass.states.async_set(TARGET, "heat", {ATTR_TEMPERATURE: BOOST_TEMP})
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(CLIMATE, "heat"),
                {
                    "hvac_mode": "heat",
                    "preset": "boost",
                    "custom_setpoint": None,
                    "override_remaining_min": 42,
                },
            )
        ],
    )

    _, mode_calls, _ = await _setup(hass, home="on", zone="on")

    state = hass.states.get(CLIMATE)
    assert state.state == "heat"
    assert state.attributes["zone_state"] == "Boost"
    assert state.attributes["preset_mode"] == "boost"
    # Timer restored (~42 min) and NO redundant set_mode (device already at 23).
    assert state.attributes["override_remaining_min"] == 42
    assert len(mode_calls) == 0


async def test_restart_without_timer_degrades_to_auto(
    hass: HomeAssistant,
) -> None:
    """A restored heat stand whose timer is unknown degrades to auto (spec §9)."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(CLIMATE, "heat"),
                {
                    "hvac_mode": "heat",
                    "preset": "active",
                    "custom_setpoint": None,
                    "override_remaining_min": None,
                },
            )
        ],
    )

    await _setup(hass, home="on", zone="off")

    state = hass.states.get(CLIMATE)
    assert state.state == "auto"  # deterministic fall-back
    assert state.attributes["zone_state"] == "Standby"


# --- inert without zones (spec §9) ------------------------------------------
async def test_inert_without_zones(hass: HomeAssistant) -> None:
    """A hub with no zones writes nothing to Tado (inert, spec §9)."""
    mode_calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
    sched_calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Zone Heating",
        unique_id="zone_heating_hub",
        options={CONF_HOME_PRESENCE_ENTITY: HOME_SOURCE},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert len(mode_calls) == 0
    assert len(sched_calls) == 0


# --- baseline write dedupe (spec §8) ----------------------------------------
async def test_reload_unchanged_baseline_no_rewrite(
    hass: HomeAssistant,
) -> None:
    """A reload with an unchanged standby config writes no new baseline (spec §8)."""
    entry, _, sched_calls = await _setup(hass, home="on", zone="off")
    assert len(sched_calls) == 1  # first-setup derived baseline

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert len(sched_calls) == 1  # unchanged -> no new set_schedule


async def test_reload_changed_baseline_rewrites(hass: HomeAssistant) -> None:
    """A changed standby config does write a new baseline on reload (spec §8)."""
    entry, _, sched_calls = await _setup(hass, home="on", zone="off")
    assert len(sched_calls) == 1
    assert sched_calls[-1].data["blocks"][0]["temperature"] == STANDBY_TEMP

    # Change the standby base temperature (persisted RestoreNumber).
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: "number.woonkamer_standby_temp", "value": 19.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert len(sched_calls) == 2  # changed -> new set_schedule
    assert sched_calls[-1].data["blocks"][0]["temperature"] == 19.0
