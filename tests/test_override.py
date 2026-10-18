"""Pure-engine unit tests for the Custom (shadow-state) detection (spec §5).

Fast and HA-free: these import only ``engine.override``.
"""

from __future__ import annotations

import pytest

from custom_components.zone_heating.engine.override import (
    OverrideInputs,
    detect_custom_change,
)


def _inp(**overrides) -> OverrideInputs:
    """Base: a genuine external change (no HA cause, no fallback)."""
    base = dict(
        reported_setpoint=21.0,
        last_commanded_setpoint=18.0,
        ha_just_commanded=False,
        presence_change_recent=False,
        is_expected_schedule_fallback=False,
    )
    base.update(overrides)
    return OverrideInputs(**base)


# (description, inputs, expected is_manual_custom, expected custom_setpoint)
CASES = [
    (
        "external knob turn -> Custom at reported value",
        _inp(reported_setpoint=21.0),
        True,
        21.0,
    ),
    (
        "equals last HA command -> own echo, not Custom",
        _inp(reported_setpoint=18.0, last_commanded_setpoint=18.0),
        False,
        None,
    ),
    (
        "within own-command debounce -> ignore",
        _inp(ha_just_commanded=True),
        False,
        None,
    ),
    (
        "within presence debounce -> ignore",
        _inp(presence_change_recent=True),
        False,
        None,
    ),
    (
        "expected schedule fallback -> not Custom (F4/F5 input)",
        _inp(is_expected_schedule_fallback=True),
        False,
        None,
    ),
    (
        "no reported value -> nothing to detect",
        _inp(reported_setpoint=None),
        False,
        None,
    ),
    (
        "no prior HA command, real change -> Custom",
        _inp(last_commanded_setpoint=None, reported_setpoint=22.5),
        True,
        22.5,
    ),
    (
        "float noise vs last command -> treated as echo (tolerance)",
        _inp(reported_setpoint=18.02, last_commanded_setpoint=18.0),
        False,
        None,
    ),
]


@pytest.mark.parametrize(
    ("desc", "inp", "is_manual", "setpoint"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_detect_custom_change_table(desc, inp, is_manual, setpoint) -> None:
    decision = detect_custom_change(inp)
    assert decision.is_manual_custom is is_manual, desc
    assert decision.custom_setpoint == setpoint, desc


def test_own_command_takes_priority_over_a_different_value() -> None:
    """A different value but within the own-command window is still ignored."""
    decision = detect_custom_change(
        _inp(reported_setpoint=25.0, ha_just_commanded=True)
    )
    assert decision.is_manual_custom is False
