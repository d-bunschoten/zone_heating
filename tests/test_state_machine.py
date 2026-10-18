"""Pure-engine unit tests for the auto state machine (spec §3).

Fast and HA-free: these import only ``engine.state_machine``.
"""

from __future__ import annotations

import pytest

from custom_components.zone_heating.engine.state_machine import (
    HVAC_AUTO,
    HVAC_HEAT,
    HVAC_OFF,
    PRESET_ACTIVE,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_STANDBY,
    STAND_ACTIVE,
    STAND_AWAY,
    STAND_BOOST,
    STAND_CUSTOM,
    STAND_OFF,
    STAND_STANDBY,
    STAND_WINDOW_OPEN,
    AutoInputs,
    ScheduleState,
    decide,
    decide_auto,
)

BOOST = 23.0

AWAY, STANDBY, ACTIVE = 16.0, 18.0, 20.0


def _inp(**overrides) -> AutoInputs:
    """Base: home present, nobody in the zone, no schedules."""
    base = dict(
        window_open=False,
        home_present=True,
        present=False,
        away_temp=AWAY,
        standby_temp=STANDBY,
        active_temp=ACTIVE,
        active_schedule=None,
        standby_schedule=None,
    )
    base.update(overrides)
    return AutoInputs(**base)


# (description, inputs, expected stand, expected setpoint, expected preset)
CASES = [
    (
        "window open wins over everything",
        _inp(window_open=True, home_present=False, present=True),
        STAND_WINDOW_OPEN,
        None,
        None,
    ),
    (
        "house away -> Away at away_temp",
        _inp(home_present=False, present=True),
        STAND_AWAY,
        AWAY,
        PRESET_AWAY,
    ),
    (
        "present, no active schedule -> Active at active_temp base",
        _inp(present=True),
        STAND_ACTIVE,
        ACTIVE,
        PRESET_ACTIVE,
    ),
    (
        "present, active schedule on-block -> Active at block temp",
        _inp(present=True, active_schedule=ScheduleState(block_temp=21.5)),
        STAND_ACTIVE,
        21.5,
        PRESET_ACTIVE,
    ),
    (
        "present, active schedule gap -> Standby (gate closed)",
        _inp(present=True, active_schedule=ScheduleState(block_temp=None)),
        STAND_STANDBY,
        STANDBY,
        PRESET_STANDBY,
    ),
    (
        "home present, not in zone -> Standby at standby_temp base",
        _inp(present=False),
        STAND_STANDBY,
        STANDBY,
        PRESET_STANDBY,
    ),
    (
        "standby schedule on-block sets standby temp",
        _inp(present=False, standby_schedule=ScheduleState(block_temp=17.0)),
        STAND_STANDBY,
        17.0,
        PRESET_STANDBY,
    ),
    (
        "standby schedule gap falls back to standby_temp base",
        _inp(present=False, standby_schedule=ScheduleState(block_temp=None)),
        STAND_STANDBY,
        STANDBY,
        PRESET_STANDBY,
    ),
    (
        "active gate closed but standby schedule block applies",
        _inp(
            present=True,
            active_schedule=ScheduleState(block_temp=None),
            standby_schedule=ScheduleState(block_temp=17.5),
        ),
        STAND_STANDBY,
        17.5,
        PRESET_STANDBY,
    ),
    (
        "not present ignores active schedule on-block (no presence -> Standby)",
        _inp(present=False, active_schedule=ScheduleState(block_temp=21.0)),
        STAND_STANDBY,
        STANDBY,
        PRESET_STANDBY,
    ),
]


@pytest.mark.parametrize(
    ("desc", "inp", "stand", "setpoint", "preset"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_decide_auto_table(desc, inp, stand, setpoint, preset) -> None:
    decision = decide_auto(inp)
    assert decision.stand == stand, desc
    assert decision.setpoint == setpoint, desc
    assert decision.preset_mode == preset, desc
    # Only the window-open stand turns the zone off.
    assert decision.off is (stand == STAND_WINDOW_OPEN), desc


def test_decide_dispatches_auto() -> None:
    decision = decide(HVAC_AUTO, _inp(present=True))
    assert decision.stand == STAND_ACTIVE


# --- hvac_mode=heat (manual stand-override, spec §3 "heat") ------------------
# (description, inputs, expected stand, setpoint, preset)
HEAT_CASES = [
    (
        "boost -> Boost at boost_temp",
        _inp(preset=PRESET_BOOST, boost_temp=BOOST),
        STAND_BOOST,
        BOOST,
        PRESET_BOOST,
    ),
    (
        "away -> Away at away_temp (sticky handled by coordinator)",
        _inp(preset=PRESET_AWAY),
        STAND_AWAY,
        AWAY,
        PRESET_AWAY,
    ),
    (
        "standby -> Standby at standby_temp base",
        _inp(preset=PRESET_STANDBY),
        STAND_STANDBY,
        STANDBY,
        PRESET_STANDBY,
    ),
    (
        "standby -> Standby uses schedule block temp when present",
        _inp(preset=PRESET_STANDBY, standby_schedule=ScheduleState(17.0)),
        STAND_STANDBY,
        17.0,
        PRESET_STANDBY,
    ),
    (
        "active -> Active at active_temp base (no schedule)",
        _inp(preset=PRESET_ACTIVE),
        STAND_ACTIVE,
        ACTIVE,
        PRESET_ACTIVE,
    ),
    (
        "active -> Active uses schedule block temp (no gate in heat)",
        _inp(preset=PRESET_ACTIVE, active_schedule=ScheduleState(21.5)),
        STAND_ACTIVE,
        21.5,
        PRESET_ACTIVE,
    ),
    (
        "active -> gap gives active_temp base (gate is auto-only, spec §3)",
        _inp(
            preset=PRESET_ACTIVE,
            present=False,
            home_present=False,
            active_schedule=ScheduleState(None),
        ),
        STAND_ACTIVE,
        ACTIVE,
        PRESET_ACTIVE,
    ),
    (
        "none -> Custom at the dialed setpoint, preset none",
        _inp(preset=None, custom_setpoint=19.5),
        STAND_CUSTOM,
        19.5,
        None,
    ),
]


@pytest.mark.parametrize(
    ("desc", "inp", "stand", "setpoint", "preset"),
    HEAT_CASES,
    ids=[c[0] for c in HEAT_CASES],
)
def test_decide_heat_table(desc, inp, stand, setpoint, preset) -> None:
    decision = decide(HVAC_HEAT, inp)
    assert decision.stand == stand, desc
    assert decision.setpoint == setpoint, desc
    assert decision.preset_mode == preset, desc
    assert decision.off is False, desc


def test_decide_heat_ignores_home_away_and_presence() -> None:
    """In heat the forced stand ignores presence/home (coordinator handles end)."""
    decision = decide(
        HVAC_HEAT,
        _inp(preset=PRESET_ACTIVE, home_present=False, present=False),
    )
    assert decision.stand == STAND_ACTIVE
    assert decision.setpoint == ACTIVE


def test_decide_heat_unknown_preset_raises() -> None:
    with pytest.raises(ValueError, match="unknown preset"):
        decide(HVAC_HEAT, _inp(preset="banana"))


def test_decide_off() -> None:
    decision = decide(HVAC_OFF, _inp())
    assert decision.stand == STAND_OFF
    assert decision.setpoint is None
    assert decision.preset_mode is None
    assert decision.off is True


def test_decide_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown hvac_mode"):
        decide("banana", _inp())
