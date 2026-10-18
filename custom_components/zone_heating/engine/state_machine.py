"""Zone Heating state machine (pure Python, HA-agnostic).

This module holds the core per-zone stand-selection logic for ``hvac_mode=auto``
(spec §3). It must remain free of any Home Assistant imports so it can be
unit-tested in isolation (plan §3).

The ``auto`` branch (F2) and the ``heat``/``off`` branches (F3) are implemented
here. ``heat`` is the manual stand-override: a forced preset (away/standby/active/
boost) or Custom (``preset=None``, the manually dialed setpoint). ``off`` turns
the zone off. Durations, the respect-window and sticky-away live in the HA glue
(coordinator); this module only maps (hvac_mode, preset) -> desired setpoint.
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
STAND_BOOST = "Boost"
STAND_CUSTOM = "Custom"
STAND_WINDOW_OPEN = "Window-open"
STAND_OFF = "Off"
# Master-mode Fallback: HA observes, the zone runs the Tado baseline (spec §2, §4).
STAND_FALLBACK = "Fallback"

# --- preset_mode values (spec §1.1, §4) -------------------------------------
# Lowercase; identical to the climate entity's preset strings (``const.py``
# ``PRESET_*``). Duplicated here deliberately: the engine cannot import the HA
# ``const`` module (which imports Home Assistant) and must stay pure.
PRESET_AWAY = "away"
PRESET_STANDBY = "standby"
PRESET_ACTIVE = "active"
PRESET_BOOST = "boost"


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
    # --- manual control inputs (hvac_mode=heat, spec §3 "heat") -------------
    # ``preset`` selects the forced stand in ``heat``: one of PRESET_AWAY /
    # PRESET_STANDBY / PRESET_ACTIVE / PRESET_BOOST, or ``None`` for **Custom**
    # (the manually dialed setpoint). Ignored in ``auto``/``off``.
    preset: str | None = None
    # The manually dialed setpoint used for Custom (preset=None in heat).
    custom_setpoint: float | None = None
    # Global boost temperature (``number.heating_boost_temp``), for PRESET_BOOST.
    boost_temp: float | None = None


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


def _active_heat_temp(inp: AutoInputs) -> float:
    """Active temperature for the manual ``active`` preset in ``heat`` (spec §3).

    In ``heat`` the schedule supplies **only the temperature** of the current
    on-block (no gate; the gate is auto-only). A gap or no schedule -> the
    ``active_temp`` base.
    """
    sched = inp.active_schedule
    if sched is not None and sched.block_temp is not None:
        return sched.block_temp
    return inp.active_temp


def decide_heat(inp: AutoInputs) -> ZoneDecision:
    """Choose the stand + setpoint for ``hvac_mode=heat`` (spec §3 "heat").

    The forced ``preset`` maps straight onto a stand; the schedules only supply
    the current block temperature (no gate — that is auto-only). Durations, the
    respect-window and sticky-away are enforced by the coordinator, not here.

      - ``boost``   -> Boost at ``boost_temp``
      - ``away``    -> Away at ``away_temp`` (sticky; coordinator never times out)
      - ``standby`` -> Standby at the standby block temp, else ``standby_temp``
      - ``active``  -> Active at the active block temp, else ``active_temp``
      - ``None``    -> Custom at the manually dialed ``custom_setpoint``
    """
    preset = inp.preset
    if preset == PRESET_BOOST:
        return ZoneDecision(
            stand=STAND_BOOST,
            setpoint=inp.boost_temp,
            preset_mode=PRESET_BOOST,
            off=False,
        )
    if preset == PRESET_AWAY:
        return ZoneDecision(
            stand=STAND_AWAY,
            setpoint=inp.away_temp,
            preset_mode=PRESET_AWAY,
            off=False,
        )
    if preset == PRESET_STANDBY:
        return ZoneDecision(
            stand=STAND_STANDBY,
            setpoint=_standby_temp(inp),
            preset_mode=PRESET_STANDBY,
            off=False,
        )
    if preset == PRESET_ACTIVE:
        return ZoneDecision(
            stand=STAND_ACTIVE,
            setpoint=_active_heat_temp(inp),
            preset_mode=PRESET_ACTIVE,
            off=False,
        )
    if preset is None:
        # Custom: preset_mode none, hold the manually dialed setpoint (spec §4).
        return ZoneDecision(
            stand=STAND_CUSTOM,
            setpoint=inp.custom_setpoint,
            preset_mode=None,
            off=False,
        )
    raise ValueError(f"unknown preset: {preset!r}")


def decide_off(inp: AutoInputs) -> ZoneDecision:
    """``hvac_mode=off`` -> zone off (spec §3 "off"). Sticky (coordinator)."""
    return ZoneDecision(
        stand=STAND_OFF, setpoint=None, preset_mode=None, off=True
    )


def decide(hvac_mode: str, inp: AutoInputs) -> ZoneDecision:
    """Top-level dispatch on the zone's ``hvac_mode``.

    ``hvac_mode`` is kept as the dispatch argument (not a field on
    :class:`AutoInputs`) so the existing ``decide(HVAC_AUTO, inp)`` call shape is
    preserved; the manual-control fields (``preset``/``custom_setpoint``/
    ``boost_temp``) live on :class:`AutoInputs`.
    """
    if hvac_mode == HVAC_AUTO:
        return decide_auto(inp)
    if hvac_mode == HVAC_HEAT:
        return decide_heat(inp)
    if hvac_mode == HVAC_OFF:
        return decide_off(inp)
    raise ValueError(f"unknown hvac_mode: {hvac_mode!r}")
