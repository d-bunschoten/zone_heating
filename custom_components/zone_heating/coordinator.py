"""Zone Heating coordinator (HA glue, spec §3, §6).

Reads live HA state for each zone, builds the pure-engine inputs, runs the
state machine (:mod:`engine.state_machine`) and applies the decision through a
:class:`~.backend.HeatingBackend` — but only when the desired setpoint/stand
changes (spec §3: "een stand-wissel herschrijft alleen als het doel-setpoint
wijzigt").

Triggering is event-driven (state listeners on the source entities) **plus** a
periodic tick for schedule-block boundary crossings (spec §3). Only the ``auto``
master mode is wired here; ``fallback``/``off`` are F4 and are an explicit guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.const import (
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .backend import HeatingBackend
from .const import (
    CONF_ACTIVE_SCHEDULE,
    CONF_STANDBY_SCHEDULE,
    CONF_TARGETS,
    DOMAIN,
    KEY_ACTIVE_TEMP,
    KEY_AWAY_TEMP,
    KEY_CLIMATE,
    KEY_HEATING_MODE,
    KEY_HOME_PRESENT,
    KEY_PRESENT,
    KEY_STANDBY_TEMP,
    LOGGER,
    MODE_AUTO,
    SCHEDULE_TEMPERATURE_ATTR,
    SIGNAL_ZONE_UPDATE,
    TICK_INTERVAL_MINUTES,
)
from .engine.state_machine import (
    HVAC_AUTO,
    AutoInputs,
    ScheduleState,
    ZoneDecision,
    decide,
)
from .helpers import iter_zone_subentries, zone_name

_UNREADY = (None, STATE_UNKNOWN, STATE_UNAVAILABLE)


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
    """Drives every zone's auto state machine and applies it via the backend."""

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
        self._applied: dict[str, _Applied] = {}
        self._mode_entity: str | None = None
        self._home_present_entity: str | None = None

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

        tracked: set[str] = set()
        if self._mode_entity:
            tracked.add(self._mode_entity)
        if self._home_present_entity:
            tracked.add(self._home_present_entity)

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

        if tracked:
            self.entry.async_on_unload(
                async_track_state_change_event(
                    self.hass, sorted(tracked), self._handle_source_change
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

        await self.async_refresh()

    # --- triggers -----------------------------------------------------------
    @callback
    def _handle_source_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        self.entry.async_create_task(
            self.hass, self.async_refresh(), eager_start=False
        )

    @callback
    def _handle_tick(self, _now) -> None:
        self.entry.async_create_task(
            self.hass, self.async_refresh(), eager_start=False
        )

    def async_request_refresh(self) -> None:
        """Schedule a re-evaluation (e.g. after a climate interaction)."""
        self.entry.async_create_task(
            self.hass, self.async_refresh(), eager_start=False
        )

    # --- evaluation ---------------------------------------------------------
    async def async_refresh(self) -> None:
        """Re-evaluate every zone."""
        if self._master_mode() != MODE_AUTO:
            # fallback/off master modes are F4; stay hands-off in auto-only F2.
            return
        for ctx in self.zones.values():
            await self._async_evaluate_zone(ctx)

    async def _async_evaluate_zone(self, ctx: ZoneContext) -> None:
        hvac_mode = self._zone_hvac_mode(ctx)
        if hvac_mode != HVAC_AUTO:
            # heat/off are manual (F3); the engine would raise for them.
            return
        inp = self._build_inputs(ctx)
        if inp is None:
            LOGGER.debug("Zone %s not ready for evaluation", ctx.name)
            return
        decision = decide(hvac_mode, inp)
        await self._apply(ctx, decision)
        self._update_runtime(ctx, decision, inp)

    async def _apply(self, ctx: ZoneContext, decision: ZoneDecision) -> None:
        """Send to the backend only when the setpoint/off state changes."""
        want = _Applied(off=decision.off, setpoint=decision.setpoint)
        if self._applied.get(ctx.subentry_id) == want:
            return
        if decision.off:
            await self.backend.set_off(ctx.targets)
        else:
            assert decision.setpoint is not None
            await self.backend.set_setpoint(ctx.targets, decision.setpoint)
        self._applied[ctx.subentry_id] = want

    def _update_runtime(
        self, ctx: ZoneContext, decision: ZoneDecision, inp: AutoInputs
    ) -> None:
        new = ZoneRuntimeState(
            zone_state=decision.stand,
            setpoint=decision.setpoint,
            preset_mode=decision.preset_mode,
            present=inp.present,
            window_open=inp.window_open,
        )
        if self.zone_runtime.get(ctx.subentry_id) != new:
            self.zone_runtime[ctx.subentry_id] = new
            async_dispatcher_send(
                self.hass,
                SIGNAL_ZONE_UPDATE.format(subentry_id=ctx.subentry_id),
            )

    # --- state reads --------------------------------------------------------
    def _master_mode(self) -> str | None:
        if not self._mode_entity:
            return None
        state = self.hass.states.get(self._mode_entity)
        return state.state if state else None

    def _zone_hvac_mode(self, ctx: ZoneContext) -> str | None:
        if not ctx.climate_entity:
            return None
        state = self.hass.states.get(ctx.climate_entity)
        return state.state if state else None

    def _build_inputs(self, ctx: ZoneContext) -> AutoInputs | None:
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
