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

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_ON,
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
    split_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .backend import HeatingBackend, OverlayTermination
from .const import (
    CONF_ACTIVE_SCHEDULE,
    CONF_ACTIVE_SCHEDULE_GATE,
    CONF_DEAD_MANS_SWITCH,
    CONF_OFF_BASELINE_TEMP,
    CONF_OVERLAY_TERMINATION,
    CONF_OVERLAY_TIMER_DURATION,
    CONF_SCHEDULE_SWITCH,
    CONF_STANDBY_SCHEDULE,
    CONF_TARGETS,
    CONTROL_SOURCE_HA_OVERLAY,
    CONTROL_SOURCE_TADO_SCHEDULE,
    DEFAULT_ACTIVE_SCHEDULE_GATE,
    DEFAULT_BOOST_DURATION,
    DEFAULT_BOOST_TEMP,
    DEFAULT_DEAD_MANS_SWITCH,
    DEFAULT_OFF_BASELINE_TEMP,
    DEFAULT_OVERLAY_TERMINATION,
    DEFAULT_OVERLAY_TIMER_DURATION,
    DEFAULT_OVERRIDE_TIMEOUT,
    DOMAIN,
    HEARTBEAT_INTERVAL_MINUTES,
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
    KEY_WINDOW_OPEN,
    LOGGER,
    MODE_AUTO,
    MODE_FALLBACK,
    MODE_OFF,
    OVERLAY_NEXT_BLOCK,
    OVERLAY_TIMER,
    OWN_COMMAND_DEBOUNCE_SECONDS,
    PRESENCE_DEBOUNCE_SECONDS,
    SCHEDULE_SWITCH_SUFFIX,
    SCHEDULE_TEMPERATURE_ATTR,
    SIGNAL_GLOBAL_UPDATE,
    SIGNAL_ZONE_UPDATE,
    STORAGE_KEY_BASELINES,
    STORAGE_VERSION,
    SWITCH_DOMAIN,
    TICK_INTERVAL_MINUTES,
)
from .engine.baseline import (
    BaselinePlan,
    ScheduleBlock as BaselineBlock,
    derive_normal_baseline_week,
    has_next_block,
    off_baseline,
)
from .engine.override import OverrideInputs, detect_custom_change
from .engine.state_machine import (
    HVAC_AUTO,
    HVAC_HEAT,
    HVAC_OFF,
    PRESET_AWAY,
    PRESET_BOOST,
    STAND_FALLBACK,
    STAND_OFF,
    STAND_WINDOW_OPEN,
    AutoInputs,
    ScheduleState,
    ZoneDecision,
    decide,
)
from .helpers import iter_zone_subentries, zone_name

# HA core ``schedule`` helper: the full weekly config is only available via the
# ``get_schedule`` service (the entity state exposes just the current block).
SCHEDULE_DOMAIN = "schedule"
SCHEDULE_GET_SERVICE = "get_schedule"
# HA schedule weekday keys, in canonical order; the derived baseline keeps the
# week's own shape (one_day when all days match, else seven_day; spec §8).
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_UNREADY = (None, STATE_UNKNOWN, STATE_UNAVAILABLE)
_SETPOINT_TOLERANCE = 0.05


def _entries_to_blocks(entries: object) -> list[BaselineBlock]:
    """Convert ``schedule.get_schedule`` day entries to baseline blocks (spec §8).

    Each entry is ``{"from": <time>, "to": <time>, "data": {"temperature": T}}``;
    ``from``/``to`` may be ``datetime.time`` objects or strings. Entries without a
    temperature become ``None`` (treated as a base-filled gap by the engine).
    """
    blocks: list[BaselineBlock] = []
    if not isinstance(entries, list):
        return blocks
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        start = entry.get("from", entry.get("start"))
        end = entry.get("to", entry.get("end"))
        if start is None or end is None:
            continue
        data = entry.get("data")
        temperature = data.get("temperature") if isinstance(data, dict) else None
        try:
            temp = float(temperature) if temperature is not None else None
        except (TypeError, ValueError):
            temp = None
        blocks.append(
            BaselineBlock(start=str(start), end=str(end), temperature=temp)
        )
    return blocks


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
    boost_number: str | None = None
    # Referenced schedule helper entity_ids (``None`` => no schedule, spec §1.1).
    standby_schedule: str | None = None
    active_schedule: str | None = None
    # Per-zone Active-schedule gate (spec §3). Only relevant with an
    # ``active_schedule``; ``True`` (default) keeps the auto time-gate, ``False``
    # lets Active run block-or-base like Standby, no time-gate.
    active_gate: bool = DEFAULT_ACTIVE_SCHEDULE_GATE
    # Off/frost-protection baseline temperature (spec §8).
    off_baseline_temp: float = DEFAULT_OFF_BASELINE_TEMP
    # ``switch.<zone>_schedule`` (spec §5): ``on`` = zone follows Tado schedule,
    # ``off`` = HA holds an overlay. Drives ``control_source`` / ``in_fallback``.
    schedule_switch: str | None = None
    # ``binary_sensor.<zone>_window_open`` (debounced window mirror, spec §3).
    window_binary: str | None = None


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


@dataclass
class _RestoredManual:
    """A zone's manual state recovered from RestoreEntity on restart (spec §9)."""

    hvac_mode: str
    preset: str | None
    custom_setpoint: float | None
    remaining_min: int | None


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
        # Live ``control_source`` per zone (spec §5), surfaced on the sensor + climate.
        self._control_source: dict[str, str | None] = {}
        # Cached global ``in_fallback`` so we only push on a real change (spec §9).
        self._in_fallback: bool | None = None
        # Manual state recovered from RestoreEntity, consumed in ``async_setup`` (spec §9).
        self._restored: dict[str, _RestoredManual] = {}
        # Per-zone signature of the last-written baseline, loaded from the Store so a
        # reload only rewrites a baseline that actually changed (spec §8 dedupe).
        self._store: Store | None = None
        self._baseline_sigs: dict[str, str] = {}
        # Per-zone: does the active baseline have a schedule transition ("next
        # block")? A flat one-block baseline (every off/frost baseline, and any
        # standby schedule with no on-blocks) has none, so a NEXT_TIME_BLOCK overlay
        # termination would be rejected by Tado (HTTP 422) — such a zone gets a timer
        # termination instead (spec §7). Set on every baseline write; missing is
        # treated as flat (timer is valid everywhere, NEXT_TIME_BLOCK is not).
        self._baseline_has_next: dict[str, bool] = {}
        # target / switch entity_id -> zone subentry_ids that use it.
        self._switch_to_zone: dict[str, list[str]] = {}
        self._mode_entity: str | None = None
        # Last applied master mode (spec §2). Set in ``async_setup``; transitions
        # are driven by ``_async_handle_mode_change``.
        self._mode: str = MODE_AUTO
        # Dead-man's-switch heartbeat canceller (spec §7); only armed in auto.
        self._heartbeat_cancel: Callable[[], None] | None = None
        self._home_present_entity: str | None = None
        # Previous home-presence value, to detect the present -> away *edge* (spec
        # §3): a heat stand ends only when the house *becomes* Away, not on every
        # evaluation while already away (so a manual override set while away sticks).
        self._prev_home_present: bool | None = None
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
        # Load the persisted baseline signatures (spec §8 dedupe). Survives reloads
        # and restarts via ``.storage`` so an unchanged baseline is not rewritten.
        self._store = Store(
            self.hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_BASELINES}_{self.entry.entry_id}",
        )
        self._baseline_sigs = await self._store.async_load() or {}
        self._mode_entity = registry.async_get_entity_id(
            Platform.SELECT, DOMAIN, KEY_HEATING_MODE
        )
        self._home_present_entity = registry.async_get_entity_id(
            Platform.BINARY_SENSOR, DOMAIN, KEY_HOME_PRESENT
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
                boost_number=registry.async_get_entity_id(
                    Platform.NUMBER, DOMAIN, f"{sid}_{KEY_BOOST_TEMP}"
                ),
                standby_schedule=subentry.data.get(CONF_STANDBY_SCHEDULE),
                active_schedule=subentry.data.get(CONF_ACTIVE_SCHEDULE),
                active_gate=subentry.data.get(
                    CONF_ACTIVE_SCHEDULE_GATE, DEFAULT_ACTIVE_SCHEDULE_GATE
                ),
                off_baseline_temp=float(
                    subentry.data.get(
                        CONF_OFF_BASELINE_TEMP, DEFAULT_OFF_BASELINE_TEMP
                    )
                ),
                schedule_switch=self._resolve_schedule_switch(subentry),
                window_binary=registry.async_get_entity_id(
                    Platform.BINARY_SENSOR, DOMAIN, f"{sid}_{KEY_WINDOW_OPEN}"
                ),
            )
            self.zones[sid] = ctx
            self.zone_runtime.setdefault(sid, ZoneRuntimeState())
            # Restore the zone's manual state (hvac_mode/preset/Custom) from the
            # climate entity's RestoreEntity data, or a fresh default (spec §9).
            self.manual.setdefault(sid, self._initial_manual_state(sid))

            for entity_id in (
                ctx.present_entity,
                ctx.away_number,
                ctx.standby_number,
                ctx.active_number,
                ctx.boost_number,
                ctx.standby_schedule,
                ctx.active_schedule,
                ctx.window_binary,
            ):
                if entity_id:
                    tracked.add(entity_id)

            if ctx.schedule_switch:
                tracked.add(ctx.schedule_switch)
                self._switch_to_zone.setdefault(
                    ctx.schedule_switch, []
                ).append(sid)

            for target in ctx.targets:
                targets.add(target)
                self._target_to_zone.setdefault(target, []).append(sid)

        # Group each zone's configured schedule helpers under its device (spec §1.1).
        self._link_zone_schedule_helpers(registry)

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
        # Cancel any running timer / heartbeat on unload.
        self.entry.async_on_unload(self._cancel_all_timers)
        self.entry.async_on_unload(self._cancel_heartbeat)

        # Re-arm restored respect-window / boost timers (spec §9). A heat stand with
        # a recoverable remaining time keeps its timer; a sticky ``away`` keeps no
        # timer; anything else whose timer could not be restored drops to ``auto``.
        self._rearm_restored_timers()

        # Apply the current master mode: first setup writes the (derived normal or
        # off) baseline per zone and either starts the state machine (auto),
        # observes (fallback) or turns the zones off (off). (spec §2, §8).
        self._mode = self._master_mode() or MODE_AUTO
        await self._apply_master_mode(previous=None)

    @callback
    def _cancel_all_timers(self) -> None:
        for sid in list(self.manual):
            self._cancel_timer(sid)

    def _resolve_schedule_switch(self, subentry) -> str | None:
        """Resolve a zone's ``switch.<zone>_schedule`` (spec §5).

        Uses the explicit config override if set, else derives it from the zone's
        first target climate (``switch.<object_id>_schedule``, tado_hijack's naming).
        Returns ``None`` when nothing is resolvable -> ``control_source`` degrades to
        unknown.
        """
        explicit = subentry.data.get(CONF_SCHEDULE_SWITCH)
        if explicit:
            return explicit
        targets = subentry.data.get(CONF_TARGETS) or []
        if not targets:
            return None
        object_id = split_entity_id(targets[0])[1]
        return f"{SWITCH_DOMAIN}.{object_id}{SCHEDULE_SWITCH_SUFFIX}"

    def _link_zone_schedule_helpers(self, registry: er.EntityRegistry) -> None:
        """Group each zone's configured schedule helpers under the zone device.

        The referenced HA ``schedule`` helpers are UI helpers with
        ``device_id=None``, so without this they appear nowhere near the zone's
        device. We attach **only** the schedule helpers (never the window sensor
        or presence source) to the zone's device so they show up in its entity
        list (spec §1.1), and we detach any schedule helper that is still on the
        device but no longer configured, so it disappears again when unlinked.

        Idempotent (only writes when ``device_id`` differs), robust (a missing
        device/entity is skipped) and fully defensive: it must never let setup
        fail, so any unexpected error is logged and swallowed.
        """
        try:
            device_reg = dr.async_get(self.hass)
            for sid, ctx in self.zones.items():
                # ``async_get_device(identifiers=...)`` is deprecated (identifiers
                # are no longer globally unique); the per-identifier lookup is the
                # supported replacement. Our zone identifier ``(DOMAIN, sid)`` comes
                # from ``helpers.zone_device_info`` and is unique within this entry.
                device = device_reg.async_get_device_by_identifier(
                    (DOMAIN, sid), self.entry.entry_id
                )
                if device is None:
                    continue
                configured = {
                    entity_id
                    for entity_id in (ctx.standby_schedule, ctx.active_schedule)
                    if entity_id
                }
                # Attach every configured schedule helper that exists in the
                # registry (idempotent: only when the device_id actually differs).
                for entity_id in configured:
                    entry = registry.async_get(entity_id)
                    if entry is None:
                        continue  # not (yet) a registered entity -> skip
                    if entry.device_id != device.id:
                        registry.async_update_entity(
                            entity_id, device_id=device.id
                        )
                # Detach schedule helpers still linked to this device but dropped
                # from the config, so unlinking a helper removes it from the device.
                for entry in er.async_entries_for_device(
                    registry, device.id, include_disabled_entities=True
                ):
                    if (
                        entry.platform == SCHEDULE_DOMAIN
                        and entry.entity_id not in configured
                    ):
                        registry.async_update_entity(
                            entry.entity_id, device_id=None
                        )
        except Exception as err:  # noqa: BLE001 - must never break setup
            LOGGER.warning(
                "Kon schedule-helpers niet aan het zone-device koppelen: %s",
                err,
            )

    def _initial_manual_state(self, sid: str) -> ZoneManualState:
        """Build a zone's initial manual state, honouring a restore (spec §9)."""
        restored = self._restored.get(sid)
        if restored is None:
            return ZoneManualState()
        return ZoneManualState(
            hvac_mode=restored.hvac_mode,
            preset=restored.preset,
            custom_setpoint=restored.custom_setpoint,
        )

    def _rearm_restored_timers(self) -> None:
        """Re-arm respect-window / boost timers for restored heat stands (spec §9)."""
        for sid, ms in self.manual.items():
            if ms.hvac_mode != HVAC_HEAT or ms.preset == PRESET_AWAY:
                continue  # off / auto / sticky-away need no timer
            restored = self._restored.get(sid)
            remaining = restored.remaining_min if restored else None
            if remaining and remaining > 0:
                self._arm_timer(sid, remaining)
            else:
                # Could not restore the timer -> deterministic fall-back to auto.
                self._to_auto_state(sid)

    def restore_zone_manual(
        self,
        sid: str,
        *,
        hvac_mode: str,
        preset: str | None,
        custom_setpoint: float | None,
        remaining_min: int | None,
    ) -> None:
        """Record a zone's RestoreEntity manual state for ``async_setup`` (spec §9).

        Called by the climate entity from ``async_added_to_hass`` (which runs before
        the coordinator's ``async_setup``), so the first evaluation uses the restored
        ``hvac_mode``/preset instead of a cold default.
        """
        self._restored[sid] = _RestoredManual(
            hvac_mode=hvac_mode,
            preset=preset,
            custom_setpoint=custom_setpoint,
            remaining_min=remaining_min,
        )

    # --- triggers -----------------------------------------------------------
    @callback
    def _handle_source_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        entity_id = event.data["entity_id"]
        # Master-mode changes drive the orchestration (auto/fallback/off, spec §2).
        if entity_id == self._mode_entity:
            self.entry.async_create_task(
                self.hass, self._async_handle_mode_change(), eager_start=False
            )
            return
        # A ``switch.<zone>_schedule`` change is a live mirror event: refresh
        # control_source + in_fallback (spec §5, §9). No overlay is re-asserted
        # here — the dead-man's-switch heartbeat retakes control in auto (spec §9).
        switch_zones = self._switch_to_zone.get(entity_id)
        if switch_zones is not None:
            for sid in switch_zones:
                ctx = self.zones.get(sid)
                if ctx is not None:
                    self._refresh_control_source(ctx)
            self._refresh_in_fallback()
            return
        # Stamp presence-change timestamps so the shadow-state can ignore
        # presence-driven underlying changes (spec §5 step 2).
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
        if self._mode != MODE_AUTO:
            # The master switch is the top-level control: in Off/Fallback the
            # per-zone machine is paused (spec §2), so per-zone changes are ignored
            # (they would not apply and would leave a stale "heat at off-temp").
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
        if self._mode != MODE_AUTO:
            return  # master Off/Fallback: per-zone machine paused (spec §2)
        ms.hvac_mode = HVAC_HEAT
        ms.preset = preset
        ms.custom_setpoint = None
        self._start_stand_timer(sid)
        await self.async_refresh()

    async def async_set_zone_custom(self, sid: str, temperature: float) -> None:
        """Handle ``climate.set_temperature`` -> Custom on ``T`` (spec §4).

        A manually ``off`` zone turns back on to Custom when dialed. But while the
        window is open the zone is force-off by the safety modifier (spec §3); a
        dialed setpoint must not fight it, so it is ignored.
        """
        ms = self.manual.get(sid)
        if ms is None:
            return
        if self._mode != MODE_AUTO:
            return  # master Off/Fallback: per-zone machine paused (spec §2)
        ctx = self.zones.get(sid)
        if ctx is not None and self._read_binary(ctx.window_binary):
            return
        ms.hvac_mode = HVAC_HEAT
        ms.preset = None
        ms.custom_setpoint = temperature
        self._start_stand_timer(sid)
        await self.async_refresh()

    def zone_hvac_mode(self, sid: str) -> str:
        """Displayed hvac_mode; an open window shows ``off`` (spec §3).

        The user's chosen mode stays stored in ``manual`` so it resumes when the
        window closes; only the surfaced value flips to ``off`` while open.
        """
        runtime = self.zone_runtime.get(sid)
        if runtime is not None and runtime.window_open:
            return HVAC_OFF
        return self.zone_stored_hvac_mode(sid)

    def zone_stored_hvac_mode(self, sid: str) -> str:
        """The zone's stored hvac_mode, ignoring the window-open mask (spec §9)."""
        ms = self.manual.get(sid)
        return ms.hvac_mode if ms else HVAC_AUTO

    def zone_manual_preset(self, sid: str) -> str | None:
        """The forced preset in heat (None => Custom)."""
        ms = self.manual.get(sid)
        return ms.preset if ms else None

    def zone_custom_setpoint(self, sid: str) -> float | None:
        """The Custom (manually dialed) setpoint, if any (spec §9 restore)."""
        ms = self.manual.get(sid)
        return ms.custom_setpoint if ms else None

    def zone_control_source(self, sid: str) -> str | None:
        """Live ``control_source`` for a zone (ha_overlay/tado_schedule/unknown)."""
        return self._control_source.get(sid)

    def in_fallback(self) -> bool:
        """Global ``heating_in_fallback`` (spec §1.2, §9).

        True when the master mode is ``fallback`` (zones intentionally on the Tado
        schedule), or when — in ``auto`` — at least one zone is actually on its Tado
        schedule while HA expects to hold an overlay (an unexpected fallback).
        """
        if self._mode == MODE_FALLBACK:
            return True
        if self._mode != MODE_AUTO:
            return False
        for ctx in self.zones.values():
            if self._schedule_switch_state(ctx) == STATE_ON:
                return True
        return False

    def zone_override_active(self, sid: str) -> bool:
        """True while a manual heat stand is in effect (spec §4)."""
        ms = self.manual.get(sid)
        return bool(ms and ms.hvac_mode == HVAC_HEAT)

    def zone_off_temp(self, sid: str) -> float | None:
        """The frost-protection / off-baseline temp the zone holds while off (§8)."""
        ctx = self.zones.get(sid)
        return ctx.off_baseline_temp if ctx else None

    def zone_fallback_temp(self, sid: str) -> float | None:
        """The live normal-baseline (standby) temp the zone runs on in Fallback (§2.1).

        In master Fallback the zone follows the Tado normal-baseline (the standby
        curve), so the climate entity shows that temp instead of a stale setpoint.
        Same "block-or-base" logic as Standby: the current standby-schedule block
        temperature when there is a block now, otherwise the standby base temp
        (``number.<zone>_standby_temp``). ``None`` when nothing is resolvable.
        """
        ctx = self.zones.get(sid)
        if ctx is None:
            return None
        schedule = self._read_schedule(ctx.standby_schedule)
        if schedule is not None and schedule.block_temp is not None:
            return schedule.block_temp
        return self._read_number(ctx.standby_number)

    def zone_override_remaining_min(self, sid: str) -> int | None:
        """Minutes left on the respect-window / boost timer (None if sticky)."""
        ms = self.manual.get(sid)
        if ms is None or ms.timer_end is None:
            return None
        remaining = (ms.timer_end - dt_util.utcnow()).total_seconds()
        if remaining <= 0:
            return 0
        return int((remaining + 59) // 60)

    def zone_override_end(self, sid: str) -> datetime | None:
        """End of the respect-window / boost as a timestamp (spec §1.1).

        ``None`` when no timer is running — i.e. in ``auto``, a sticky ``away``
        preset or a sticky ``off`` zone; the timestamp sensor then shows
        ``unknown`` (no countdown).
        """
        ms = self.manual.get(sid)
        return ms.timer_end if ms else None

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
        # The respect-window / boost end changed -> refresh the timestamp sensor
        # (spec §1.1 ``sensor.<zone>_override_ends``). No other behaviour changes.
        self._notify(sid)

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
        had_timer = ms.timer_end is not None
        if ms.timer_cancel is not None:
            ms.timer_cancel()
        ms.timer_cancel = None
        ms.timer_end = None
        # Clearing a running timer changes the override-end -> refresh the
        # timestamp sensor (spec §1.1). Skip the notify when there was nothing to
        # clear so a routine cancel does not churn state.
        if had_timer:
            self._notify(sid)

    def _to_auto_state(self, sid: str) -> None:
        """Drop any manual stand and return the zone to auto (spec §3)."""
        self._cancel_timer(sid)
        ms = self.manual[sid]
        ms.hvac_mode = HVAC_AUTO
        ms.preset = None
        ms.custom_setpoint = None

    # --- evaluation ---------------------------------------------------------
    async def async_refresh(self) -> None:
        """Re-evaluate every zone (only drives devices in master mode auto)."""
        if self._mode != MODE_AUTO:
            # In fallback/off the state machine is paused (spec §2): HA observes
            # (fallback) or holds the zones off (off); no per-zone overlays here.
            return
        # Detect the present -> away edge once per cycle (spec §3): only on this
        # edge do non-sticky heat stands end. A new manual override set while the
        # house is already away is therefore kept.
        home_present = self._read_binary(self._home_present_entity)
        became_away = self._prev_home_present is True and not home_present
        self._prev_home_present = home_present
        for ctx in self.zones.values():
            await self._async_evaluate_zone(ctx, became_away=became_away)

    # --- master-mode orchestration (spec §2, §8) ----------------------------
    async def _async_handle_mode_change(self) -> None:
        """React to a ``select.heating_mode`` change (auto/fallback/off)."""
        new_mode = self._master_mode() or MODE_AUTO
        if new_mode == self._mode:
            return
        previous = self._mode
        self._mode = new_mode
        await self._apply_master_mode(previous=previous)

    async def _apply_master_mode(self, *, previous: str | None) -> None:
        """Enact the current master mode and do the gated baseline writes (spec §8).

        A Tado schedule is written ONLY at first setup (``previous is None``), when
        entering Off (off-baseline) and when leaving Off to auto/fallback (derived
        normal baseline). Auto <-> fallback writes nothing. Config changes re-run
        setup via an entry reload, which takes the first-setup path again.
        """
        mode = self._mode
        # Leaving Off (or first setup) into auto/fallback restores the derived
        # normal baseline; first setup into Off / entering Off writes the off one.
        # (``previous is None`` is first setup; it is never ``MODE_OFF``.)
        from_off_or_initial = previous is None or previous == MODE_OFF
        # A live master switch (not first setup/reload) is the top-level control:
        # -> Off puts every zone in off; -> Auto/Fallback returns every zone to auto
        # (spec §2/§4). On setup (previous is None) the restored per-zone modes are
        # kept instead, so a restart does not wipe a zone's manual stand.
        reset_zones = previous is not None
        if mode == MODE_OFF:
            await self._enter_off(write_baseline=previous != MODE_OFF)
        elif mode == MODE_FALLBACK:
            await self._enter_fallback(
                write_baseline=from_off_or_initial, reset_zones=reset_zones
            )
        else:
            await self._enter_auto(
                write_baseline=from_off_or_initial, reset_zones=reset_zones
            )
        self._manage_heartbeat()
        # The mode itself changes the live mirror (Fallback/Off pin tado_schedule,
        # spec §5) and the fallback flag; refresh both after enacting it.
        for ctx in self.zones.values():
            self._refresh_control_source(ctx)
        self._refresh_in_fallback()

    async def _enter_auto(
        self, *, write_baseline: bool, reset_zones: bool = False
    ) -> None:
        """Master mode auto: (re)write the normal baseline, run the machine.

        ``reset_zones`` (a live switch into Auto) returns every zone to ``auto``,
        dropping any per-zone manual stand — the master switch is the top-level
        control (spec §2/§4). On setup/reload it is False so restored modes stay.
        """
        if reset_zones:
            for sid in self.manual:
                self._to_auto_state(sid)
        if write_baseline:
            for ctx in self.zones.values():
                await self._async_write_normal_baseline(ctx)
        await self.async_refresh()

    async def _enter_fallback(
        self, *, write_baseline: bool, reset_zones: bool = False
    ) -> None:
        """Master mode fallback: stop asserting overlays, just observe (spec §2).

        ``reset_zones`` (a live switch into Fallback) returns every zone to ``auto``
        so none stays stuck in a prior ``off``/manual stand while HA observes, and
        resumes every zone **directly** onto its Tado schedule (spec §2.1).
        """
        if reset_zones:
            for sid in self.manual:
                self._to_auto_state(sid)
        if write_baseline:
            for ctx in self.zones.values():
                await self._async_write_normal_baseline(ctx)
        # Drop the applied-command cache so returning to auto re-asserts overlays
        # (the zone may have drifted onto its Tado schedule while in fallback).
        for ctx in self.zones.values():
            # On a deliberate switch into Fallback (the reset_zones path, i.e.
            # ``previous is not None``) resume every zone with a configured
            # schedule switch directly onto its Tado schedule (spec §2.1): the
            # overlay would otherwise only lapse on its ``next_block`` termination
            # (the heartbeat is off in fallback), so the fall-back would be slow.
            # On first setup/reload there are no overlays yet, so skip it.
            if reset_zones and ctx.schedule_switch:
                await self._resume_zone_schedule(ctx)
            self._applied.pop(ctx.subentry_id, None)
            self._set_runtime_stand(ctx, STAND_FALLBACK)

    async def _resume_zone_schedule(self, ctx: ZoneContext) -> None:
        """Resume a zone onto its Tado schedule via ``switch.<zone>_schedule`` (spec §2.1).

        tado_hijack has no per-zone resume service (``resume_all_schedules`` is too
        broad), but turning the zone's schedule switch **on** lifts the HA overlay
        and puts the zone back on the Tado schedule — this works for both tado_hijack
        and the ``fake_tado`` test harness. Fully defensive: a failing/missing switch
        is logged and skipped, never propagated, so a flaky switch can never make the
        fallback entry itself fail (the overlay then simply lapses naturally).
        """
        try:
            await self.hass.services.async_call(
                SWITCH_DOMAIN,
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: ctx.schedule_switch},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - must never break fallback entry
            LOGGER.warning(
                "Zone %s: directe terugval naar het Tado-schema mislukt "
                "(switch.turn_on op %s): %s; overlay vervalt vanzelf",
                ctx.name,
                ctx.schedule_switch,
                err,
            )

    async def _enter_off(self, *, write_baseline: bool) -> None:
        """Master mode off: write the off-baseline, set the zones off (spec §2)."""
        for ctx in self.zones.values():
            sid = ctx.subentry_id
            # The master switch is the top-level control: Off puts the zone in off
            # (spec §2/§4), so the zone climate shows ``off`` and any manual stand /
            # respect-window is dropped. It returns to auto when the master does.
            self._cancel_timer(sid)
            ms = self.manual[sid]
            ms.hvac_mode = HVAC_OFF
            ms.preset = None
            ms.custom_setpoint = None
            if write_baseline:
                await self._async_write_off_baseline(ctx)
            ok = True
            if ctx.targets:
                # The off baseline is a flat frost block, so this resolves to a timer
                # termination (NEXT_TIME_BLOCK would 422 on it); spec §7.
                termination = self._overlay_termination(ctx)
                # Resilient like the auto path (F5.2 §A): a failed set_off at
                # master=off is logged + skipped, never propagated, so Off still
                # loads. On failure leave the applied-cache untouched so a later
                # attempt retries; the surfaced stand still reflects Off (spec §8).
                ok = await self._safe_backend_write(
                    self.backend.set_off(ctx.targets, termination),
                    ctx=ctx,
                    action="set_off",
                )
            if ok:
                self._applied[ctx.subentry_id] = _Applied(
                    off=True, setpoint=None
                )
            self._set_runtime_stand(ctx, STAND_OFF)

    async def _async_evaluate_zone(
        self, ctx: ZoneContext, *, became_away: bool = False
    ) -> None:
        sid = ctx.subentry_id
        ms = self.manual[sid]
        home_present = self._read_binary(self._home_present_entity)

        # Non-sticky heat stands end only when the house *becomes* Away (the
        # present -> away edge, spec §3); the ``away`` preset is sticky and
        # survives it. While already away a new manual override/Custom (and off)
        # is allowed and kept until its respect-window or a manual change.
        if (
            became_away
            and ms.hvac_mode == HVAC_HEAT
            and ms.preset != PRESET_AWAY
        ):
            self._to_auto_state(sid)

        present = self._read_binary(ctx.present_entity)
        window_open = self._read_binary(ctx.window_binary)

        # Window-open is the safety modifier on top (spec §3): the zone goes off
        # and shows ``Window-open`` regardless of hvac_mode. The user's chosen mode
        # stays stored in ``ms`` and resumes when the window closes (after its
        # close-delay debounce, handled by ``binary_sensor.<zone>_window_open``).
        if window_open:
            decision = ZoneDecision(
                stand=STAND_WINDOW_OPEN,
                setpoint=None,
                preset_mode=None,
                off=True,
            )
            await self._apply(ctx, decision)
            self._update_runtime(
                ctx, decision, present=present, window_open=True
            )
            self._refresh_control_source(ctx)
            return

        hvac = ms.hvac_mode
        if hvac == HVAC_OFF:
            decision = ZoneDecision(
                stand=STAND_OFF, setpoint=None, preset_mode=None, off=True
            )
            await self._apply(ctx, decision)
            self._update_runtime(
                ctx, decision, present=present, window_open=False
            )
            self._refresh_control_source(ctx)
            return

        inp = self._build_inputs(ctx, ms, present=present, window_open=window_open)
        if inp is None:
            LOGGER.debug("Zone %s not ready for evaluation", ctx.name)
            return
        decision = decide(hvac, inp)
        await self._apply(ctx, decision)
        self._update_runtime(
            ctx, decision, present=inp.present, window_open=inp.window_open
        )
        self._refresh_control_source(ctx)

    async def _apply(self, ctx: ZoneContext, decision: ZoneDecision) -> None:
        """Send to the backend only when the setpoint/off state changes."""
        if not decision.off and decision.setpoint is None:
            # Manual stand without a resolvable setpoint (e.g. Custom before a
            # value, or a missing boost_temp): nothing safe to write.
            LOGGER.debug("Zone %s has no setpoint to apply", ctx.name)
            return
        sid = ctx.subentry_id
        want = _Applied(off=decision.off, setpoint=decision.setpoint)
        ms = self.manual[sid]
        if sid not in self._applied and self._device_matches(ctx, want):
            # First application this session (e.g. after a restart): the device
            # already holds the desired state, so adopt it without a redundant
            # write (spec §9: "alleen schrijven als het doel-setpoint afwijkt").
            self._applied[sid] = want
            if not want.off and want.setpoint is not None:
                ms.last_commanded_setpoint = want.setpoint
            return
        if self._applied.get(sid) == want:
            return
        termination = self._overlay_termination(ctx)
        if decision.off:
            ok = await self._safe_backend_write(
                self.backend.set_off(ctx.targets, termination),
                ctx=ctx,
                action="set_off",
            )
        else:
            ok = await self._safe_backend_write(
                self.backend.set_setpoint(
                    ctx.targets, decision.setpoint, termination
                ),
                ctx=ctx,
                action="set_setpoint",
            )
        if not ok:
            # The write failed (e.g. tado_hijack down): leave _applied,
            # last_commanded_setpoint and last_command_at untouched so the next
            # evaluation retries it. The existing overlay/schedule is the vangnet.
            return
        if not decision.off:
            ms.last_commanded_setpoint = decision.setpoint
        ms.last_command_at = dt_util.utcnow()
        self._applied[ctx.subentry_id] = want

    async def _safe_backend_write(
        self, coro, *, ctx: ZoneContext, action: str
    ) -> bool:
        """Await a backend write, swallowing a failing Tado call (spec §8, §9).

        A failed Tado-write (backend service missing/erroring, e.g.
        ``ServiceNotFound: tado_hijack.set_mode``) is **logged as a warning and
        skipped** instead of propagating — so the integration keeps loading and the
        existing Tado schedule/overlay stays the vangnet. Returns ``True`` on
        success, ``False`` when the write failed (caller then skips its bookkeeping
        so a later evaluation retries).
        """
        try:
            await coro
        except HomeAssistantError as err:
            LOGGER.warning(
                "Zone %s: Tado-write (%s) mislukt: %s; overgeslagen, "
                "bestaand schema/overlay blijft het vangnet",
                ctx.name,
                action,
                err,
            )
            return False
        except Exception as err:  # noqa: BLE001 - defensive: a flaky backend must
            # never crash setup/evaluation; log and skip the write.
            LOGGER.warning(
                "Zone %s: onverwachte fout bij Tado-write (%s): %s; overgeslagen, "
                "bestaand schema/overlay blijft het vangnet",
                ctx.name,
                action,
                err,
            )
            return False
        return True

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

    def _set_runtime_stand(self, ctx: ZoneContext, stand: str) -> None:
        """Surface a bare stand (Off/Fallback) on the climate entity (spec §4)."""
        new = ZoneRuntimeState(
            zone_state=stand,
            setpoint=None,
            preset_mode=None,
            present=self._read_binary(ctx.present_entity),
            window_open=False,
        )
        if self.zone_runtime.get(ctx.subentry_id) != new:
            self.zone_runtime[ctx.subentry_id] = new
            self._notify(ctx.subentry_id)

    # --- dead-man's-switch heartbeat (spec §7) ------------------------------
    def _manage_heartbeat(self) -> None:
        """Arm/disarm the heartbeat: only in auto with the dead-man's-switch on."""
        want = (
            self._mode == MODE_AUTO
            and self._dead_mans_switch()
            and bool(self.zones)
        )
        if want and self._heartbeat_cancel is None:
            self._heartbeat_cancel = async_track_time_interval(
                self.hass,
                self._handle_heartbeat,
                timedelta(minutes=HEARTBEAT_INTERVAL_MINUTES),
            )
        elif not want:
            self._cancel_heartbeat()

    @callback
    def _cancel_heartbeat(self) -> None:
        if self._heartbeat_cancel is not None:
            self._heartbeat_cancel()
            self._heartbeat_cancel = None

    @callback
    def _handle_heartbeat(self, _now) -> None:
        self.entry.async_create_task(
            self.hass, self._async_heartbeat(), eager_start=False
        )

    async def _async_heartbeat(self) -> None:
        """Re-assert each zone's active HA overlay so it stays well inside its
        termination window (spec §7). Bypasses the redundancy guard on purpose."""
        if self._mode != MODE_AUTO:
            return
        now = dt_util.utcnow()
        for ctx in self.zones.values():
            applied = self._applied.get(ctx.subentry_id)
            if applied is None or applied.off or applied.setpoint is None:
                continue
            if ctx.targets:
                termination = self._overlay_termination(ctx)
                # A failed heartbeat (e.g. tado_hijack down) is logged + skipped,
                # not raised as a loose exception (spec §7/§8 resilience). The
                # dead-man's-switch still protects: a crashed HA lets the overlay
                # lapse to the Tado baseline regardless.
                ok = await self._safe_backend_write(
                    self.backend.set_setpoint(
                        ctx.targets, applied.setpoint, termination
                    ),
                    ctx=ctx,
                    action="heartbeat",
                )
                # Only refresh the own-command debounce when the write landed, so
                # the device's echo is never read as Custom (spec §5); on failure
                # nothing was written, so leave it.
                if ok:
                    self.manual[ctx.subentry_id].last_command_at = now

    # --- global config reads (spec §7) --------------------------------------
    def _dead_mans_switch(self) -> bool:
        return bool(
            self.entry.options.get(
                CONF_DEAD_MANS_SWITCH, DEFAULT_DEAD_MANS_SWITCH
            )
        )

    def _overlay_termination(
        self, ctx: ZoneContext | None = None
    ) -> OverlayTermination:
        """The overlay termination for a zone's write (spec §7).

        Honours the configured mode, but **falls back to a timer when the zone's
        active baseline is a single flat block**: Tado rejects a ``NEXT_TIME_BLOCK``
        overlay on a zone with no "next schedule block" (HTTP 422), which every
        off/frost baseline and any flat standby baseline is. The timer duration is
        the configured ``overlay_timer_duration`` either way. A zone whose baseline
        shape is not yet known is treated as flat (the timer is valid everywhere,
        ``NEXT_TIME_BLOCK`` is not).
        """
        mode = self.entry.options.get(
            CONF_OVERLAY_TERMINATION, DEFAULT_OVERLAY_TERMINATION
        )
        if (
            mode == OVERLAY_NEXT_BLOCK
            and ctx is not None
            and not self._baseline_has_next.get(ctx.subentry_id, False)
        ):
            mode = OVERLAY_TIMER
        if mode == OVERLAY_TIMER:
            duration = self.entry.options.get(
                CONF_OVERLAY_TIMER_DURATION, DEFAULT_OVERLAY_TIMER_DURATION
            )
            return OverlayTermination(
                mode=OVERLAY_TIMER, duration_min=int(duration)
            )
        return OverlayTermination()

    # --- baseline writes (spec §8) ------------------------------------------
    async def _async_write_normal_baseline(self, ctx: ZoneContext) -> None:
        """Write the derived normal baseline for one zone (spec §8).

        Reads the whole weekly standby schedule and derives the baseline in the
        week's own shape: a ``one_day`` plan when every weekday matches, else a
        ``seven_day`` plan with one write per distinct day-pattern.
        """
        base = self._read_number(ctx.standby_number)
        if base is None:
            LOGGER.debug(
                "Zone %s: standby_temp unavailable, skipping normal baseline",
                ctx.name,
            )
            return
        week = await self._async_read_standby_week(ctx)
        plan = derive_normal_baseline_week(week, base, _WEEKDAYS)
        await self._async_write_baseline_if_changed(ctx, plan)

    async def _async_write_off_baseline(self, ctx: ZoneContext) -> None:
        """Write the flat off/frost-protection baseline for one zone (spec §8)."""
        plan = off_baseline(ctx.off_baseline_temp)
        await self._async_write_baseline_if_changed(ctx, plan)

    @staticmethod
    def _baseline_signature(plan: BaselinePlan) -> str:
        """A stable signature of the whole baseline plan, for the dedupe (spec §8).

        Covers the timetable type and every write (its ``all_days``/weekdays and
        blocks), so a change to any day-pattern rewrites the baseline.
        """
        return json.dumps(
            {
                "t": plan.timetable,
                "w": [
                    {
                        "a": write.all_days,
                        "d": list(write.weekdays),
                        "b": list(write.blocks),
                    }
                    for write in plan.writes
                ],
            },
            sort_keys=True,
            default=str,
        )

    async def _async_write_baseline_if_changed(
        self, ctx: ZoneContext, plan: BaselinePlan
    ) -> None:
        """Write a baseline plan only when it changed from the last one (spec §8).

        The per-zone signature covers the whole plan and is persisted in the Store,
        so a reload/restart with an unchanged baseline makes no needless Tado-cloud
        ``set_schedule`` calls (fixes the F4 note that every reload re-wrote it).
        When it did change, one ``set_schedule`` call is issued per write (one per
        distinct day-pattern; spec §8 budget).
        """
        if not ctx.targets:
            return
        sid = ctx.subentry_id
        # Record the active baseline's shape even when the write is deduped below:
        # the overlay termination depends on it (spec §7).
        self._baseline_has_next[sid] = has_next_block(plan)
        signature = self._baseline_signature(plan)
        if self._baseline_sigs.get(sid) == signature:
            LOGGER.debug(
                "Zone %s baseline unchanged, skipping set_schedule (spec §8)",
                ctx.name,
            )
            return
        for write in plan.writes:
            ok = await self._safe_backend_write(
                self.backend.set_schedule(
                    ctx.targets,
                    plan.timetable,
                    list(write.blocks),
                    all_days=write.all_days,
                    days=None if write.all_days else list(write.weekdays),
                ),
                ctx=ctx,
                action="set_schedule",
            )
            if not ok:
                # Do not store the signature on a failed write, so a next attempt
                # retries the baseline instead of deduping it away (spec §8).
                return
        self._baseline_sigs[sid] = signature
        if self._store is not None:
            await self._store.async_save(self._baseline_sigs)

    async def _async_read_standby_week(
        self, ctx: ZoneContext
    ) -> dict[str, list[BaselineBlock]]:
        """Read the whole weekly standby schedule as per-weekday baseline blocks.

        The full weekly schedule is read via the ``schedule.get_schedule`` service.
        Every weekday is returned (a missing/empty day -> an empty list, which the
        pure derivation fills with a flat base-temp curve). No schedule configured,
        or the helper unavailable -> every day empty (spec §8).
        """
        empty: dict[str, list[BaselineBlock]] = {day: [] for day in _WEEKDAYS}
        if not ctx.standby_schedule:
            return empty
        try:
            response = await self.hass.services.async_call(
                SCHEDULE_DOMAIN,
                SCHEDULE_GET_SERVICE,
                {ATTR_ENTITY_ID: ctx.standby_schedule},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - defensive: never block the write
            LOGGER.warning(
                "Zone %s: could not read standby schedule %s (%s); "
                "using a flat base baseline",
                ctx.name,
                ctx.standby_schedule,
                err,
            )
            return empty
        if not isinstance(response, dict):
            return empty
        week = response.get(ctx.standby_schedule, response)
        if not isinstance(week, dict):
            return empty
        return {
            day: _entries_to_blocks(week.get(day)) for day in _WEEKDAYS
        }

    # --- Custom detection (spec §5) -----------------------------------------
    async def _async_maybe_custom(self, sid: str, reported: float) -> None:
        """Run the shadow-state algorithm for an underlying change (spec §5)."""
        ms = self.manual.get(sid)
        if ms is None:
            return
        ctx = self.zones.get(sid)
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
        # Expected schedule fallback (spec §5 step 3): the zone follows its Tado
        # schedule (``switch.<zone>_schedule`` = on), so an underlying setpoint change
        # is the schedule — not a manual knob turn (a knob turn would create an
        # overlay and flip the switch off). A manual Custom is therefore impossible
        # while the switch is on.
        expected_fallback = (
            ctx is not None and self._schedule_switch_state(ctx) == STATE_ON
        )
        decision = detect_custom_change(
            OverrideInputs(
                reported_setpoint=reported,
                last_commanded_setpoint=ms.last_commanded_setpoint,
                ha_just_commanded=ha_just,
                presence_change_recent=presence_recent,
                is_expected_schedule_fallback=expected_fallback,
            )
        )
        # The switch may just have flipped to on (overlay expiry): keep the live
        # mirror accurate (spec §5 -> control_source = tado_schedule).
        if ctx is not None:
            self._refresh_control_source(ctx)
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

    # --- control_source / in_fallback (spec §5, §9) -------------------------
    def _schedule_switch_state(self, ctx: ZoneContext) -> str | None:
        """Raw state of a zone's ``switch.<zone>_schedule`` (or None)."""
        if not ctx.schedule_switch:
            return None
        state = self.hass.states.get(ctx.schedule_switch)
        return state.state if state else None

    def _compute_control_source(self, ctx: ZoneContext) -> str | None:
        """Live mirror of the schedule switch (spec §5).

        ``switch`` off -> ``ha_overlay``; on -> ``tado_schedule``; unresolvable ->
        unknown (``None``). In master mode Fallback/Off it stays ``tado_schedule``
        on purpose (spec §5: "In master-stand Fallback/Off blijft hij bewust
        tado_schedule").
        """
        if self._mode in (MODE_FALLBACK, MODE_OFF):
            return CONTROL_SOURCE_TADO_SCHEDULE
        state = self._schedule_switch_state(ctx)
        if state in _UNREADY:
            return None
        if state == STATE_ON:
            return CONTROL_SOURCE_TADO_SCHEDULE
        return CONTROL_SOURCE_HA_OVERLAY

    def _refresh_control_source(self, ctx: ZoneContext) -> None:
        """Recompute a zone's control_source; notify its entities on a change."""
        value = self._compute_control_source(ctx)
        sid = ctx.subentry_id
        if self._control_source.get(sid) != value:
            self._control_source[sid] = value
            self._notify(sid)

    def _refresh_in_fallback(self) -> None:
        """Recompute the global in_fallback flag; push on a change (spec §9)."""
        value = self.in_fallback()
        if self._in_fallback != value:
            self._in_fallback = value
            async_dispatcher_send(self.hass, SIGNAL_GLOBAL_UPDATE)

    # --- device state (restart redundancy guard, spec §9) ------------------
    def _device_current(self, ctx: ZoneContext) -> _Applied | None:
        """Best-effort read of a zone's current applied state from its targets."""
        for target in ctx.targets:
            state = self.hass.states.get(target)
            if state is None or state.state in _UNREADY:
                continue
            if state.state == HVAC_OFF:
                return _Applied(off=True, setpoint=None)
            setpoint = self._extract_setpoint(state)
            if setpoint is not None:
                return _Applied(off=False, setpoint=setpoint)
            return _Applied(off=False, setpoint=None)
        return None

    def _device_matches(self, ctx: ZoneContext, want: _Applied) -> bool:
        """True when the device already holds the desired state (within tolerance)."""
        current = self._device_current(ctx)
        if current is None or current.off != want.off:
            return False
        if want.off:
            return True
        if current.setpoint is None or want.setpoint is None:
            return False
        return abs(current.setpoint - want.setpoint) <= _SETPOINT_TOLERANCE

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
        self,
        ctx: ZoneContext,
        ms: ZoneManualState,
        *,
        present: bool,
        window_open: bool,
    ) -> AutoInputs | None:
        away = self._read_number(ctx.away_number)
        standby = self._read_number(ctx.standby_number)
        active = self._read_number(ctx.active_number)
        if away is None or standby is None or active is None:
            return None
        boost = self._read_number(ctx.boost_number)
        if boost is None:
            boost = DEFAULT_BOOST_TEMP
        return AutoInputs(
            # Real window state (spec §3). The coordinator already short-circuits an
            # open window above (safety, across all hvac modes), so this is False
            # here; passing it keeps the engine's auto window-branch consistent.
            window_open=window_open,
            home_present=self._read_binary(self._home_present_entity),
            present=present,
            away_temp=away,
            standby_temp=standby,
            active_temp=active,
            active_schedule=self._read_schedule(ctx.active_schedule),
            standby_schedule=self._read_schedule(ctx.standby_schedule),
            active_gate=ctx.active_gate,
            preset=ms.preset,
            custom_setpoint=ms.custom_setpoint,
            boost_temp=boost,
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
            # In an on-block now (on_block=True) even when it carries no
            # temperature — a gating-only schedule still permits Active (spec §3).
            return ScheduleState(block_temp=block, on_block=True)
        return ScheduleState(block_temp=None, on_block=False)
