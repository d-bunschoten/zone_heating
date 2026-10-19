"""HA integration tests for F4 master modes + dead-man's-switch (spec §2, §7, §8).

These mock both ``tado_hijack.set_mode`` and ``tado_hijack.set_schedule`` and drive
``select.heating_mode`` to assert:

- entering Off writes the off-baseline (flat frost temp) + sets the zones off;
- leaving Off -> auto writes the derived normal baseline (flat standby temp here);
- auto <-> fallback writes NO schedule;
- fallback stops asserting set_mode overlays;
- set_mode carries the configured overlay termination (next_block / timer+duration);
- the heartbeat re-asserts the active overlay within the termination window, and is
  absent when the dead-man's-switch is off.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.zone_heating.backend_tado import (
    ATTR_ALL_DAYS,
    ATTR_BLOCKS,
    ATTR_DAYS,
    ATTR_DURATION,
    ATTR_HVAC_MODE,
    ATTR_OVERLAY,
    ATTR_TIMETABLE,
    TADO_DOMAIN,
    TADO_OVERLAY_NEXT_BLOCK,
    TADO_SERVICE_SET_MODE,
    TADO_SERVICE_SET_SCHEDULE,
)
from custom_components.zone_heating.const import (
    CONF_DEAD_MANS_SWITCH,
    CONF_HOME_OFF_DELAY,
    CONF_HOME_ON_DELAY,
    CONF_HOME_PRESENCE_ENTITY,
    CONF_NAME,
    CONF_OFF_BASELINE_TEMP,
    CONF_OFF_DELAY,
    CONF_ON_DELAY,
    CONF_OVERLAY_TERMINATION,
    CONF_OVERLAY_TIMER_DURATION,
    CONF_PRESENCE_ENTITY,
    CONF_TARGETS,
    DOMAIN,
    OVERLAY_TIMER,
    SUBENTRY_TYPE_ZONE,
)
from custom_components.zone_heating.engine.baseline import TIMETABLE_ONE_DAY

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

HOME_SOURCE = "input_boolean.home_source"
ZONE_SOURCE = "input_boolean.zone_source"
TARGET = "climate.living"
CLIMATE = "climate.woonkamer"
MODE = "select.heating_mode"

STANDBY_TEMP = 18.0
OFF_TEMP = 7.0


def _zone_subentry() -> ConfigSubentryData:
    return ConfigSubentryData(
        data={
            CONF_NAME: "Woonkamer",
            CONF_TARGETS: [TARGET],
            CONF_PRESENCE_ENTITY: ZONE_SOURCE,
            CONF_ON_DELAY: 0,
            CONF_OFF_DELAY: 0,
            CONF_OFF_BASELINE_TEMP: OFF_TEMP,
        },
        subentry_type=SUBENTRY_TYPE_ZONE,
        title="Woonkamer",
        unique_id=None,
    )


async def _setup(
    hass: HomeAssistant, *, options: dict | None = None
) -> tuple[list[ServiceCall], list[ServiceCall]]:
    """Home present, zone empty -> Standby. Returns (mode_calls, schedule_calls)."""
    hass.states.async_set(HOME_SOURCE, "on")
    hass.states.async_set(ZONE_SOURCE, "off")

    mode_calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
    sched_calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE)

    base_options = {
        CONF_HOME_PRESENCE_ENTITY: HOME_SOURCE,
        CONF_HOME_ON_DELAY: 0,
        CONF_HOME_OFF_DELAY: 0,
    }
    if options:
        base_options.update(options)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Zone Heating",
        unique_id="zone_heating_hub",
        options=base_options,
        subentries_data=[_zone_subentry()],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return mode_calls, sched_calls


async def _select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: MODE, "option": option},
        blocking=True,
    )
    await hass.async_block_till_done()


# --- first setup: derived normal baseline (spec §8.1) ------------------------
async def test_first_setup_writes_normal_baseline(hass: HomeAssistant) -> None:
    """First setup in auto writes the derived normal baseline (flat standby)."""
    mode_calls, sched_calls = await _setup(hass)

    assert len(sched_calls) == 1
    call = sched_calls[0]
    assert call.data[ATTR_ENTITY_ID] == [TARGET]
    assert call.data[ATTR_TIMETABLE] == TIMETABLE_ONE_DAY
    assert call.data[ATTR_ALL_DAYS] is True
    # one_day plan -> no per-weekday ``days`` field (new backend signature, spec §8).
    assert ATTR_DAYS not in call.data
    assert call.data[ATTR_BLOCKS] == [
        {"start": "00:00", "end": "24:00", "temperature": STANDBY_TEMP}
    ]
    # And the state machine asserted the Standby overlay.
    assert mode_calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP


# --- set_mode carries the configured overlay termination (spec §7) -----------
async def test_set_mode_carries_next_block_termination(
    hass: HomeAssistant,
) -> None:
    mode_calls, _ = await _setup(hass)
    assert mode_calls[-1].data[ATTR_OVERLAY] == TADO_OVERLAY_NEXT_BLOCK
    assert ATTR_DURATION not in mode_calls[-1].data


async def test_set_mode_carries_timer_termination(hass: HomeAssistant) -> None:
    mode_calls, _ = await _setup(
        hass,
        options={
            CONF_OVERLAY_TERMINATION: OVERLAY_TIMER,
            CONF_OVERLAY_TIMER_DURATION: 90,
        },
    )
    assert mode_calls[-1].data[ATTR_DURATION] == 90
    assert ATTR_OVERLAY not in mode_calls[-1].data


# --- Off: off-baseline + zones off (spec §2, §8) -----------------------------
async def test_entering_off_writes_off_baseline_and_sets_off(
    hass: HomeAssistant,
) -> None:
    mode_calls, sched_calls = await _setup(hass)
    assert len(sched_calls) == 1  # normal baseline at setup

    await _select(hass, "off")

    # Off-baseline written (flat frost temp) ...
    assert len(sched_calls) == 2
    assert sched_calls[-1].data[ATTR_BLOCKS] == [
        {"start": "00:00", "end": "24:00", "temperature": OFF_TEMP}
    ]
    # ... and the zone was turned off.
    assert mode_calls[-1].data[ATTR_HVAC_MODE] == "off"
    assert ATTR_TEMPERATURE not in mode_calls[-1].data
    assert hass.states.get(CLIMATE).attributes["zone_state"] == "Off"


async def test_off_pauses_state_machine(hass: HomeAssistant) -> None:
    """In Off a source change produces no further set_mode overlays (spec §2)."""
    mode_calls, _ = await _setup(hass)
    await _select(hass, "off")
    before = len(mode_calls)

    hass.states.async_set(ZONE_SOURCE, "on")  # someone enters the zone
    await hass.async_block_till_done()
    assert len(mode_calls) == before


# --- master switch drives the per-zone hvac_mode (spec §2/§4) ----------------
async def test_master_off_sets_zone_off_and_auto_restores(
    hass: HomeAssistant,
) -> None:
    """Master Off puts the zone climate in ``off``; Auto returns it to ``auto``."""
    await _setup(hass)
    assert hass.states.get(CLIMATE).state == "auto"

    await _select(hass, "off")
    assert hass.states.get(CLIMATE).state == "off"

    await _select(hass, "auto")
    assert hass.states.get(CLIMATE).state == "auto"


async def test_master_off_then_auto_drops_a_manual_stand(
    hass: HomeAssistant,
) -> None:
    """A per-zone manual stand is dropped when the master goes Off -> Auto."""
    await _setup(hass)
    # Take a manual stand (boost -> hvac_mode heat).
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {ATTR_ENTITY_ID: CLIMATE, "preset_mode": "boost"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == "heat"

    await _select(hass, "off")
    assert hass.states.get(CLIMATE).state == "off"

    await _select(hass, "auto")
    # Back to auto, not the prior manual heat stand (master is the top control).
    assert hass.states.get(CLIMATE).state == "auto"


# --- leaving Off -> auto rewrites the normal baseline (spec §8) --------------
async def test_leaving_off_to_auto_rewrites_normal_baseline(
    hass: HomeAssistant,
) -> None:
    _, sched_calls = await _setup(hass)
    await _select(hass, "off")
    assert sched_calls[-1].data[ATTR_BLOCKS][0]["temperature"] == OFF_TEMP
    n_after_off = len(sched_calls)

    await _select(hass, "auto")

    # Leaving Off writes the derived normal baseline back (spec §8.2).
    assert len(sched_calls) == n_after_off + 1
    assert sched_calls[-1].data[ATTR_BLOCKS] == [
        {"start": "00:00", "end": "24:00", "temperature": STANDBY_TEMP}
    ]


# --- auto <-> fallback writes NO schedule (spec §8) --------------------------
async def test_auto_fallback_roundtrip_writes_no_schedule(
    hass: HomeAssistant,
) -> None:
    _, sched_calls = await _setup(hass)
    assert len(sched_calls) == 1  # only the first-setup normal baseline

    await _select(hass, "fallback")
    assert len(sched_calls) == 1  # no write on auto->fallback

    await _select(hass, "auto")
    assert len(sched_calls) == 1  # no write on fallback->auto


# --- fallback stops set_mode overlays (spec §2) ------------------------------
async def test_fallback_stops_overlays(hass: HomeAssistant) -> None:
    mode_calls, _ = await _setup(hass)

    await _select(hass, "fallback")
    before = len(mode_calls)

    # A source change in fallback must not assert a new overlay (HA observes).
    hass.states.async_set(ZONE_SOURCE, "on")
    await hass.async_block_till_done()
    assert len(mode_calls) == before
    assert hass.states.get(CLIMATE).attributes["zone_state"] == "Fallback"


async def test_fallback_to_auto_reasserts_overlay(hass: HomeAssistant) -> None:
    """Returning to auto re-asserts the overlay even if the setpoint is unchanged."""
    mode_calls, _ = await _setup(hass)
    await _select(hass, "fallback")
    before = len(mode_calls)

    await _select(hass, "auto")
    # The applied-cache was dropped on fallback, so auto re-issues the Standby overlay.
    assert len(mode_calls) == before + 1
    assert mode_calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP


# --- dead-man's-switch heartbeat (spec §7) -----------------------------------
async def test_heartbeat_reasserts_overlay(
    hass: HomeAssistant, freezer
) -> None:
    """With the dead-man's-switch on, the heartbeat re-asserts the overlay."""
    mode_calls, _ = await _setup(hass)  # DMS default on
    n_setup = len(mode_calls)

    # Advance past one heartbeat interval (10 min) -> re-assert of Standby.
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(mode_calls) > n_setup
    assert mode_calls[-1].data[ATTR_TEMPERATURE] == STANDBY_TEMP
    assert mode_calls[-1].data[ATTR_HVAC_MODE] == "heat"


async def test_no_heartbeat_when_dead_mans_switch_off(
    hass: HomeAssistant, freezer
) -> None:
    mode_calls, _ = await _setup(hass, options={CONF_DEAD_MANS_SWITCH: False})
    n_setup = len(mode_calls)

    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # No heartbeat re-assert (the overlay lapses on its own termination, spec §7).
    assert len(mode_calls) == n_setup


async def test_no_heartbeat_in_fallback(hass: HomeAssistant, freezer) -> None:
    mode_calls, _ = await _setup(hass)
    await _select(hass, "fallback")
    n_after = len(mode_calls)

    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(mode_calls) == n_after  # no heartbeat while observing
