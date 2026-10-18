"""Zone Heating override / Custom-detection (pure Python, HA-agnostic).

Implements the shadow-state algorithm from spec §5: when a zone's **underlying**
setpoint changes (a state-listener on the target climate entities fires), decide
whether that change is a genuine *manual* change (a physical knob turn or a card
action outside HA's own command) that should flip the zone into **Custom**, or a
change HA itself caused and must ignore.

This module must stay free of any Home Assistant imports. The time-based decisions
("within a debounce window") are pre-computed by the coordinator (which owns the
clock and the stored timestamps) and passed in as plain booleans; this keeps the
rule table deterministic and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

# Setpoints equal within this margin count as "the same value" (float noise /
# unit rounding between HA's command and the device's echo).
DEFAULT_SETPOINT_TOLERANCE = 0.05


@dataclass(frozen=True)
class OverrideInputs:
    """Flat inputs for one underlying-setpoint-change evaluation (spec §5).

    The coordinator resolves live state + its shadow bookkeeping into these:

    - ``reported_setpoint``: the newly reported underlying setpoint.
    - ``last_commanded_setpoint``: the setpoint HA last wrote to this zone (or
      ``None`` if HA has not commanded it yet).
    - ``ha_just_commanded``: HA issued its own command within the own-command
      debounce (~10 s, spec §5 step 1).
    - ``presence_change_recent``: an HA-initiated presence switch happened within
      the presence debounce (~30 s, spec §5 step 2).
    - ``is_expected_schedule_fallback``: an overlay expired and the zone fell back
      to the Tado schedule (spec §5 step 3). **F3 always passes ``False``**; the
      ``switch.<zone>_schedule`` wiring that computes it is F4/F5.
    """

    reported_setpoint: float | None
    last_commanded_setpoint: float | None
    ha_just_commanded: bool
    presence_change_recent: bool
    is_expected_schedule_fallback: bool = False


@dataclass(frozen=True)
class OverrideDecision:
    """Outcome of the Custom-detection (spec §5)."""

    # True => a manual Custom change: coordinator sets hvac_mode=heat,
    # preset=none, zone_state=Custom, holds ``custom_setpoint`` and starts/
    # refreshes the respect-window.
    is_manual_custom: bool
    # The setpoint to hold for Custom (the reported value) when manual; else None.
    custom_setpoint: float | None


def detect_custom_change(
    inp: OverrideInputs, *, tolerance: float = DEFAULT_SETPOINT_TOLERANCE
) -> OverrideDecision:
    """Decide whether an underlying-setpoint change is a manual Custom (spec §5).

    Order of the guards mirrors spec §5:
      0. no reported value, or it equals what HA last commanded -> not manual
         (HA's own value echoed back by the device);
      1. HA just issued its own command (own-command debounce) -> ignore;
      2. an HA-initiated presence switch just happened (presence debounce) ->
         ignore;
      3. an expected schedule fallback (overlay expiry) -> not Custom (F3: never);
      4. otherwise -> **Custom**, holding the reported value.
    """
    reported = inp.reported_setpoint
    if reported is None:
        return OverrideDecision(is_manual_custom=False, custom_setpoint=None)

    if (
        inp.last_commanded_setpoint is not None
        and abs(reported - inp.last_commanded_setpoint) <= tolerance
    ):
        return OverrideDecision(is_manual_custom=False, custom_setpoint=None)

    if inp.ha_just_commanded:
        return OverrideDecision(is_manual_custom=False, custom_setpoint=None)

    if inp.presence_change_recent:
        return OverrideDecision(is_manual_custom=False, custom_setpoint=None)

    if inp.is_expected_schedule_fallback:
        return OverrideDecision(is_manual_custom=False, custom_setpoint=None)

    return OverrideDecision(is_manual_custom=True, custom_setpoint=reported)
