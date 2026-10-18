"""Zone Heating coordinator (HA glue, spec §3, §4, §5, §6).

Reads live HA state for each zone, builds the pure-engine inputs, runs the
state machine (:mod:`engine.state_machine`) and applies the decision through a
:class:`~.backend.HeatingBackend` — but only when the desired setpoint/stand
changes (spec §3: "een stand-wissel herschrijft alleen als het doel-setpoint
wijzigt").

F2 wired the ``auto`` branch. F3 adds **manual control**: the coordinator owns
each zone's manual state (``hvac_mode`` heat/off, the forced preset or Custom
setpoint), drives the ``heat``/``off`` engine branches, runs the respect-window /
boost timers, keeps the ``away`` preset sticky, ends non-sticky heat stands when
the house becomes Away, and detects external (physical-knob) setpoint changes as
Custom via the shadow-state algorithm (:mod:`engine.override`, spec §5).

Triggering is event-driven (state listeners on the source entities) **plus** a
periodic tick for schedule-block boundary crossings (spec §3). Only the ``auto``
master mode drives devices here; ``fallback``/``off`` are F4 and are guarded.
Restart-persistence of a running timer / the manual state is F5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Callable

from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .backend import HeatingBackend
from .const import (
    CONF_ACTIVE_SCHEDULE,
    CONF_STANDBY_SCHEDULE,
    CONF_TARGETS,
    DEFAULT_BOOST_DURATION,
    DEFAULT_OVERRIDE_TIMEOUT,
    DOMAIN,
    KEY_ACTIVE_TEMP,
    KEY_AWAY_TEMP,
    KEY_BOOST_DURATION,
    KEY_BOOST_TEMP,
    KEY_CLIMATE,
    KEY_HEATING_MODE,
    KEY_HOME_PRESENT,
    KEY_OVERRIDE_TIMEOUT,
    KEY_PRESENT,
    KEY_STANDBY_TEMP,
    LOGGER,
    MODE_AUTO,
    OWN_COMMAND_DEBOUNCE_SECONDS,
    PRESENCE_DEBOUNCE_SECONDS,
    SCHEDULE_TEMPERATURE_ATTR,
    SIGNAL_ZONE_UPDATE,
    TICK_INTERVAL_MINUTES,
)
from .engine.override import OverrideInputs, detect_custom_change
from .engine.state_machine import (
    HVAC_AUTO,
    HVAC_HEAT,
    HVAC_OFF,
    PRESET_AWAY,
    PRESET_BOOST,
    STAND_OFF,
    AutoInputs,
    ScheduleState,
    ZoneDecision,
    decide,
)
from .helpers import iter_zone_subentries, zone_name

_UNREADY = (None, STATE_UNKNOWN, STATE_UNAVAILABLE)
_SETPOINT_TOLERANCE = 0.05


@dataclass
class ZoneContext:
    """Resolved config + source entity_ids for one zone."""

    subentry_id: str
    name: str
    targets: list[str]
    climate_entity: str | None = None
    present_entity: str | None = None
    away_number: str | None = None
    standby_number: str | None = None
    active_number: str | None = None
    # Referenced schedule helper entity_ids (``None`` => no schedule, spec §1.1).
    standby_schedule: str | None = None
    active_schedule: str | None = None


@dataclass
class ZoneManualState:
    """Per-zone manual-control state (hvac_mode heat/off, spec §3/§4/§5).

    In ``auto`` the coordinator chooses the stand; in ``heat`` the user has forced
    a stand via a ``preset`` (or Custom when ``preset is None``); ``off`` is the
    zone off. ``last_commanded_*`` / ``presence_change_at`` back the shadow-state
    Custom detection (spec §5). The timer backs the respect-window / boost
    duration; it is lost on restart (RestoreEntity is F5).
    """

    hvac_mode: str = HVAC_AUTO
    preset: str | None = None  # forced preset in heat; None => Custom
    custom_setpoint: float | None = None
    # Shadow-state (spec §5).
    last_commanded_setpoint: float | None = None
    last_command_at: datetime | None = None
    presence_change_at: datetime | None = None
    # Respect-window / boost timer.
    timer_cancel: Callable[[], None] | None = None
    timer_end: datetime | None = None


@dataclass
class ZoneRuntimeState:
    """Per-zone runtime surfaced on ``climate.<zone>`` (spec §4)."""

    zone_state: str | None = None
    setpoint: float | None = None
    preset_mode: str | None = None
    present: bool = False
    window_open: bool = False


@dataclass
class _Applied:
    """Last command actually sent to the backend (redundancy guard, spec §3)."""

    off: bool
    setpoint: float | None


class ZoneHeatingCoordinator:
    """Drives every zone's state machine and applies it via the backend."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry,
        backend: HeatingBackend,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.backend = backend
        self.zones: dict[str, ZoneContext] = {}
        self.zone_runtime: dict[str, ZoneRuntimeState] = {}
        self.manual: dict[str, ZoneManualState] = {}
        self._applied: dict[str, _Applied] = {}
        self._mode_entity: str | None = None
        self._home_present_entity: str | None = None
        self._boost_temp_entity: str | None = None
        self._boost_duration_entity: str | None = None
        self._override_timeout_entity: str | None = None
        # target entity_id -> list of zone subentry_ids that use it.
        self._target_to_zone: dict[str, list[str]] = {}

    # --- setup / teardown ---------------------------------------------------
    async def async_setup(self) -> None:
        """Resolve entities, register listeners + tick, do the first eval.

        Runs *after* the platforms are forwarded, so all source entities already
        exist in the registry and have written their initial state.
        """
        registry = er.async_get(self.hass)
        self._mode_entity = registry.async_get_entity_id(
            Platform.SELECT, DOMAIN, KEY_HEATING_MODE
        )
        self._home_present_entity = registry.async_get_entity_id(
            Platform.BINARY_SENSOR, DOMAIN, KEY_HOME_PRESENT
        )
        self._boost_temp_entity = registry.async_get_entity_id(
            Platform.NUMBER, DOMAIN, KEY_BOOST_TEMP
        )
        self._boost_duration_entity = registry.async_get_entity_id(
            Platform.NUMBER, DOMAIN, KEY_BOOST_DURATION
        )
        self._override_timeout_entity = registry.async_get_entity_id(
            Platform.NUMBER, DOMAIN, KEY_OVERRIDE_TIMEOUT
        )

        tracked: set[str] = set()
        if self._mode_entity:
            tracked.add(self._mode_entity)
        if self._home_present_entity:
            tracked.add(self._home_present_entity)

        targets: set[str] = set()
        for subentry in iter_zone_subentries(self.entry):
            sid = subentry.subentry_id
            ctx = ZoneContext(
                subentry_id=sid,
                name=zone_name(subentry),
                targets=list(subentry.data.get(CONF_TARGETS, [])),
                climate_entity=registry.async_get_entity_id(
                    Platform.CLIMATE, DOMAIN, f"{sid}_{KEY_CLIMATE}"
                ),
                present_entity=registry.async_get_entity_id(
                    Platform.BINARY_SENSOR, DOMAIN, f"{sid}_{KEY_PRESENT}"
                ),
                away_number=registry.async_get_entity_id(
                    Platform.NUMBER, DOMAIN, f"{sid}_{KEY_AWAY_TEMP}"
                ),
                standby_number=registry.async_get_entity_id(
                    Platform.NUMBER, DOMAIN, f"{sid}_{KEY_STANDBY_TEMP}"
                ),
                active_number=registry.async_get_entity_id(
                    Platform.NUMBER, DOMAIN, f"{sid}_{KEY_ACTIVE_TEMP}"
                ),
                standby_schedule=subentry.data.get(CONF_STANDBY_SCHEDULE),
                active_schedule=subentry.data.get(CONF_ACTIVE_SCHEDULE),
            )
            self.zones[sid] = ctx
            self.zone_runtime.setdefault(sid, ZoneRuntimeState())
            self.manual.setdefault(sid, ZoneManualState())

            for entity_id in (
                ctx.present_entity,
                ctx.away_number,
                ctx.standby_number,
                ctx.active_number,
                ctx.standby_schedule,
                ctx.active_schedule,
            ):
                if entity_id:
                    tracked.add(entity_id)

            for target in ctx.targets:
                targets.add(target)
                self._target_to_zone.setdefault(target, []).append(sid)

        if tracked:
            self.entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, sorted(tracked), self._handle_source_change
                )
            )

        # Underlying-setpoint changes -> shadow-state Custom detection (spec §5).
        if targets:
            self.entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, sorted(targets), self._handle_target_change
                )
            )

        # Periodic tick for schedule-block boundary crossings (spec §3).
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._handle_tick,
                timedelta(minutes=TICK_INTERVAL_MINUTES),
            )
        )
        # Cancel any running timer on unload.
        self.entry.async_on_unload(self._cancel_all_timers)

        await self.async_refresh()

    @callback
    def _cancel_all_timers(self) -> None:
        for sid in list(self.manual):
            self._cancel_timer(sid)

    # --- triggers -----------------------------------------------------------
    @callback
    def _handle_source_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        # Stamp presence-change timestamps so the shadow-state can ignore
        # presence-driven underlying changes (spec §5 step 2).
        entity_id = event.data["entity_id"]
        now = dt_util.utcnow()
        if entity_id == self._home_present_entity:
            for ms in self.manual.values():
                ms.presence_change_at = now
        else:
            for sid, ctx in self.zones.items():
                if ctx.present_entity == entity_id:
                    self.manual[sid].presence_change_at = now
        self.async_request_refresh()

    @callback
    def _handle_tick(self, _now) -> None:
        self.async_request_refresh()

    @callback
    def _handle_target_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """An underlying target setpoint changed -> maybe Custom (spec §5)."""
        if self._master_mode() != MODE_AUTO:
            return
        new_state = event.data["new_state"]
        if new_state is None:
            return
        reported = self._extract_setpoint(new_state)
        if reported is None:
            return
        old_reported = self._extract_setpoint(event.data.get("old_state"))
        if (
            old_reported is not None
            and abs(reported - old_reported) <= _SETPOINT_TOLERANCE
        ):
            return  # not an actual setpoint change
        entity_id = event.data["entity_id"]
        for sid in self._target_to_zone.get(entity_id, []):
            self.entry.async_create_task(
                self.hass,
                self._async_maybe_custom(sid, reported),
                eager_start=False,
            )

    def async_request_refresh(self) -> None:
        """Schedule a re-evaluation (e.g. after a climate interaction)."""
        self.entry.async_create_task(
            self.hass, self.async_refresh(), eager_start=False
        )

    # --- climate entity interface (spec §4) ---------------------------------
    async def async_set_zone_hvac_mode(self, sid: str, hvac_mode: str) -> None:
        """Handle ``climate.set_hvac_mode`` (auto/heat/off, spec §4)."""
        ms = self.manual.get(sid)
        if ms is None:
            return
        if hvac_mode == HVAC_AUTO:
            self._to_auto_state(sid)
        elif hvac_mode == HVAC_OFF:
            self._cancel_timer(sid)
            ms.hvac_mode = HVAC_OFF
            ms.preset = None
            ms.custom_setpoint = None
        elif hvac_mode == HVAC_HEAT:
            # "take a manual stand" (spec §4). Keep a forced preset if one is
            # already set; otherwise adopt Custom at the current setpoint.
            ms.hvac_mode = HVAC_HEAT
            if ms.preset is None and ms.custom_setpoint is None:
                ms.custom_setpoint = self._current_setpoint(sid)
            self._start_stand_timer(sid)
        await self.async_refresh()

    async def async_set_zone_preset(self, sid: str, preset: str) -> None:
        """Handle ``climate.set_preset_mode`` -> force stand ``preset`` (spec §4)."""
        ms = self.manual.get(sid)
        if ms is None:
            return
        ms.hvac_mode = HVAC_HEAT
        ms.preset = preset
        ms.custom_setpoint = None
        self._start_stand_timer(sid)
        await self.async_refresh()

    async def async_set_zone_custom(self, sid: str, temperature: float) -> None:
        """Handle ``climate.set_temperature`` -> Custom on ``T`` (spec §4)."""
        ms = self.manual.get(sid)
        if ms is None:
            return
        ms.hvac_mode = HVAC_HEAT
        ms.preset = None
        ms.custom_setpoint = temperature
        self._start_stand_timer(sid)
        await self.async_refresh()

    def zone_hvac_mode(self, sid: str) -> str:
        ms = self.manual.get(sid)
        return ms.hvac_mode if ms else HVAC_AUTO

    def zone_manual_preset(self, sid: str) -> str | None:
        """The forced preset in heat (None => Custom)."""
        ms = self.manual.get(sid)
        return ms.preset if ms else None

    def zone_override_active(self, sid: str) -> bool:
        """True while a manual heat stand is in effect (spec §4)."""
        ms = self.manual.get(sid)
        return bool(ms and ms.hvac_mode == HVAC_HEAT)

    def zone_override_remaining_min(self, sid: str) -> int | None:
        """Minutes left on the respect-window / boost timer (None if sticky)."""
        ms = self.manual.get(sid)
        if ms is None or ms.timer_end is None:
            return None
        remaining = (ms.timer_end - dt_util.utcnow()).total_seconds()
        if remaining <= 0:
            return 0
        return int((remaining + 59) // 60)

    # --- timers -------------------------------------------------------------
    def _start_stand_timer(self, sid: str) -> None:
        """(Re)start the timer for the current manual stand (spec §3).

        - ``away``  -> sticky: no timer (ends only on manual change / auto).
        - ``boost`` -> ``heating_boost_duration``.
        - standby/active/Custom -> ``heating_override_timeout``.
        - ``off``   -> sticky: no timer.
        """
        ms = self.manual[sid]
        if ms.hvac_mode != HVAC_HEAT or ms.preset == PRESET_AWAY:
            self._cancel_timer(sid)  # sticky away (or not heat) -> no timeout
            return
        if ms.preset == PRESET_BOOST:
            minutes = self._read_number(self._boost_duration_entity)
            if minutes is None:
                minutes = DEFAULT_BOOST_DURATION
        else:  # standby / active / Custom
            minutes = self._read_number(self._override_timeout_entity)
            if minutes is None:
                minutes = DEFAULT_OVERRIDE_TIMEOUT
        self._arm_timer(sid, minutes)

    def _arm_timer(self, sid: str, minutes: float) -> None:
        self._cancel_timer(sid)
        if minutes <= 0:
            return
        ms = self.manual[sid]
        delay = minutes * 60
        ms.timer_end = dt_util.utcnow() + timedelta(seconds=delay)
        ms.timer_cancel = async_call_later(
            self.hass, delay, partial(self._on_timer_expire, sid)
        )

    @callback
    def _on_timer_expire(self, sid: str, _now) -> None:
        """Respect-window / boost expired -> back to auto (spec §3)."""
        ms = self.manual.get(sid)
        if ms is None:
            return
        ms.timer_cancel = None
        ms.timer_end = None
        self._to_auto_state(sid)
        self.async_request_refresh()
        self._notify(sid)

    @callback
    def _cancel_timer(self, sid: str) -> None:
        ms = self.manual.get(sid)
        if ms is None:
            return
        if ms.timer_cancel is not None:
            ms.timer_cancel()
        ms.timer_cancel = None
        ms.timer_end = None

    def _to_auto_state(self, sid: str) -> None:
        """Drop any manual stand and return the zone to auto (spec §3)."""
        self._cancel_timer(sid)
        ms = self.manual[sid]
        ms.hvac_mode = HVAC_AUTO
        ms.preset = None
        ms.custom_setpoint = None

    # --- evaluation ---------------------------------------------------------
    async def async_refresh(self) -> None:
        """Re-evaluate every zone."""
        if self._master_mode() != MODE_AUTO:
            # fallback/off master modes are F4; stay hands-off (auto only).
            return
        for ctx in self.zones.values():
            await self._async_evaluate_zone(ctx)

    async def _async_evaluate_zone(self, ctx: ZoneContext) -> None:
        sid = ctx.subentry_id
        ms = self.manual[sid]
        home_present = self._read_binary(self._home_present_entity)

        # Non-sticky heat stands end when the house becomes Away (spec §3);
        # the ``away`` preset is sticky and survives it.
        if (
            ms.hvac_mode == HVAC_HEAT
            and ms.preset != PRESET_AWAY
            and not home_present
        ):
            self._to_auto_state(sid)

        hvac = ms.hvac_mode
        present = self._read_binary(ctx.present_entity)

        if hvac == HVAC_OFF:
            decision = ZoneDecision(
                stand=STAND_OFF, setpoint=None, preset_mode=None, off=True
            )
            await self._apply(ctx, decision)
            self._update_runtime(
                ctx, decision, present=present, window_open=False
            )
            return

        inp = self._build_inputs(ctx, ms)
        if inp is None:
            LOGGER.debug("Zone %s not ready for evaluation", ctx.name)
            return
        decision = decide(hvac, inp)
        await self._apply(ctx, decision)
        self._update_runtime(
            ctx, decision, present=inp.present, window_open=inp.window_open
        )

    async def _apply(self, ctx: ZoneContext, decision: ZoneDecision) -> None:
        """Send to the backend only when the setpoint/off state changes."""
        if not decision.off and decision.setpoint is None:
            # Manual stand without a resolvable setpoint (e.g. Custom before a
            # value, or a missing boost_temp): nothing safe to write.
            LOGGER.debug("Zone %s has no setpoint to apply", ctx.name)
            return
        want = _Applied(off=decision.off, setpoint=decision.setpoint)
        if self._applied.get(ctx.subentry_id) == want:
            return
        ms = self.manual[ctx.subentry_id]
        if decision.off:
            await self.backend.set_off(ctx.targets)
        else:
            await self.backend.set_setpoint(ctx.targets, decision.setpoint)
            ms.last_commanded_setpoint = decision.setpoint
        ms.last_command_at = dt_util.utcnow()
        self._applied[ctx.subentry_id] = want

    def _update_runtime(
        self,
        ctx: ZoneContext,
        decision: ZoneDecision,
        *,
        present: bool,
        window_open: bool,
    ) -> None:
        new = ZoneRuntimeState(
            zone_state=decision.stand,
            setpoint=decision.setpoint,
            preset_mode=decision.preset_mode,
            present=present,
            window_open=window_open,
        )
        if self.zone_runtime.get(ctx.subentry_id) != new:
            self.zone_runtime[ctx.subentry_id] = new
            self._notify(ctx.subentry_id)

    @callback
    def _notify(self, sid: str) -> None:
        async_dispatcher_send(
            self.hass, SIGNAL_ZONE_UPDATE.format(subentry_id=sid)
        )

    # --- Custom detection (spec §5) -----------------------------------------
    async def _async_maybe_custom(self, sid: str, reported: float) -> None:
        """Run the shadow-state algorithm for an underlying change (spec §5)."""
        ms = self.manual.get(sid)
        if ms is None:
            return
        now = dt_util.utcnow()
        ha_just = (
            ms.last_command_at is not None
            and (now - ms.last_command_at).total_seconds()
            < OWN_COMMAND_DEBOUNCE_SECONDS
        )
        presence_recent = (
            ms.presence_change_at is not None
            and (now - ms.presence_change_at).total_seconds()
            < PRESENCE_DEBOUNCE_SECONDS
        )
        decision = detect_custom_change(
            OverrideInputs(
                reported_setpoint=reported,
                last_commanded_setpoint=ms.last_commanded_setpoint,
                ha_just_commanded=ha_just,
                presence_change_recent=presence_recent,
                # switch.<zone>_schedule wiring is F4/F5 (spec §5 step 3).
                is_expected_schedule_fallback=False,
            )
        )
        if not decision.is_manual_custom:
            return
        # Enter Custom: hvac_mode=heat, preset=none, hold the reported value,
        # start/refresh the respect-window (spec §5 step 4). The device already
        # holds the value, so mark it applied to avoid an echo write.
        ms.hvac_mode = HVAC_HEAT
        ms.preset = None
        ms.custom_setpoint = decision.custom_setpoint
        ms.last_commanded_setpoint = decision.custom_setpoint
        ms.last_command_at = now
        self._applied[sid] = _Applied(
            off=False, setpoint=decision.custom_setpoint
        )
        self._start_stand_timer(sid)
        await self.async_refresh()
        self._notify(sid)

    # --- state reads --------------------------------------------------------
    def _master_mode(self) -> str | None:
        if not self._mode_entity:
            return None
        state = self.hass.states.get(self._mode_entity)
        return state.state if state else None

    def _current_setpoint(self, sid: str) -> float | None:
        """Best-known current setpoint for a zone (runtime, else a target)."""
        runtime = self.zone_runtime.get(sid)
        if runtime is not None and runtime.setpoint is not None:
            return runtime.setpoint
        ctx = self.zones.get(sid)
        if ctx:
            for target in ctx.targets:
                value = self._extract_setpoint(self.hass.states.get(target))
                if value is not None:
                    return value
        return None

    @staticmethod
    def _extract_setpoint(state: State | None) -> float | None:
        if state is None or state.state in _UNREADY:
            return None
        raw = state.attributes.get(ATTR_TEMPERATURE)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _build_inputs(
        self, ctx: ZoneContext, ms: ZoneManualState
    ) -> AutoInputs | None:
        away = self._read_number(ctx.away_number)
        standby = self._read_number(ctx.standby_number)
        active = self._read_number(ctx.active_number)
        if away is None or standby is None or active is None:
            return None
        return AutoInputs(
            # Window-sensor wiring is F5; the engine already supports it.
            window_open=False,
            home_present=self._read_binary(self._home_present_entity),
            present=self._read_binary(ctx.present_entity),
            away_temp=away,
            standby_temp=standby,
            active_temp=active,
            active_schedule=self._read_schedule(ctx.active_schedule),
            standby_schedule=self._read_schedule(ctx.standby_schedule),
            preset=ms.preset,
            custom_setpoint=ms.custom_setpoint,
            boost_temp=self._read_number(self._boost_temp_entity),
        )

    def _read_number(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _UNREADY:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_binary(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_ON

    def _read_schedule(self, entity_id: str | None) -> ScheduleState | None:
        """Return the schedule's current on-block temp, or None if unconfigured.

        The HA ``schedule`` helper merges the active block's custom ``data`` into
        its state attributes while ``on`` (verified against the schedule
        component); a gap (``off``) carries no block temperature.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _UNREADY:
            # Referenced but unavailable: treat as "no block now".
            return ScheduleState(block_temp=None)
        if state.state == STATE_ON:
            raw = state.attributes.get(SCHEDULE_TEMPERATURE_ATTR)
            try:
                block = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                block = None
            return ScheduleState(block_temp=block)
        return ScheduleState(block_temp=None)
