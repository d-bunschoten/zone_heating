"""Zone Heating state machine (pure Python, HA-agnostic).

This module holds the core per-zone stand-selection logic for ``hvac_mode=auto``
(spec §3). It must remain free of any Home Assistant imports so it can be
unit-tested in isolation (plan §3).

Only the ``auto`` branch is implemented in F2. The ``heat`` and ``off`` branches
are explicit extension points for F3 and raise ``NotImplementedError``.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- hvac_mode values -------------------------------------------------------
# These mirror Home Assistant's ``HVACMode`` string values, kept as plain
# strings so the engine stays import-pure.
HVAC_AUTO = "auto"
HVAC_HEAT = "heat"
HVAC_OFF = "off"

# --- zone_state ("stand") values (spec §4) ----------------------------------
# User-facing strings surfaced on ``climate.<zone>``'s ``zone_state`` attribute.
STAND_AWAY = "Away"
STAND_ACTIVE = "Active"
STAND_STANDBY = "Standby"
STAND_WINDOW_OPEN = "Window-open"
STAND_OFF = "Off"

# --- preset_mode values (spec §1.1, §4) -------------------------------------
# Lowercase; identical to the climate entity's preset strings (``const.py``
# ``PRESET_*``). Duplicated here deliberately: the engine cannot import the HA
# ``const`` module (which imports Home Assistant) and must stay pure.
PRESET_AWAY = "away"
PRESET_STANDBY = "standby"
PRESET_ACTIVE = "active"


@dataclass(frozen=True)
class ScheduleState:
    """Current state of a referenced ``schedule`` helper.

    ``block_temp`` is the temperature of the on-block that is active *now*, or
    ``None`` when there is no on-block right now (a gap in the schedule). A zone
    that has *no* schedule configured is represented by passing ``None`` in place
    of a ``ScheduleState`` on :class:`AutoInputs`.
    """

    block_temp: float | None


@dataclass(frozen=True)
class AutoInputs:
    """Flat inputs for one zone's ``hvac_mode=auto`` decision (spec §3).

    The HA glue (coordinator) resolves live states into these plain values; the
    engine never touches Home Assistant.
    """

    window_open: bool
    home_present: bool
    present: bool
    away_temp: float
    standby_temp: float
    active_temp: float
    # ``None`` means the stand has no schedule configured.
    active_schedule: ScheduleState | None = None
    standby_schedule: ScheduleState | None = None


@dataclass(frozen=True)
class ZoneDecision:
    """The chosen stand + target setpoint for a zone (spec §3)."""

    stand: str
    setpoint: float | None
    preset_mode: str | None
    # True => the backend should turn the zone off (no setpoint). Used for the
    # window-open safety modifier; the setpoint is then ``None``.
    off: bool


def _active_allowed(inp: AutoInputs) -> tuple[bool, float | None]:
    """Resolve the Active "poort" (gate) and its temperature (spec §1.1, §3).

    - No ``active_schedule`` configured -> the gate is lifted: Active is allowed
      whenever present, at the ``active_temp`` base.
    - ``active_schedule`` configured -> Active is allowed *only* during an
      on-block, at that block's temperature; a gap means not allowed.
    """
    sched = inp.active_schedule
    if sched is None:
        return True, inp.active_temp
    if sched.block_temp is not None:
        return True, sched.block_temp
    return False, None


def _standby_temp(inp: AutoInputs) -> float:
    """Resolve the Standby temperature (spec §1.1, §3).

    ``standby_schedule`` has no gate: use its current block temperature if there
    is one, otherwise fall back to the ``standby_temp`` base.
    """
    sched = inp.standby_schedule
    if sched is not None and sched.block_temp is not None:
        return sched.block_temp
    return inp.standby_temp


def decide_auto(inp: AutoInputs) -> ZoneDecision:
    """Choose the stand + setpoint for ``hvac_mode=auto`` (spec §3).

    Order of evaluation:
      1. window_open -> off (safety modifier on top);
      2. home not present -> Away (away_temp);
      3. present AND Active allowed -> Active (on-block temp, else active_temp);
      4. otherwise -> Standby (standby block temp, else standby_temp).
    """
    if inp.window_open:
        return ZoneDecision(
            stand=STAND_WINDOW_OPEN, setpoint=None, preset_mode=None, off=True
        )

    if not inp.home_present:
        return ZoneDecision(
            stand=STAND_AWAY,
            setpoint=inp.away_temp,
            preset_mode=PRESET_AWAY,
            off=False,
        )

    allowed, active_temp = _active_allowed(inp)
    if inp.present and allowed:
        return ZoneDecision(
            stand=STAND_ACTIVE,
            setpoint=active_temp,
            preset_mode=PRESET_ACTIVE,
            off=False,
        )

    return ZoneDecision(
        stand=STAND_STANDBY,
        setpoint=_standby_temp(inp),
        preset_mode=PRESET_STANDBY,
        off=False,
    )


def decide(hvac_mode: str, inp: AutoInputs) -> ZoneDecision:
    """Top-level dispatch on the zone's ``hvac_mode``.

    Only ``auto`` is implemented in F2. ``heat`` (manual override / respect
    window) and ``off`` are handled in F3 and are explicit extension points.
    """
    if hvac_mode == HVAC_AUTO:
        return decide_auto(inp)
    if hvac_mode == HVAC_HEAT:
        # TODO(F3): manual stand-override with respect-window (spec §3 "heat").
        raise NotImplementedError("hvac_mode=heat is handled in F3")
    if hvac_mode == HVAC_OFF:
        # TODO(F3): zone off until the user picks auto/heat (spec §3 "off").
        raise NotImplementedError("hvac_mode=off is handled in F3")
    raise ValueError(f"unknown hvac_mode: {hvac_mode!r}")
