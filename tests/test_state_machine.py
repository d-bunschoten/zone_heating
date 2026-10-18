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
    PRESET_STANDBY,
    STAND_ACTIVE,
    STAND_AWAY,
    STAND_STANDBY,
    STAND_WINDOW_OPEN,
    AutoInputs,
    ScheduleState,
    decide,
    decide_auto,
)

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


def test_decide_heat_is_f3() -> None:
    with pytest.raises(NotImplementedError):
        decide(HVAC_HEAT, _inp())


def test_decide_off_is_f3() -> None:
    with pytest.raises(NotImplementedError):
        decide(HVAC_OFF, _inp())


def test_decide_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown hvac_mode"):
        decide("banana", _inp())
